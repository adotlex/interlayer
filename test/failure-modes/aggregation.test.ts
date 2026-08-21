/**
 * THE AGGREGATION RULE, both directions.
 *
 *   0 attempts -> `NoProviderError`
 *   1 failure  -> THAT failure, UNWRAPPED
 *   >= 2       -> an aggregate, every cause preserved, in order
 *
 * Both halves are load-bearing and both are easy to get wrong. Wrapping a
 * single failure buries the real error one level down and breaks every
 * `err.code` assertion a consumer writes. Aggregating without keeping the
 * suppressed errors turns an N-provider outage into one arbitrary message —
 * and it is usually the FIRST failure that is diagnostic, with the rest being
 * `ECONNREFUSED` from an already-dead process.
 *
 * This file also pins the two inheritance rules that keep the chain moving:
 * `RetryExhaustedError.retryable` comes from the LAST failure, and fallback
 * does NOT key off `retryable` at all.
 */

import { describe, expect, it } from 'vitest';
import {
  type AnyInterlayerError,
  ConfigError,
  hasCode,
  ProviderError,
  TransportError,
} from '../../src/core/errors.ts';
import type { ResilienceOptions } from '../../src/core/policy.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { NO_POLICIES, probe, rejectionOf, type Say, settle } from './harness.ts';

/** Retry on, everything else off: isolates the retry aggregation rule. */
const RETRY_ONLY: (retry: ResilienceOptions['retry']) => ResilienceOptions = (retry) => ({
  retry,
  breaker: false,
  rateLimit: false,
  timeout: false,
});

/** A provider that always throws whatever the script hands it next. */
function scriptedFailures(id: string, script: readonly unknown[]) {
  let n = 0;
  return defineProvider(probe, {
    id,
    capabilities: {
      run: async (): Promise<Say> => {
        const thrown = script[Math.min(n, script.length - 1)];
        n++;
        throw thrown;
      },
    },
  });
}

/* ------------------------------------------------------------------ *
 * 1. The fallback chain
 * ------------------------------------------------------------------ */

describe('AllProvidersFailedError retains EVERY suppressed failure', () => {
  it('a three-provider chain keeps all three, in order, by identity', async () => {
    const runtime = createFakeRuntime();
    const roots = [new Error('a is down'), new Error('b is down'), new Error('c is down')];
    const providers = [
      scriptedFailures('a', [roots[0]]),
      scriptedFailures('b', [roots[1]]),
      scriptedFailures('c', [roots[2]]),
    ] as const;
    const layer = createLayer({
      contract: probe,
      providers,
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'ALL_FAILED')).toBe(true);
    if (!hasCode(error, 'ALL_FAILED')) return;

    // COUNT: not one, not the last — all three.
    expect(error.failures).toHaveLength(3);
    expect(error.triedProviderIds).toEqual(['a', 'b', 'c']);
    expect(error.failures.map((f) => f.providerId)).toEqual(['a', 'b', 'c']);

    // IDENTITY: each normalised failure still carries the exact object the
    // corresponding handler threw. The FIRST one is usually the diagnostic one.
    expect(error.failures[0]?.cause).toBe(roots[0]);
    expect(error.failures[1]?.cause).toBe(roots[1]);
    expect(error.failures[2]?.cause).toBe(roots[2]);

    // `cause` points at the last, so Node's default printing follows the chain.
    expect(error.cause).toBe(error.failures[2]);
    await layer.close();
  });

  it('the same three are on ctx.failures, shared by reference with the aggregate', async () => {
    const runtime = createFakeRuntime();
    const providers = [
      scriptedFailures('a', [new TransportError('a')]),
      scriptedFailures('b', [new TransportError('b')]),
      scriptedFailures('c', [new TransportError('c')]),
    ] as const;
    const layer = createLayer({
      contract: probe,
      providers,
      runtime,
      resilience: NO_POLICIES,
    });

    let observed: readonly AnyInterlayerError[] = [];
    layer.use(async (ctx, next) => {
      try {
        return await next();
      } finally {
        // `failures` survives every `{...ctx}` derivation by reference, which
        // is what lets middleware read the whole history without catching.
        observed = [...ctx.failures];
      }
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'ALL_FAILED')).toBe(true);
    if (!hasCode(error, 'ALL_FAILED')) return;
    expect(observed).toHaveLength(3);
    expect(observed[0]).toBe(error.failures[0]);
    expect(observed[2]).toBe(error.failures[2]);
    await layer.close();
  });

  it('a five-provider chain keeps five — nothing is capped or de-duplicated', async () => {
    const runtime = createFakeRuntime();
    const ids = ['p1', 'p2', 'p3', 'p4', 'p5'] as const;
    const providers = ids.map((id) => scriptedFailures(id, [new TransportError('same message')]));
    const layer = createLayer({
      contract: probe,
      providers,
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    if (!hasCode(error, 'ALL_FAILED')) {
      expect.unreachable('expected ALL_FAILED');
      return;
    }
    // Identical messages must not collapse into one entry.
    expect(error.failures).toHaveLength(5);
    expect(error.triedProviderIds).toEqual([...ids]);
    expect(new Set(error.failures).size).toBe(5);
    await layer.close();
  });
});

describe('single failure surfaces unwrapped; two or more aggregate', () => {
  it('ONE failing provider surfaces its own error, never ALL_FAILED', async () => {
    const runtime = createFakeRuntime();
    const only = scriptedFailures('only', [new TransportError('the real problem')]);
    const layer = createLayer({
      contract: probe,
      providers: [only],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // Wrapping would bury it one level down and break `err.code`.
    expect(error.code).toBe('TRANSPORT');
    expect(error.message).toBe('the real problem');
    expect(hasCode(error, 'ALL_FAILED')).toBe(false);
    await layer.close();
  });

  it('TWO failing providers is the first count that aggregates', async () => {
    const runtime = createFakeRuntime();
    const providers = [
      scriptedFailures('a', [new TransportError('a')]),
      scriptedFailures('b', [new TransportError('b')]),
    ] as const;
    const layer = createLayer({
      contract: probe,
      providers,
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(error.code).toBe('ALL_FAILED');
    if (!hasCode(error, 'ALL_FAILED')) return;
    expect(error.failures).toHaveLength(2);
    await layer.close();
  });

  it('two providers but only one TRIED surfaces unwrapped (maxProviders: 1)', async () => {
    const runtime = createFakeRuntime();
    let bCalls = 0;
    const a = scriptedFailures('a', [new TransportError('a')]);
    const b = defineProvider(probe, {
      id: 'b',
      capabilities: {
        run: async (): Promise<Say> => {
          bCalls++;
          return { a: 'ok', by: 'b' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      maxProviders: 1,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // The count that matters is failures COLLECTED, not providers registered.
    expect(error.code).toBe('TRANSPORT');
    expect(bCalls).toBe(0);
    await layer.close();
  });

  it('a chain that stops early on a no-fallback code surfaces that code unwrapped', async () => {
    const runtime = createFakeRuntime();
    let bCalls = 0;
    const a = scriptedFailures('a', [new ConfigError('misconfigured credentials')]);
    const b = defineProvider(probe, {
      id: 'b',
      capabilities: {
        run: async (): Promise<Say> => {
          bCalls++;
          return { a: 'ok', by: 'b' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // CONFIG is in NO_FALLBACK_CODES: every provider would reject identically.
    expect(error.code).toBe('CONFIG');
    expect(bCalls).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. The retry loop
 * ------------------------------------------------------------------ */

describe('RetryExhaustedError aggregates at two, never at one', () => {
  it('exactly one failure is rethrown unwrapped even with maxAttempts: 3', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const stubborn = defineProvider(probe, {
      id: 'stubborn',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          // Non-retryable: the loop stops after one physical invocation.
          throw new ProviderError('deterministic bug');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [stubborn],
      runtime,
      resilience: RETRY_ONLY({ maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(calls).toBe(1);
    // `maxAttempts: 1` — and a first failure that stops the loop — must behave
    // exactly like no retry wrapper at all.
    expect(error.code).toBe('PROVIDER_ERROR');
    expect(hasCode(error, 'RETRY_EXHAUSTED')).toBe(false);
    expect(runtime.now()).toBe(0); // nothing slept
    await layer.close();
  });

  it('maxAttempts: 1 is indistinguishable from no retry wrapper', async () => {
    const runtime = createFakeRuntime();
    const only = scriptedFailures('only', [new TransportError('down')]);
    const layer = createLayer({
      contract: probe,
      providers: [only],
      runtime,
      resilience: RETRY_ONLY({ maxAttempts: 1 }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(error.code).toBe('TRANSPORT');
    await layer.close();
  });

  it('two failures aggregate, chronologically, with cause = the last', async () => {
    const runtime = createFakeRuntime();
    const first = new TransportError('first: the diagnostic one');
    const second = new TransportError('second: ECONNREFUSED');
    const dying = scriptedFailures('dying', [first, second]);
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      resilience: RETRY_ONLY({ maxAttempts: 2, strategy: 'fixed', baseDelayMs: 25 }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'RETRY_EXHAUSTED')).toBe(true);
    if (!hasCode(error, 'RETRY_EXHAUSTED')) return;
    expect(error.attempts).toBe(2);
    expect(error.errors).toHaveLength(2);
    expect(error.errors[0]).toBe(first); // the first is NOT discarded
    expect(error.errors[1]).toBe(second);
    expect(error.cause).toBe(second);
    expect(error.message).toContain('second: ECONNREFUSED');
    expect(runtime.now()).toBe(25);
    await layer.close();
  });

  it('three failures keep all three, not a sliding window of the last two', async () => {
    const runtime = createFakeRuntime();
    const script = [
      new TransportError('one'),
      new TransportError('two'),
      new TransportError('three'),
    ];
    const dying = scriptedFailures('dying', script);
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      resilience: RETRY_ONLY({ maxAttempts: 3, strategy: 'fixed', baseDelayMs: 5 }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    if (!hasCode(error, 'RETRY_EXHAUSTED')) {
      expect.unreachable('expected RETRY_EXHAUSTED');
      return;
    }
    expect(error.attempts).toBe(3);
    expect(error.errors).toEqual(script);
    await layer.close();
  });
});

describe('RetryExhaustedError.retryable is inherited from the LAST failure', () => {
  it('a retryable last failure makes the wrapper retryable', async () => {
    const runtime = createFakeRuntime();
    const dying = scriptedFailures('dying', [
      new ProviderError('not retryable'),
      new TransportError('retryable'),
    ]);
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      // Force both attempts regardless of the errors' own classification, so
      // the ONLY thing under test is what the wrapper inherits.
      resilience: RETRY_ONLY({
        maxAttempts: 2,
        strategy: 'fixed',
        baseDelayMs: 0,
        isRetryable: () => true,
      }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    if (!hasCode(error, 'RETRY_EXHAUSTED')) {
      expect.unreachable('expected RETRY_EXHAUSTED');
      return;
    }
    expect(error.errors).toHaveLength(2);
    expect(error.retryable).toBe(true);
    await layer.close();
  });

  it('a non-retryable last failure makes the wrapper non-retryable', async () => {
    const runtime = createFakeRuntime();
    const dying = scriptedFailures('dying', [
      new TransportError('retryable'),
      new ProviderError('not retryable'),
    ]);
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      resilience: RETRY_ONLY({
        maxAttempts: 2,
        strategy: 'fixed',
        baseDelayMs: 0,
        isRetryable: () => true,
      }),
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    if (!hasCode(error, 'RETRY_EXHAUSTED')) {
      expect.unreachable('expected RETRY_EXHAUSTED');
      return;
    }
    // If the wrapper answered for itself this would be a constant, and the
    // routing layer above would lose the only signal it has.
    expect(error.retryable).toBe(false);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Fallback does NOT key off `retryable`
 * ------------------------------------------------------------------ */

describe('the fallback rule is "advance unless a different provider is futile"', () => {
  it('the most ordinary handler throw in the world still reaches a healthy alternate', async () => {
    const runtime = createFakeRuntime();
    const buggy = defineProvider(probe, {
      id: 'buggy',
      capabilities: {
        run: async (): Promise<Say> => {
          // Becomes a NON-RETRYABLE ProviderError. Keying fallback off
          // `retryable` stranded the call here with a healthy alternate unused.
          throw new Error('undefined is not a function');
        },
      },
    });
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'healthy' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [buggy, healthy],
      runtime,
      resilience: NO_POLICIES,
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('healthy');
    expect(meta.errors).toHaveLength(1);
    expect(meta.errors[0]?.retryable).toBe(false); // …and it advanced anyway
    await layer.close();
  });

  it('a non-retryable RETRY_EXHAUSTED still advances the chain', async () => {
    const runtime = createFakeRuntime();
    const dying = scriptedFailures('dying', [
      new TransportError('flaky'),
      new ProviderError('then a hard failure'),
    ]);
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'healthy' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [dying, healthy],
      runtime,
      resilience: RETRY_ONLY({
        maxAttempts: 2,
        strategy: 'fixed',
        baseDelayMs: 0,
        isRetryable: () => true,
      }),
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('healthy');
    expect(meta.errors).toHaveLength(1);
    expect(meta.errors[0]?.code).toBe('RETRY_EXHAUSTED');
    expect(meta.errors[0]?.retryable).toBe(false);
    await layer.close();
  });

  it('an unknown code from a custom policy falls back rather than failing the call', async () => {
    const runtime = createFakeRuntime();
    // A future/unfamiliar code must fall back: an unnecessary hop costs one
    // wasted call, a missed hop costs an outage.
    const odd = scriptedFailures('odd', [new ProviderError('surprising', {}, 418)]);
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'healthy' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [odd, healthy],
      runtime,
      resilience: NO_POLICIES,
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('healthy');
    await layer.close();
  });
});
