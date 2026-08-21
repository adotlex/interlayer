/**
 * ABORT AT EVERY LAYER OF THE STACK.
 *
 * There are four places a call can be waiting when the caller withdraws — in a
 * provider handler, asleep in a retry backoff, parked in the rate limiter's
 * FIFO queue, or holding a circuit breaker's half-open probe slot — and each
 * one is a separate piece of code that has to notice, unwind and let go of its
 * timer. A miss shows up as a hung call, a leaked timer holding the event loop
 * open, or (worst) the right failure under the wrong `code`.
 *
 * ── THE RULE THAT COSTS THE MOST WHEN IT IS BROKEN (R4 §2.3) ──────────────
 *
 * A caller abort and a deadline breach must stay distinguishable all the way
 * out to `err.code`. They are not the same event and they do not want the same
 * response: `CANCELLED` is never retryable and never falls back, so relabelling
 * a timeout as a cancellation silently converts "try the backup" into "give up".
 * Four sites in the library each decide this independently, and each is pinned
 * below by aborting with a TYPED reason that only that site could pass through
 * unchanged — `timeoutMs: 999` is a value no policy in the stack can invent.
 */

import { describe, expect, it } from 'vitest';
import { hasCode, TimeoutError, TransportError } from '../../src/core/errors.ts';
import type { AnyInterlayerError, InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import {
  abortAt,
  expectNoLeakedTimers,
  NO_POLICIES,
  probe,
  rejectionOf,
  type Say,
  settle,
} from './harness.ts';

/** A reason no policy in the stack could ever manufacture. */
function foreignDeadline(): TimeoutError {
  return new TimeoutError(999, 'call', { details: { source: 'an outer framework' } });
}

/* ------------------------------------------------------------------ *
 * 1. Abort while a HANDLER is in flight
 * ------------------------------------------------------------------ */

describe('abort during a provider call', () => {
  it('surfaces CANCELLED through the whole stack, and never tries the backup', async () => {
    const runtime = createFakeRuntime();
    let backupCalls = 0;
    const slow = defineProvider(probe, {
      id: 'slow',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(1_000, ctx.signal);
          return { a: 'never', by: 'slow' };
        },
      },
    });
    const backup = defineProvider(probe, {
      id: 'backup',
      capabilities: {
        run: async (): Promise<Say> => {
          backupCalls++;
          return { a: 'ok', by: 'backup' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [slow, backup],
      runtime,
      // Every policy ON: retry, rate limit, breaker and both timeouts each get
      // a chance to relabel this, and none of them may.
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 50 },
        rateLimit: { capacity: 10, refillPerSec: 10 },
        breaker: { minimumThroughput: 10 },
        timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
      },
    });

    const controller = new AbortController();
    abortAt(runtime, 100, controller);
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(error.code).toBe('CANCELLED');
    expect(error.retryable).toBe(false);
    expect(runtime.now()).toBe(100); // not 5_000, not 60_000
    // A withdrawal is not a provider failure: "try harder" is the wrong answer.
    expect(backupCalls).toBe(0);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('keeps the failures collected BEFORE the abort on ctx.failures', async () => {
    const runtime = createFakeRuntime();
    const a = defineProvider(probe, {
      id: 'a',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new TransportError('a is down');
        },
      },
    });
    const b = defineProvider(probe, {
      id: 'b',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(1_000, ctx.signal);
          return { a: 'never', by: 'b' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      resilience: NO_POLICIES,
    });

    let observed: readonly AnyInterlayerError[] = [];
    layer.use(async (ctx, next) => {
      try {
        return await next();
      } finally {
        observed = [...ctx.failures];
      }
    });

    const controller = new AbortController();
    abortAt(runtime, 200, controller);
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    // The caller is told about the withdrawal, not about an aggregate…
    expect(error.code).toBe('CANCELLED');
    // …but nothing was thrown away.
    expect(observed.map((e) => e.code)).toEqual(['TRANSPORT', 'CANCELLED']);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Abort while asleep in a RETRY BACKOFF
 * ------------------------------------------------------------------ */

describe('abort during a retry backoff', () => {
  it('surfaces CANCELLED, stops the loop where it stands, and clears the sleep timer', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const flapping = defineProvider(probe, {
      id: 'flapping',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          throw new TransportError(`attempt ${String(calls)}`);
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [flapping],
      runtime,
      resilience: {
        breaker: false,
        rateLimit: false,
        timeout: false,
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 100 },
      },
    });
    const scheduled: InterlayerEvents['retry:scheduled'][] = [];
    layer.on('retry:scheduled', (payload) => {
      scheduled.push(payload);
    });

    const controller = new AbortController();
    // t=0 attempt 1 fails, sleeps to 100; attempt 2 fails, sleeps 100..200.
    abortAt(runtime, 150, controller);
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(error.code).toBe('CANCELLED');
    expect(calls).toBe(2); // not 5: the loop stopped mid-backoff
    expect(scheduled).toHaveLength(2);
    expect(runtime.now()).toBe(150);
    // An uncleared backoff timer holds the event loop open for its full
    // duration — the real leak, and the one `sleep(ms, signal)` exists to stop.
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Abort while QUEUED IN THE RATE LIMITER
 * ------------------------------------------------------------------ */

describe('abort while queued for a rate-limit token', () => {
  it('surfaces CANCELLED without spending a token or entering the provider', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const paced = defineProvider(probe, {
      id: 'paced',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: 'ok', by: 'paced' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [paced],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        // One token, one per second: the next caller waits a full second.
        rateLimit: { capacity: 1, refillPerSec: 1, onExhaustion: 'wait' },
      },
    });

    await settle(runtime, layer.call('run', { q: 'x' })); // spends the only token
    expect(calls).toBe(1);

    const controller = new AbortController();
    abortAt(runtime, 300, controller); // 300ms into a 1000ms queue wait
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(error.code).toBe('CANCELLED');
    expect(error.message).toContain('queued for a rate-limit token');
    expect(calls).toBe(1); // never admitted
    expect(runtime.now()).toBe(300);
    // Dropping the last waiter must DISARM the head-of-queue wake-up.
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. Abort while a BREAKER HALF-OPEN PROBE is in flight
 * ------------------------------------------------------------------ */

describe('abort during a half-open probe', () => {
  it('surfaces CANCELLED, and the probe is discarded rather than scored', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const recovering = defineProvider(probe, {
      id: 'recovering',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          calls++;
          if (calls <= 2) throw new TransportError('down');
          if (calls === 3) {
            // The probe: still in flight when the caller withdraws.
            await ctx.runtime.sleep(500, ctx.signal);
            return { a: 'unreachable', by: 'recovering' };
          }
          return { a: 'ok', by: 'recovering' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [recovering],
      runtime,
      resilience: {
        retry: false,
        rateLimit: false,
        timeout: false,
        breaker: {
          minimumThroughput: 2,
          failureRatio: 0.5,
          resetMs: 1_000,
          halfOpenMaxConcurrent: 1,
          halfOpenSuccessesToClose: 1,
        },
      },
    });
    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (payload) => {
      transitions.push(payload);
    });

    await rejectionOf(runtime, layer.call('run', { q: 'x' }));
    await rejectionOf(runtime, layer.call('run', { q: 'x' })); // trips it open
    expect(transitions.map((t) => `${t.from}->${t.to}`)).toEqual(['closed->open']);

    await runtime.advance(1_000); // the cooldown elapses

    const controller = new AbortController();
    abortAt(runtime, 200, controller);
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(error.code).toBe('CANCELLED');
    expect(calls).toBe(3); // the probe really was admitted
    expect(runtime.now()).toBe(1_200);
    // A burst of client-side cancellations must not take a healthy provider out
    // of service: the outcome is scored NEITHER way and the slot is released.
    expect(transitions.map((t) => `${t.from}->${t.to}`)).toEqual([
      'closed->open',
      'open->half-open',
    ]);

    // Proof the slot was released: the very next call is admitted as a probe.
    const reply = await settle(runtime, layer.call('run', { q: 'x' }));
    expect(reply.by).toBe('recovering');
    expect(transitions.map((t) => `${t.from}->${t.to}`)).toEqual([
      'closed->open',
      'open->half-open',
      'half-open->closed',
    ]);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 5. Caller-abort vs timeout-abort — the four relabelling sites (R4 §2.3)
 * ------------------------------------------------------------------ */

describe('a typed abort reason is surfaced as itself, never relabelled CANCELLED', () => {
  it('site 1/4 — routing/fallback.ts abortErrorFor(): before the first candidate', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const fine = defineProvider(probe, {
      id: 'fine',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: 'ok', by: 'fine' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [fine],
      runtime,
      resilience: NO_POLICIES,
    });

    const controller = new AbortController();
    controller.abort(foreignDeadline());
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.timeoutMs).toBe(999); // MY error, not one the stack invented
    expect(error.scope).toBe('call');
    expect(error.capability).toBe('run'); // merely stamped with call identity
    expect(calls).toBe(0);
    await layer.close();
  });

  it('site 2/4 — resilience/timeout.ts inheritedAbortError(): mid-attempt', async () => {
    const runtime = createFakeRuntime();
    const deaf = defineProvider(probe, {
      id: 'deaf',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          // Ignores the signal on purpose, so ONLY the timeout wrapper can
          // decide what this abort is called.
          await ctx.runtime.sleep(1_000);
          return { a: 'never', by: 'deaf' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [deaf],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 60_000 },
      },
    });

    const controller = new AbortController();
    abortAt(runtime, 200, controller, foreignDeadline());
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    // Relabelling this `CANCELLED` reads as "the caller hung up" when a
    // deadline was breached — and makes it non-retryable and non-falling-back.
    expect(error.timeoutMs).toBe(999);
    expect(runtime.now()).toBe(200);
    await layer.close();
  });

  it('site 3/4 — core/runtime.ts interruptionOf(): asleep in a retry backoff', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const flapping = defineProvider(probe, {
      id: 'flapping',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          throw new TransportError('down');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [flapping],
      runtime,
      resilience: {
        breaker: false,
        rateLimit: false,
        timeout: false,
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 100 },
      },
    });

    const controller = new AbortController();
    abortAt(runtime, 150, controller, foreignDeadline());
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.timeoutMs).toBe(999);
    expect(calls).toBe(2);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('site 4/4 — rate-limit.ts onAbort(): parked in the FIFO queue', async () => {
    const runtime = createFakeRuntime();
    const paced = defineProvider(probe, {
      id: 'paced',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'paced' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [paced],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        rateLimit: { capacity: 1, refillPerSec: 1, onExhaustion: 'wait' },
      },
    });

    await settle(runtime, layer.call('run', { q: 'x' }));

    const controller = new AbortController();
    abortAt(runtime, 300, controller, foreignDeadline());
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.timeoutMs).toBe(999);
    expect(runtime.now()).toBe(300);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('a typed reason that is NOT a timeout survives too, and stops the chain', async () => {
    const runtime = createFakeRuntime();
    let backupCalls = 0;
    const slow = defineProvider(probe, {
      id: 'slow',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(1_000, ctx.signal);
          return { a: 'never', by: 'slow' };
        },
      },
    });
    const backup = defineProvider(probe, {
      id: 'backup',
      capabilities: {
        run: async (): Promise<Say> => {
          backupCalls++;
          return { a: 'ok', by: 'backup' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [slow, backup],
      runtime,
      resilience: NO_POLICIES,
    });

    const controller = new AbortController();
    abortAt(runtime, 100, controller, new TransportError('upstream connection reset'));
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    // "…or whatever typed error the signal carried as its reason."
    expect(error.code).toBe('TRANSPORT');
    expect(error.message).toBe('upstream connection reset');
    // The caller withdrew, so the chain stops where it stands regardless.
    expect(backupCalls).toBe(0);
    await layer.close();
  });
});

describe('the library’s OWN deadlines stay labelled TIMEOUT', () => {
  it('an attempt timeout is TIMEOUT/attempt, not CANCELLED', async () => {
    const runtime = createFakeRuntime();
    const slow = defineProvider(probe, {
      id: 'slow',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(10_000, ctx.signal);
          return { a: 'never', by: 'slow' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [slow],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 250, totalTimeoutMs: 60_000 },
      },
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('attempt');
    expect(error.retryable).toBe(true); // a different try may be quicker
    expect(runtime.now()).toBe(250);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('a total timeout is TIMEOUT/call, and the caller sees it even mid-handler', async () => {
    const runtime = createFakeRuntime();
    const slow = defineProvider(probe, {
      id: 'slow',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(10_000, ctx.signal);
          return { a: 'never', by: 'slow' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [slow],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        // The attempt budget is clamped to the call deadline, so both timers
        // are due at 400; the OUTER one was armed first and the fake clock
        // breaks ties by insertion, so the call-scoped error is what surfaces.
        timeout: { attemptTimeoutMs: 30_000, totalTimeoutMs: 400 },
      },
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('call');
    expect(error.timeoutMs).toBe(400);
    expect(runtime.now()).toBe(400);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });
});
