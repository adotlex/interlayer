/**
 * THE TWO CLAIMS THE NESTING ORDER EXISTS TO MAKE TRUE (R4 §5.6):
 *
 *   Retry > RateLimit  ⇒ every RETRY spends a token — the limiter counts
 *                        PHYSICAL calls, so a retry storm is metered.
 *   Retry > Breaker    ⇒ every RETRY is recorded — a dead provider trips the
 *                        breaker `maxAttempts` times faster, not slower.
 *
 * Both are unverifiable from inside any single unit: the limiter unit never
 * meets the retry unit, and neither meets the breaker. They are only true
 * because of where `policyStack()` puts them.
 *
 * Two vehicles are used, deliberately:
 *
 *  - `buildComposition()` assembles the REAL policies through the REAL
 *    `policyStack()` and keeps each object reachable, so token counts and
 *    breaker windows can be read EXACTLY off `describe()`. `refillPerSec: 0`
 *    makes the bucket static, so those counts are integers with no drift.
 *  - `createLayer()` proves the same facts survive the facade, the router and
 *    the fallback chain.
 */

import { describe, expect, it } from 'vitest';
import { hasCode, TransportError } from '../../src/core/errors.ts';
import type { InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime, fakeProvider, testContext } from '../support/index.ts';
import {
  atVirtual,
  breakerView,
  buildComposition,
  drainMicrotasks,
  echoContract,
  rejectionOf,
  scripted,
  settle,
  tokensOf,
} from './harness.ts';

/** A static bucket: no refill, so every token count below is an exact integer. */
const STATIC_BUCKET = { capacity: 10, refillPerSec: 0, key: 'p' } as const;
/** Deterministic backoff — nothing here asserts on jitter. */
const FIXED_BACKOFF = { strategy: 'fixed', baseDelayMs: 1 } as const;

/* ------------------------------------------------------------------ *
 * 1. ORD-4 — retries pay for their own tokens
 * ------------------------------------------------------------------ */

describe('RateLimit inside Retry (ORD-4)', () => {
  it('maxAttempts 3 against a failing provider consumes exactly 3 tokens', async () => {
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: { maxAttempts: 3, ...FIXED_BACKOFF },
      rateLimit: STATIC_BUCKET,
      breaker: false,
      timeout: false,
    });

    const limiter = composition.limiter;
    if (limiter === undefined) throw new Error('limiter was not built');
    expect(tokensOf(limiter), 'the bucket starts FULL').toBe(10);

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(provider.callCount, 'three PHYSICAL calls').toBe(3);
    expect(tokensOf(limiter), '10 - 3: one token per physical try, not one per call').toBe(7);
    await composition.dispose();
  });

  it('the same failing call with retry OFF consumes exactly 1 token', async () => {
    // The control. If the limiter sat OUTSIDE retry it would be admitting
    // logical operations and this number would be 1 in both tests.
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      rateLimit: STATIC_BUCKET,
      breaker: false,
      timeout: false,
    });
    const limiter = composition.limiter;
    if (limiter === undefined) throw new Error('limiter was not built');

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(provider.callCount).toBe(1);
    expect(tokensOf(limiter)).toBe(9);
    await composition.dispose();
  });

  it('a retry that finds the bucket empty WAITS for a token before trying again', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        // Two tokens, refilling at one per second: the third physical try must
        // queue. A limiter that exempted retries would never throttle here.
        rateLimit: { capacity: 2, refillPerSec: 1 },
        breaker: false,
        timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    const throttled: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttled.push(e));

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    expect(dead.at, 'try 1 free, try 2 free, try 3 waits ~1 s for a refill').toEqual([
      0, 10, 1_000,
    ]);
    expect(throttled).toHaveLength(1);
    expect(throttled[0]?.key, 'one limiter per provider, keyed by provider id').toBe('dead');
    expect(throttled[0]?.waitMs, 'ceil((1 - 0.02) / 1 * 1000)').toBe(980);
    expect(throttled[0]?.at).toBe(20);

    expect(error.code).toBe('RETRY_EXHAUSTED');
    if (hasCode(error, 'RETRY_EXHAUSTED')) expect(error.attempts).toBe(3);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. ORD-5 — retries count toward the breaker
 * ------------------------------------------------------------------ */

describe('CircuitBreaker inside Retry (ORD-5)', () => {
  it('maxAttempts 3 against a failing provider records exactly 3 breaker failures', async () => {
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: { maxAttempts: 3, ...FIXED_BACKOFF },
      // A threshold high enough that the breaker never trips: this test counts,
      // it does not trip.
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 99 },
      rateLimit: false,
      timeout: false,
    });
    const breaker = composition.breaker;
    if (breaker === undefined) throw new Error('breaker was not built');

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    const view = breakerView(breaker, 'p');
    expect(provider.callCount).toBe(3);
    expect(view?.consecutiveFailures, 'three, not one — the loop did not hide them').toBe(3);
    expect(view?.state).toBe('closed');
    await composition.dispose();
  });

  it('fills a ratio breaker’s minimum-throughput window inside a SINGLE call', async () => {
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: { maxAttempts: 3, ...FIXED_BACKOFF },
      breaker: { mode: 'ratio', minimumThroughput: 3, failureRatio: 0.5 },
      rateLimit: false,
      timeout: false,
    });
    const breaker = composition.breaker;
    if (breaker === undefined) throw new Error('breaker was not built');

    await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    // total = 3 >= minimumThroughput, failures/total = 1.0 >= 0.5.
    expect(breakerView(breaker, 'p')?.state).toBe('open');
    await composition.dispose();
  });

  it('trips a consecutive breaker in ONE call that retry-off needs THREE calls to trip', async () => {
    const withRetry = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const retrying = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 },
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 3, resetMs: 60_000 },
        rateLimit: false,
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime: withRetry,
    });
    const trips: InterlayerEvents['breaker:transition'][] = [];
    retrying.on('breaker:transition', (e) => trips.push(e));

    await rejectionOf(withRetry, retrying.call('echo', { n: 1 }));

    expect(dead.callCount).toBe(3);
    expect(
      trips.map((t) => [t.from, t.to]),
      'tripped inside a single logical call',
    ).toEqual([['closed', 'open']]);
    expect(trips[0]?.failures).toBe(3);
    await retrying.close();

    /* -- the control: the identical breaker, with retry switched off -- */
    const noRetry = createFakeRuntime();
    const dead2 = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const plain = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead2.handler } }),
      ],
      resilience: {
        retry: false,
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 3, resetMs: 60_000 },
        rateLimit: false,
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime: noRetry,
    });
    const plainTrips: InterlayerEvents['breaker:transition'][] = [];
    plain.on('breaker:transition', (e) => plainTrips.push(e));

    await rejectionOf(noRetry, plain.call('echo', { n: 1 }));
    expect(plainTrips, 'one call, one failure — nowhere near the threshold').toHaveLength(0);
    await rejectionOf(noRetry, plain.call('echo', { n: 2 }));
    expect(plainTrips).toHaveLength(0);
    await rejectionOf(noRetry, plain.call('echo', { n: 3 }));
    expect(plainTrips, 'three CALLS to reach what retry reached in one').toHaveLength(1);
    expect(dead2.callCount).toBe(3);
    await plain.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. ORD-6 — a breaker that opens mid-loop
 * ------------------------------------------------------------------ */

describe('a breaker opening inside the retry loop (ORD-6)', () => {
  it('fails attempt 3 fast and STOPS the loop instead of exhausting maxAttempts', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 10 },
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 2, resetMs: 60_000 },
        rateLimit: false,
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('echo', { n: 1 }));

    // Attempts 1 and 2 reach the provider and trip it; attempt 3 is refused by
    // the breaker BEFORE the provider, and `CircuitOpenError` is non-retryable
    // (R4 §1.3 rule 2), so attempts 4 and 5 never happen.
    expect(dead.callCount, '2 physical calls out of a budget of 5').toBe(2);

    expect(error.code).toBe('RETRY_EXHAUSTED');
    if (hasCode(error, 'RETRY_EXHAUSTED')) {
      expect(error.attempts, 'two provider failures plus the circuit refusal').toBe(3);
      const last = error.errors.at(-1);
      expect(hasCode(last, 'CIRCUIT_OPEN'), 'what the loop actually stopped on').toBe(true);
      expect(error.cause, 'the cause chain reaches the refusal').toBe(last);
      if (hasCode(last, 'CIRCUIT_OPEN')) {
        expect(last.key).toBe('dead');
        // Opened at t=10 (attempt 2), refused at t=20, cooldown 60_000.
        expect(last.openedAt).toBe(10);
        expect(last.retryAfterMs).toBe(59_990);
      }
    }
    expect(runtime.now(), 'attempt 1 at 0, attempt 2 at 10, refusal at 20').toBe(20);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('still advances the fallback chain: the healthy sibling answers', async () => {
    const runtime = createFakeRuntime();
    const dead = scripted({ id: 'dead', failures: Number.POSITIVE_INFINITY });
    const healthy = scripted({ id: 'healthy' });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'dead', capabilities: { echo: dead.handler } }),
        defineProvider(echoContract, { id: 'healthy', capabilities: { echo: healthy.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 10 },
        breaker: { mode: 'consecutive', consecutiveFailureThreshold: 2, resetMs: 60_000 },
        rateLimit: false,
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('echo', { n: 4 }));

    expect(meta.providerId).toBe('healthy');
    expect(meta.value).toEqual({ n: 4, by: 'healthy' });
    expect(dead.callCount).toBe(2);
    // `RetryExhaustedError.retryable` is inherited from its last failure —
    // `CircuitOpenError`, which is retryable ELSEWHERE — so the chain advances.
    expect(meta.errors.map((e) => e.code)).toEqual(['RETRY_EXHAUSTED']);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. RateLimit is INSIDE the breaker — quota is only spent on real calls
 * ------------------------------------------------------------------ */

describe('RateLimit inside CircuitBreaker', () => {
  it('spends NO token to discover that the breaker is open', async () => {
    // CHANGED WITH `POLICY_ORDER`, deliberately. This test used to be called
    // "spends a token to discover that the breaker is open" and asserted 8
    // tokens left, on the grounds that the breaker must not hold a half-open
    // trial slot while a call sits in the limiter queue.
    //
    // That trade was the wrong way round. The limiter's own contract is that it
    // meters PHYSICAL calls and never refunds "because the physical call has
    // been made" — and for a refusal it has not been. Worse, in the default
    // `'wait'` exhaustion mode the fail-fast path SLEPT waiting for a token it
    // would never use, so a burst against an open circuit queued instead of
    // shedding, and a healthy sibling later in the fallback chain was delayed by
    // exactly the dead provider's queue time.
    //
    // The cost that remains — a half-open probe holding its slot while it waits
    // for a token — is bounded by `maxQueueWaitMs` and self-correcting. See
    // `POLICY_ORDER` in `src/core/policy.ts`.
    const provider = fakeProvider('p').alwaysFail(new TransportError('down'));
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      rateLimit: STATIC_BUCKET,
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
      timeout: false,
    });
    const limiter = composition.limiter;
    const breaker = composition.breaker;
    if (limiter === undefined || breaker === undefined) throw new Error('policies not built');

    const first = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );
    expect(first.code).toBe('TRANSPORT');
    expect(tokensOf(limiter)).toBe(9);
    expect(breakerView(breaker, 'p')?.state).toBe('open');

    const second = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(second.code).toBe('CIRCUIT_OPEN');
    expect(provider.callCount, 'the provider was never reached the second time').toBe(1);
    expect(tokensOf(limiter), 'and no quota was spent finding that out').toBe(9);
    await composition.dispose();
  });
});

/* ------------------------------------------------------------------ *
 * 5. HALF-OPEN meets the retry loop
 * ------------------------------------------------------------------ */

describe('half-open admission inside a retry loop', () => {
  it('spends exactly ONE probe per call, then refuses the rest of the loop', async () => {
    // The composition question no unit could ask: the breaker allows one trial
    // call at a time, and the thing calling it is a loop that wants three. The
    // loop must not be able to burn the whole cooldown's worth of probes.
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p', failures: 2 });
    const layer = createLayer({
      contract: echoContract,
      providers: [
        defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } }),
      ],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        rateLimit: false,
        breaker: {
          mode: 'consecutive',
          consecutiveFailureThreshold: 1,
          resetMs: 1_000,
          halfOpenMaxConcurrent: 1,
          halfOpenSuccessesToClose: 1,
        },
        timeout: { attemptTimeoutMs: 1_000, totalTimeoutMs: 60_000 },
      },
      runtime,
    });
    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    // Call A trips the breaker on attempt 1; attempt 2 is refused, attempt 3
    // never happens (CIRCUIT_OPEN is non-retryable).
    await rejectionOf(runtime, layer.call('echo', { n: 1 }));
    expect(provider.callCount).toBe(1);
    expect(runtime.now()).toBe(10);

    // Call B, after the cooldown: attempt 1 IS the probe, it fails, the breaker
    // re-opens with a fresh cooldown and attempt 2 is refused again.
    await runtime.advance(990);
    await rejectionOf(runtime, layer.call('echo', { n: 2 }));
    expect(provider.callCount, 'one probe, not three').toBe(2);

    // Call C, after the second cooldown: the probe succeeds and closes it.
    await runtime.advance(990);
    const reply = await settle(runtime, layer.call('echo', { n: 3 }));

    expect(reply).toEqual({ n: 3, by: 'p' });
    expect(provider.at, 'one physical call per cooldown window').toEqual([0, 1_000, 2_000]);
    expect(transitions.map((t) => [t.from, t.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'open'],
      ['open', 'half-open'],
      ['half-open', 'closed'],
    ]);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 6. An observation, not a rule: what the breaker makes of a CALL-scope
 *    deadline breach that happened to land inside an attempt.
 * ------------------------------------------------------------------ */

describe('what a total-timeout breach does to the breaker', () => {
  it('is NOT scored, exactly like a caller cancellation', async () => {
    // DECIDED, and changed with `isUnscored()`. This test used to be called
    // "is scored as a provider FAILURE, unlike a caller cancellation" and
    // asserted `'open'`, flagging the question to the orchestrator because
    // R4 §3.7 does not decide it either way. It is decided now, and the answer
    // is that a CALL-scope timeout is not evidence about the provider.
    //
    // `isUnscored()` discards `CancelledError` because a caller withdrawal says
    // nothing about provider health. A call-scope breach is the same kind of
    // thing wearing a different code: ONE caller's own budget — `totalTimeoutMs`
    // or a `deadlineAt` they supplied — running out across every provider and
    // retry the call happened to make. Scoring it meant that a caller with a
    // 50 ms deadline against a provider that reliably answers in 200 ms could
    // trip the breaker and hand fail-fast errors to everyone else, for a
    // provider that was never at fault.
    //
    // The ATTEMPT-scope timeout is still scored as a failure, and that
    // distinction is the whole point: `attemptTimeoutMs` is the budget EVERY
    // caller of that provider gets, so blowing it is real evidence. The test
    // below this one, and `an attempt timeout counts against the breaker` in
    // the breaker's own unit tests, pin that half.
    const provider = fakeProvider('p').alwaysSucceed('ok', 1_000);
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      rateLimit: false,
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
      timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 50 },
    });
    const breaker = composition.breaker;
    if (breaker === undefined) throw new Error('breaker was not built');

    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');

    // The breaker records on the unwind, which happens after the caller's
    // rejection has already been delivered.
    await drainMicrotasks();
    expect(
      breakerView(breaker, 'p')?.state,
      'the caller ran out of time; that is not evidence about the provider',
    ).toBe('closed');
    expect(breakerView(breaker, 'p')?.consecutiveFailures).toBe(0);
    await composition.dispose();
  });

  it('a caller cancellation at the same instant is NOT scored', async () => {
    const provider = fakeProvider('p').alwaysSucceed('ok', 1_000);
    const handle = testContext({ provider });
    const composition = buildComposition({
      retry: false,
      rateLimit: false,
      breaker: { mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 60_000 },
      timeout: false,
    });
    const breaker = composition.breaker;
    if (breaker === undefined) throw new Error('breaker was not built');

    atVirtual(handle.runtime, 50, () => {
      handle.abort();
    });
    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(error.code).toBe('CANCELLED');
    await drainMicrotasks();
    expect(
      breakerView(breaker, 'p')?.state,
      'the caller hung up; that is not evidence about the provider',
    ).toBe('closed');
    expect(breakerView(breaker, 'p')?.consecutiveFailures).toBe(0);
    await composition.dispose();
  });
});
