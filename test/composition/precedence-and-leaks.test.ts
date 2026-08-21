/**
 * WHICH ERROR WINS, AND WHAT IS LEFT BEHIND (ORD-10, ORD-11, ORD-12).
 *
 * With five policies wrapped around one call there is rarely exactly one thing
 * going wrong, and the nesting order decides which of them the caller is told
 * about. R4 §6 ORD-11 writes the intended precedence as a flat list:
 *
 *   Cancelled > Timeout(total) > Timeout(attempt) > CircuitOpen > RateLimited
 *             > RetryExhausted
 *
 * Two entries in that list do not survive contact with the canonical order, and
 * BOTH are consequences the order was chosen with its eyes open. They are
 * recorded here as executable facts rather than smoothed over:
 *
 *  1. `RateLimit` sits OUTSIDE `CircuitBreaker` (R4 §5.6, deliberate). In the
 *     default `'wait'` mode that is invisible — the limiter waits and then the
 *     breaker refuses, so CIRCUIT_OPEN still surfaces. In `'reject'` mode the
 *     limiter throws FIRST and RATE_LIMITED surfaces, inverting those two
 *     entries. §5.6 names this cost explicitly ("we spend a token before
 *     discovering it").
 *  2. `RetryExhaustedError` is a WRAPPER, not a peer (R4 §1.6). Two or more
 *     failures inside one loop are aggregated, so RETRY_EXHAUSTED is what
 *     surfaces and the "higher-precedence" error is reached through `.cause`.
 *     A single failure is never wrapped, which is when the list holds exactly.
 */

import { describe, expect, it } from 'vitest';
import { hasCode, TransportError } from '../../src/core/errors.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime, fakeProvider, testContext } from '../support/index.ts';
import {
  atVirtual,
  breakerView,
  buildComposition,
  echoContract,
  probedSignal,
  rejectionOf,
  scripted,
  settle,
  tokensOf,
} from './harness.ts';

/* ------------------------------------------------------------------ *
 * 1. ORD-10 — the fully-enabled happy path costs exactly one of everything
 * ------------------------------------------------------------------ */

describe('every layer enabled, provider succeeds immediately (ORD-10)', () => {
  it('spends 1 token, records 1 breaker success, sleeps 0 times, leaves 0 timers', async () => {
    const provider = fakeProvider('p').alwaysSucceed('ok');
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
      rateLimit: { capacity: 10, refillPerSec: 0, key: 'p' },
      breaker: {},
      timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 5_000 },
    });
    const limiter = composition.limiter;
    const breaker = composition.breaker;
    if (limiter === undefined || breaker === undefined) throw new Error('policies not built');

    const value = await settle(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(value).toBe('ok');
    expect(provider.callCount, 'one physical call').toBe(1);
    expect(tokensOf(limiter), 'one token').toBe(9);

    const view = breakerView(breaker, 'p');
    expect(view?.state).toBe('closed');
    expect(view?.bucketCount, 'exactly one recorded outcome in the window').toBe(1);
    expect(view?.consecutiveFailures, 'and it was a success').toBe(0);

    expect(handle.runtime.slept, 'no backoff, no queue wait').toEqual([]);
    expect(handle.runtime.pendingTimers, 'both deadline timers disposed').toBe(0);
    await composition.dispose();
  });

  it('the same through the facade: one attempt, no sleeps, no timers', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p' });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } })],
      // All four policies at their defaults — nothing switched off.
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('echo', { n: 3 }));

    expect(meta.value).toEqual({ n: 3, by: 'p' });
    expect(meta.attempts).toBe(1);
    expect(meta.errors).toEqual([]);
    expect(provider.at).toEqual([0]);
    expect(runtime.slept).toEqual([]);
    expect(runtime.now(), 'no virtual time passed at all').toBe(0);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
    expect(runtime.pendingTimers, 'and close() releases nothing it should not').toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 2. ORD-11 — precedence, pair by pair
 * ------------------------------------------------------------------ */

describe('error precedence (ORD-11)', () => {
  it('CANCELLED beats a total timeout that would have fired later', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const caller = new AbortController();
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } })],
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 100 },
      },
      runtime,
    });

    atVirtual(runtime, 40, () => {
      caller.abort();
    });
    const error = await rejectionOf(
      runtime,
      layer.call('echo', { n: 1 }, { signal: caller.signal }),
    );

    expect(error.code, 'the caller hung up before the deadline did').toBe('CANCELLED');
    expect(runtime.now()).toBe(40);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a total timeout beats the attempt timeout it clamped to the same instant', async () => {
    // `composeDeadline` clamps the attempt budget to the remaining call budget,
    // so on the LAST attempt both timers are due at the same virtual instant.
    // The outer policy armed its timer first and therefore wins the tie — which
    // is the correct answer, and not an accident worth leaving unasserted.
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } })],
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 60 },
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope).toBe('call');
      expect(error.timeoutMs).toBe(60);
    }
    expect(runtime.now()).toBe(60);
    await layer.close();
  });

  it('an ATTEMPT timeout beats the breaker trip it causes', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } })],
      resilience: {
        retry: false,
        rateLimit: false,
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
        timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    // The timeout IS the failure the breaker records, so it opens on this very
    // call — but the caller is told about the timeout, not about the breaker.
    const first = await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(first.code).toBe('TIMEOUT');
    if (hasCode(first, 'TIMEOUT')) expect(first.scope).toBe('attempt');

    // The NEXT call is the one the breaker answers, unwrapped.
    const second = await rejectionOf(runtime, layer.call('echo', { n: 2 }));
    expect(second.code).toBe('CIRCUIT_OPEN');
    expect(hung.callCount, 'the second call never reached the provider').toBe(1);
    await layer.close();
  });

  it('CIRCUIT_OPEN surfaces unwrapped when it is the loop’s ONLY failure', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } })],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 },
        rateLimit: false,
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    // Call 1: attempt 1 fails and trips the breaker, attempt 2 is refused. TWO
    // failures in one loop, so the aggregate wrapper is what surfaces.
    const first = await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(first.code).toBe('RETRY_EXHAUSTED');
    if (hasCode(first, 'RETRY_EXHAUSTED')) {
      expect(first.attempts).toBe(2);
      expect(hasCode(first.errors.at(-1), 'CIRCUIT_OPEN')).toBe(true);
    }
    expect(dead.callCount, 'attempt 2 never reached the provider').toBe(1);

    // Call 2: attempt 1 is refused, `CircuitOpenError` is non-retryable, the
    // loop stops with a SINGLE failure — so no `RetryExhaustedError` wrapper,
    // and the flat ORD-11 precedence holds exactly.
    const second = await rejectionOf(runtime, layer.call('echo', { n: 2 }));
    expect(second.code).toBe('CIRCUIT_OPEN');
    expect(dead.callCount).toBe(1);
    await layer.close();
  });

  it('in the default WAIT mode, an open breaker still wins after the queue drains', async () => {
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      // One token, already spent by the first run; the second run must wait.
      rateLimit: { capacity: 1, refillPerSec: 1, key: 'p', onExhaustion: 'wait' },
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
      timeout: false,
    });
    const limiter = composition.limiter;
    const breaker = composition.breaker;
    if (limiter === undefined || breaker === undefined) throw new Error('policies not built');

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );
    expect(breakerView(breaker, 'p')?.state).toBe('open');

    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(error.code, 'the limiter paced, then the breaker refused').toBe('CIRCUIT_OPEN');
    expect(handle.runtime.now(), 'a full second of queueing to be told no').toBe(1_000);
    expect(tokensOf(limiter), 'and the token was spent on the way').toBe(0);
    expect(provider.callCount).toBe(1);
    await composition.dispose();
  });

  it('in REJECT mode the limiter pre-empts the breaker — RATE_LIMITED, not CIRCUIT_OPEN', async () => {
    // The one place the flat ORD-11 list inverts. This is the acknowledged cost
    // of putting RateLimit outside CircuitBreaker (R4 §5.6): in reject mode the
    // limiter answers before the breaker is ever consulted. Asserting it so the
    // ordering cannot be "fixed" back to Polly's without this test failing.
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      rateLimit: { capacity: 1, refillPerSec: 1, key: 'p', onExhaustion: 'reject' },
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
      timeout: false,
    });
    const breaker = composition.breaker;
    if (breaker === undefined) throw new Error('breaker was not built');

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );
    expect(breakerView(breaker, 'p')?.state).toBe('open');

    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(error.code).toBe('RATE_LIMITED');
    if (hasCode(error, 'RATE_LIMITED')) expect(error.retryAfterMs).toBe(1_000);
    expect(handle.runtime.now(), 'reject mode never waits').toBe(0);
    await composition.dispose();
  });

  it('RETRY_EXHAUSTED wraps rather than yields — the winner is its `cause`', async () => {
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 },
      // Two tokens for three attempts: attempt 3 is refused by the limiter.
      rateLimit: { capacity: 2, refillPerSec: 0, key: 'p', onExhaustion: 'reject' },
      breaker: false,
      timeout: false,
    });

    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(provider.callCount, 'two physical calls; the third never got a token').toBe(2);
    expect(error.code, 'three failures in one loop are aggregated (R4 §1.6)').toBe(
      'RETRY_EXHAUSTED',
    );
    if (hasCode(error, 'RETRY_EXHAUSTED')) {
      expect(error.attempts).toBe(3);
      expect(hasCode(error.errors.at(-1), 'RATE_LIMITED'), 'the higher-precedence error').toBe(
        true,
      );
      expect(error.cause).toBe(error.errors.at(-1));
    }
    await composition.dispose();
  });
});

/* ------------------------------------------------------------------ *
 * 3. ORD-12 — nothing outlives the call
 * ------------------------------------------------------------------ */

describe('leak guard after every terminal state (ORD-12)', () => {
  it('success: no timers, and every caller-signal listener detached', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p', delayMs: 25 });
    const probe = probedSignal();
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } })],
      runtime,
    });

    await settle(runtime, layer.call('echo', { n: 1 }, { signal: probe.signal }));

    expect(probe.adds, 'the probe really did observe the stack attaching').toBeGreaterThan(0);
    expect(probe.attached, 'and every one of them was removed').toBe(0);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('retry exhaustion through repeated attempt timeouts leaves nothing behind', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const probe = probedSignal();
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } })],
      resilience: {
        retry: { maxAttempts: 4, strategy: 'fixed', baseDelayMs: 5 },
        timeout: { attemptTimeoutMs: 20, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('echo', { n: 1 }, { signal: probe.signal }),
    );

    expect(error.code).toBe('RETRY_EXHAUSTED');
    expect(hung.callCount).toBe(4);
    expect(probe.attached, '4 attempt timers came and went; none left a listener').toBe(0);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a blown total deadline leaves nothing behind', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const probe = probedSignal();
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } })],
      resilience: {
        retry: { maxAttempts: 4, strategy: 'fixed', baseDelayMs: 5 },
        timeout: { attemptTimeoutMs: 20, totalTimeoutMs: 70 },
      },
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('echo', { n: 1 }, { signal: probe.signal }),
    );

    expect(error.code).toBe('TIMEOUT');
    expect(probe.attached).toBe(0);
    expect(runtime.pendingTimers, 'the total-timeout timer was disposed in its finally').toBe(0);
    await layer.close();
  });

  it('a rate-limit queue wait detaches its abort listener on admission', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p' });
    const probe = probedSignal();
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } })],
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: { capacity: 1, refillPerSec: 1 },
        // Off, so `ctx.signal` IS the caller signal and the limiter's own
        // listener is the one the probe counts.
        timeout: false,
      },
      runtime,
    });

    await settle(runtime, layer.call('echo', { n: 1 }, { signal: probe.signal }));
    await settle(runtime, layer.call('echo', { n: 2 }, { signal: probe.signal }));

    expect(provider.at, 'the second call queued for a token').toEqual([0, 1_000]);
    expect(probe.adds, 'the limiter attached while queued').toBeGreaterThan(0);
    expect(probe.attached, 'and detached on admission').toBe(0);
    expect(runtime.pendingTimers, 'the head-of-queue timer is gone too').toBe(0);
    await layer.close();
  });
});
