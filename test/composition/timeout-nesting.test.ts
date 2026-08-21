/**
 * TIMEOUT IS BOTH PER-ATTEMPT AND OVERALL — the outermost and innermost halves
 * of the canonical order, tested against each other.
 *
 * ```
 *   TotalTimeout > Retry > RateLimit > Breaker > [ AttemptTimeout ] > call
 *   ^ bounds the whole logical operation          ^ bounds ONE physical try
 * ```
 *
 * The four consequences proved here, all of which follow from the POSITIONS and
 * from nothing else:
 *
 *  - an attempt timeout kills one try and the loop continues       (ORD-1)
 *  - the retry backoff sleep is NOT inside the attempt timeout ...
 *  - ... but it IS inside the total timeout
 *  - a call cannot outlive its budget however many providers it walks (ORD-2)
 *
 * Every instant below is an exact virtual instant. No test sleeps.
 */

import { describe, expect, it } from 'vitest';
import { hasCode } from '../../src/core/errors.ts';
import type { CallContext } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { atVirtual, echoContract, rejectionOf, scripted, settle } from './harness.ts';

/* ------------------------------------------------------------------ *
 * 1. ORD-1 — the attempt timeout is INSIDE the retry loop
 * ------------------------------------------------------------------ */

describe('AttemptTimeout inside Retry (ORD-1)', () => {
  it('kills one hung try and the loop then makes the next one', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 10_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    // THREE physical tries, each cut off at 50 ms, 10 ms of backoff between.
    // A timeout OUTSIDE the loop could only ever produce one.
    expect(hung.at, 'attempt starts: 0, 50+10, 110+10').toEqual([0, 60, 120]);
    expect(hung.calls.map((c) => c.attempt)).toEqual([1, 2, 3]);
    expect(runtime.now(), 'the third attempt timed out at 120+50').toBe(170);

    expect(error.code).toBe('RETRY_EXHAUSTED');
    if (hasCode(error, 'RETRY_EXHAUSTED')) {
      expect(error.attempts).toBe(3);
      const codes = error.errors.map((e) => (hasCode(e, 'TIMEOUT') ? e.scope : 'other'));
      expect(codes, 'every one of them is an ATTEMPT timeout').toEqual([
        'attempt',
        'attempt',
        'attempt',
      ]);
    }
    expect(runtime.pendingTimers, 'each attempt timer was cleared in its finally').toBe(0);
    await layer.close();
  });

  it('a per-attempt bound alone cannot bound the call: 3 x 50 ms of budget is spent', async () => {
    // The complement of the test above, and the reason R4 insists on BOTH
    // timeouts: with only the attempt timeout, the caller waits
    // maxAttempts x attemptTimeout + backoff and nothing stops it.
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        // A total budget far larger than the loop can spend.
        timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 1_000_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(runtime.now(), '3 x 50 ms of attempts + 2 x 10 ms of backoff').toBe(170);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. The backoff sleep sits BETWEEN the two timeouts
 * ------------------------------------------------------------------ */

describe('the retry backoff sleep', () => {
  it('is NOT bounded by the attempt timeout (retry is outside it)', async () => {
    const runtime = createFakeRuntime();
    // The backoff is 25x the attempt timeout. If the sleep happened inside the
    // attempt-timeout policy it would be cut off at 20 ms and the call would
    // fail; the loop is outside, so the sleep runs to completion.
    const flaky = scripted({ id: 'flaky', failures: 1 });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'flaky', capabilities: { echo: flaky.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 500 },
        timeout: { attemptTimeoutMs: 20, totalTimeoutMs: 10_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const reply = await settle(runtime, layer.call('echo', { n: 7 }));

    expect(reply).toEqual({ n: 7, by: 'flaky' });
    expect(runtime.slept, 'the full 500 ms backoff was slept').toEqual([500]);
    expect(flaky.at, 'the retry landed at t=500, long past the 20 ms attempt bound').toEqual([
      0, 500,
    ]);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('IS bounded by the total timeout (retry is inside it)', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
      ],
      resilience: {
        // An explicit `budgetMs` disables retry's own cooperative
        // "do not sleep into a deadline I cannot meet" check, so the total
        // timeout is left to enforce the bound PRE-EMPTIVELY. That is the thing
        // under test: the outermost policy wins even mid-sleep.
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 500, budgetMs: 1_000_000 },
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 300 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope, 'the CALL-scope budget, not an attempt one').toBe('call');
      expect(error.timeoutMs).toBe(300);
    }
    expect(runtime.now(), 'cut off mid-backoff at the total deadline').toBe(300);
    expect(dead.callCount, 'the second attempt never happened').toBe(1);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. ORD-2 — the total budget is shared by every retry and every provider
 * ------------------------------------------------------------------ */

describe('TotalTimeout outermost (ORD-2)', () => {
  it('rejects at the total deadline mid-retry when maxAttempts x attemptTimeout exceeds it', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const spare = scripted({ id: 'spare', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } }),
        defineProvider(echoContract, { id: 'spare', capabilities: { echo: spare.handler } }),
      ],
      resilience: {
        // 5 x 30 ms = 150 ms of attempts alone, against a 100 ms total budget.
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 10 },
        timeout: { attemptTimeoutMs: 30, totalTimeoutMs: 100 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');
    expect(runtime.now(), 'the deadline, not 5 x 30 + backoff').toBe(100);
    expect(hung.at, 'three tries fitted in the budget; the fourth never started').toEqual([
      0, 40, 80,
    ]);
    expect(spare.callCount, 'a blown deadline does not walk the fallback chain').toBe(0);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('bounds the whole call across providers: five candidates, one budget', async () => {
    const runtime = createFakeRuntime();
    const p1 = scripted({ id: 'p1', hang: true });
    const p2 = scripted({ id: 'p2', hang: true });
    const p3 = scripted({ id: 'p3', hang: true });
    const p4 = scripted({ id: 'p4', hang: true });
    const p5 = scripted({ id: 'p5', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'p1', capabilities: { echo: p1.handler } }),
        defineProvider(echoContract, { id: 'p2', capabilities: { echo: p2.handler } }),
        defineProvider(echoContract, { id: 'p3', capabilities: { echo: p3.handler } }),
        defineProvider(echoContract, { id: 'p4', capabilities: { echo: p4.handler } }),
        defineProvider(echoContract, { id: 'p5', capabilities: { echo: p5.handler } }),
      ],
      resilience: {
        retry: false,
        timeout: { attemptTimeoutMs: 40, totalTimeoutMs: 100 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');
    expect(runtime.now(), 'not 5 x 40 = 200').toBe(100);
    expect(
      [p1, p2, p3, p4, p5].map((p) => p.callCount),
      'p1 0-40, p2 40-80, p3 80-100 (clamped); p4 and p5 never started',
    ).toEqual([1, 1, 1, 0, 0]);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('clamps an attempt timeout that is larger than the whole remaining budget', async () => {
    const runtime = createFakeRuntime();
    const hung = scripted({ id: 'hung', hang: true });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'hung', capabilities: { echo: hung.handler } }),
      ],
      // Inverted on purpose: attempt >> total. `composeDeadline` clamps rather
      // than throwing, and the CALL-scope error is what surfaces.
      resilience: {
        retry: false,
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 250 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope, 'the outer policy wins the tie it created by clamping').toBe('call');
      expect(error.timeoutMs).toBe(250);
    }
    expect(runtime.now()).toBe(250);
    await layer.close();
  });

  it('runs user middleware INSIDE the total timeout, with the deadline already armed', async () => {
    const runtime = createFakeRuntime({ startTime: 1_000 });
    const ok = scripted({ id: 'ok' });
    const seen: CallContext[] = [];
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'ok', capabilities: { echo: ok.handler } })],
      resilience: {
        retry: false,
        timeout: { attemptTimeoutMs: 100, totalTimeoutMs: 500 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });
    layer.use((ctx, next) => {
      seen.push(ctx);
      return next();
    });

    await settle(runtime, layer.call('echo', { n: 1 }));

    // `kind: 'custom'` sorts AFTER `total-timeout`, so by the time middleware
    // runs the deadline exists. If the middleware ran first this would be
    // `undefined` — which is exactly the bug the ordering prevents.
    expect(seen).toHaveLength(1);
    expect(seen[0]?.deadlineAt, 'startedAt + totalTimeoutMs, as an absolute instant').toBe(1_500);
    expect(seen[0]?.signal.aborted).toBe(false);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. ORD-3 — abort mid-backoff propagates out unwrapped
 * ------------------------------------------------------------------ */

describe('cancellation through the whole stack (ORD-3)', () => {
  it('an abort during the backoff surfaces as CANCELLED, unwrapped', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const controller = new AbortController();
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 100 },
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 10_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    // Halfway through the 100 ms backoff, on the virtual clock.
    atVirtual(runtime, 50, () => {
      controller.abort();
    });

    const error = await rejectionOf(
      runtime,
      layer.call('echo', { n: 1 }, { signal: controller.signal }),
    );

    expect(error.code, 'not RETRY_EXHAUSTED, not ALL_FAILED — the caller withdrew').toBe(
      'CANCELLED',
    );
    expect(runtime.now()).toBe(50);
    expect(dead.callCount, 'the second attempt never started').toBe(1);
    expect(runtime.pendingTimers, 'the backoff timer was cleared, not left to fire at 100').toBe(0);
    await layer.close();
  });

  it('an abort while a provider call is in flight surfaces as CANCELLED too', async () => {
    const runtime = createFakeRuntime();
    const slow = scripted({ id: 'slow', delayMs: 400 });
    const controller = new AbortController();
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'slow', capabilities: { echo: slow.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 10_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    atVirtual(runtime, 120, () => {
      controller.abort();
    });
    const error = await rejectionOf(
      runtime,
      layer.call('echo', { n: 1 }, { signal: controller.signal }),
    );

    expect(error.code).toBe('CANCELLED');
    expect(runtime.now()).toBe(120);
    expect(slow.callCount).toBe(1);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});
