/**
 * THE ONION, READ OFF THE EVENT STREAM.
 *
 * Each policy owns a different event, and each event is emitted from inside
 * that policy's own frame. So the ORDER the emitter sees is a direct reading of
 * the nesting — no introspection, no `describe()`, no timing arithmetic:
 *
 * ```
 *   call:start / call:*         the facade          outside everything
 *   provider:selected           the router          terminal of the call stack
 *   retry:scheduled             Retry               outermost attempt policy
 *   ratelimit:throttled         RateLimit           inside Retry
 *   breaker:transition          CircuitBreaker      inside RateLimit
 *   attempt:start / :failure    the handler frame   innermost
 * ```
 *
 * If any policy moved, these sequences would change shape.
 */

import { describe, expect, it } from 'vitest';
import type { CallContext, InterlayerEventName } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { echoContract, rejectionOf, scripted, settle } from './harness.ts';

/** Every event name the layer emits, in order. */
function recordOrder(events: { onAny(fn: (name: InterlayerEventName) => void): unknown }): {
  readonly names: readonly InterlayerEventName[];
} {
  const names: InterlayerEventName[] = [];
  events.onAny((name) => {
    names.push(name);
  });
  return {
    get names(): readonly InterlayerEventName[] {
      return names;
    },
  };
}

describe('the emitted order IS the nesting order', () => {
  it('retry:scheduled precedes ratelimit:throttled precedes attempt:start', async () => {
    const runtime = createFakeRuntime();
    const flaky = scripted({ id: 'p', failures: 1 });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: flaky.handler } })],
      resilience: {
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 10 },
        // One token: the RETRY is the call that has to queue for a refill.
        rateLimit: { capacity: 1, refillPerSec: 1 },
        breaker: false,
        timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });
    const log = recordOrder(layer.events);

    await settle(runtime, layer.call('echo', { n: 1 }));

    expect(log.names).toEqual([
      'call:start',
      'provider:selected',
      // -- attempt 1 --
      'attempt:start',
      'attempt:failure',
      // Retry is OUTSIDE the limiter, so it schedules first ...
      'retry:scheduled',
      // ... and the limiter then throttles the retry it scheduled, BEFORE the
      // physical call the throttle is protecting.
      'ratelimit:throttled',
      // -- attempt 2 --
      'attempt:start',
      'attempt:success',
      'call:success',
    ]);
    expect(flaky.at, 'the retry waited out the token refill').toEqual([0, 1_000]);
    await layer.close();
  });

  it('breaker:transition lands between the handler failure and the retry schedule', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'p', failures: Number.POSITIVE_INFINITY });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: dead.handler } })],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        rateLimit: false,
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
        timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });
    const log = recordOrder(layer.events);

    await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    expect(log.names).toEqual([
      'call:start',
      'provider:selected',
      'attempt:start',
      // The handler frame reports first (it is innermost) ...
      'attempt:failure',
      // ... the breaker scores the same failure on the way out (it wraps the
      // handler) ...
      'breaker:transition',
      // ... and retry, outside the breaker, decides afterwards.
      'retry:scheduled',
      // Attempt 2 is refused by the now-open breaker: no `attempt:start`,
      // because the terminal was never reached.
      'call:failure',
    ]);
    expect(dead.callCount).toBe(1);
    await layer.close();
  });

  it('fallback:advance is emitted by the router, outside every attempt policy', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'a', failures: Number.POSITIVE_INFINITY });
    const good = scripted({ id: 'b' });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'a', capabilities: { echo: dead.handler } }),
        defineProvider(echoContract, { id: 'b', capabilities: { echo: good.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 10 },
        rateLimit: false,
        breaker: false,
        timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });
    const log = recordOrder(layer.events);

    await settle(runtime, layer.call('echo', { n: 1 }));

    expect(log.names).toEqual([
      'call:start',
      'provider:selected',
      'attempt:start',
      'attempt:failure',
      'retry:scheduled',
      'attempt:start',
      'attempt:failure',
      // Both retries against `a` are spent before the chain moves on: the retry
      // loop is INSIDE the fallback walk, per candidate.
      'fallback:advance',
      'provider:selected',
      'attempt:start',
      'attempt:success',
      'call:success',
    ]);
    expect(dead.callCount, 'a full retry budget per provider').toBe(2);
    expect(good.callCount).toBe(1);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * User middleware on the call stack
 * ------------------------------------------------------------------ */

describe('user middleware (kind "custom") on the call stack', () => {
  it('runs inside the total timeout and in declaration order', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p' });
    const order: string[] = [];
    const deadlines: (number | undefined)[] = [];
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } }),
      ],
      resilience: {
        retry: false,
        rateLimit: false,
        breaker: false,
        timeout: { attemptTimeoutMs: 100, totalTimeoutMs: 400 },
      },
      runtime,
    });

    const record = (tag: string) => async (ctx: CallContext, next: () => Promise<unknown>) => {
      order.push(`>${tag}`);
      deadlines.push(ctx.deadlineAt);
      try {
        return await next();
      } finally {
        order.push(`<${tag}`);
      }
    };
    layer.use(record('first')).use(record('second'));

    await settle(runtime, layer.call('echo', { n: 1 }));

    // `custom` sorts LAST in POLICY_ORDER and ties break on declaration order.
    expect(order).toEqual(['>first', '>second', '<second', '<first']);
    // Both see the deadline the total timeout armed, so both are inside it.
    expect(deadlines, 'startedAt(0) + totalTimeoutMs(400)').toEqual([400, 400]);
    await layer.close();
  });
});
