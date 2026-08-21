/**
 * What `createLayer({ contract, providers })` gives you when you configure
 * nothing — and what that costs.
 *
 * All four policies are ON by default. Three of them encode facts about THIS
 * library (how many attempts, how long to wait, when to give up on a backend).
 * The fourth, `rateLimit`, encodes a fact about SOMEONE ELSE — the provider's
 * quota — which this library cannot possibly know, and it is switched on at
 * 10 tokens refilling at 10/s per provider, in `'wait'` mode. This file is
 * where a user finds out.
 *
 * The last two tests are marked KNOWN DEFECT and FAIL on purpose.
 */

import { describe, expect, it } from 'vitest';
import {
  capability,
  createLayer,
  defineContract,
  defineProvider,
  hasCode,
  type InterlayerEvents,
  TransportError,
} from '../../src/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { counter, drainMicrotasks, neverSettles, rejectionOf, settle } from './harness.ts';

interface ChatRequest {
  readonly prompt: string;
}
interface ChatReply {
  readonly text: string;
  readonly by: string;
}

// `chat` is declared IDEMPOTENT here on purpose. `createLayer` now reads
// `capability({ idempotent })` and wires it into the retry policy's
// idempotency gate (R6 §2 rule 3: "the layer never retries a non-idempotent
// capability unless told to"), so a contract that declares `false` and a test
// that then asserts three attempts cannot both be right. This fixture used to
// say `false` only because nothing read the flag. Every assertion below is
// unchanged; the contract is what was wrong.
const ai = defineContract({
  chat: capability<ChatRequest, ChatReply>({ idempotent: true }),
});

function okProvider(id: string) {
  return defineProvider(ai, {
    id,
    capabilities: { chat: async (input) => ({ text: input.prompt, by: id }) },
  });
}

function deadProvider(id: string, calls?: { bump(): number }) {
  return defineProvider(ai, {
    id,
    capabilities: {
      chat: () => {
        calls?.bump();
        return Promise.reject(new TransportError(`${id} is down`));
      },
    },
  });
}

/* ------------------------------------------------------------------ *
 * 1. All four policies really are on
 * ------------------------------------------------------------------ */

describe('integration — the shipped defaults', () => {
  it('retries three times in total, unasked', async () => {
    const runtime = createFakeRuntime();
    const calls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [deadProvider('p', calls)],
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(calls.count, 'DEFAULTS.retry.maxAttempts is 3 TOTAL, not 3 extra').toBe(3);
    expect(error.code).toBe('RETRY_EXHAUSTED');
    await layer.close();
  });

  it('bounds one attempt at 10 s and the whole call at 30 s', async () => {
    const runtime = createFakeRuntime();
    const attemptOnly = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: { retry: false },
      runtime,
    });

    const attemptError = await rejectionOf(runtime, attemptOnly.call('chat', { prompt: 'x' }));
    expect(attemptError.code).toBe('TIMEOUT');
    if (hasCode(attemptError, 'TIMEOUT')) {
      expect(attemptError.scope).toBe('attempt');
      expect(attemptError.timeoutMs, 'DEFAULTS.timeout.attemptTimeoutMs').toBe(10_000);
    }
    expect(runtime.now()).toBe(10_000);
    await attemptOnly.close();

    // With the default retry loop back on, the attempts keep going until the
    // 30 s TOTAL budget stops them — the outermost policy in the stack.
    const full = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      runtime: full,
    });

    const error = await rejectionOf(full, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope, 'the call budget, not the attempt budget').toBe('call');
    }
    expect(full.now(), 'DEFAULTS.timeout.totalTimeoutMs').toBe(30_000);
    await drainMicrotasks();
    expect(full.pendingTimers).toBe(0);
    await layer.close();
  });

  it('trips the breaker only after minimumThroughput 10 requests', async () => {
    const runtime = createFakeRuntime();
    const calls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [deadProvider('p', calls)],
      // Retry and the limiter off so one call is exactly one physical request.
      resilience: { retry: false, rateLimit: false, timeout: false },
      runtime,
    });

    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    for (let i = 0; i < 9; i++) {
      const error = await rejectionOf(runtime, layer.call('chat', { prompt: String(i) }));
      expect(error.code, `call ${String(i)} still reaches the provider`).toBe('TRANSPORT');
    }
    expect(transitions, 'nine failures is below the throughput floor').toEqual([]);

    expect((await rejectionOf(runtime, layer.call('chat', { prompt: '9' }))).code).toBe(
      'TRANSPORT',
    );
    expect(transitions.map((t) => [t.from, t.to, t.failures])).toEqual([['closed', 'open', 10]]);
    expect(calls.count).toBe(10);

    expect((await rejectionOf(runtime, layer.call('chat', { prompt: '10' }))).code).toBe(
      'CIRCUIT_OPEN',
    );
    expect(calls.count, 'frozen: the eleventh never reached the provider').toBe(10);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. The rate-limit default, which is the one that surprises people
 * ------------------------------------------------------------------ */

describe('integration — the default rate limit (10 tokens, 10/s, per provider, wait)', () => {
  it('is invisible for the first ten calls and PACES the eleventh', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [okProvider('p')],
      resilience: { retry: false, breaker: false, timeout: false },
      runtime,
    });

    const throttles: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttles.push(e));

    for (let i = 0; i < 10; i++) {
      await settle(runtime, layer.call('chat', { prompt: String(i) }));
    }
    expect(runtime.now(), 'the bucket starts FULL, so a burst of 10 is free').toBe(0);
    expect(throttles).toEqual([]);

    await settle(runtime, layer.call('chat', { prompt: '10' }));

    // The eleventh call did not fail — it SLEPT. On a real clock that is 100 ms
    // of added latency on a request that had nothing wrong with it, caused by a
    // quota the library invented. This is the default worth arguing about: it
    // is the only one encoding an external fact.
    expect(runtime.now(), 'one token at 10/s = 100 ms of pacing').toBe(100);
    expect(throttles.map((e) => [e.key, e.waitMs])).toEqual([['p', 100]]);
    await layer.close();
  });

  it('meters each provider separately, so a fallback never spends the winner’s quota', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [okProvider('a'), okProvider('b')],
      resilience: { retry: false, breaker: false, timeout: false },
      runtime,
    });

    const throttles: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttles.push(e));

    for (let i = 0; i < 10; i++) {
      await settle(runtime, layer.only('a').call('chat', { prompt: String(i) }));
    }
    for (let i = 0; i < 10; i++) {
      await settle(runtime, layer.only('b').call('chat', { prompt: String(i) }));
    }

    expect(throttles, "b's bucket is untouched by a's traffic").toEqual([]);
    expect(runtime.now()).toBe(0);
    await layer.close();
  });

  it('a retry storm spends tokens: a retry is a physical call', async () => {
    const runtime = createFakeRuntime();
    const calls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [deadProvider('p', calls)],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 0 },
        breaker: false,
        timeout: false,
      },
      runtime,
    });

    const throttles: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttles.push(e));

    // Three calls x three attempts = nine of the ten tokens, all at t = 0.
    for (const prompt of ['1', '2', '3']) {
      await rejectionOf(runtime, layer.call('chat', { prompt }));
    }
    expect(calls.count).toBe(9);
    expect(throttles).toEqual([]);

    // The fourth call takes the last token, then has to buy the next two.
    await rejectionOf(runtime, layer.call('chat', { prompt: '4' }));
    expect(calls.count).toBe(12);
    expect(throttles.map((e) => e.waitMs)).toEqual([100, 100]);
    expect(runtime.now(), 'a failing provider is now retried at the refill rate').toBe(200);
    await layer.close();
  });

  it("'reject' mode turns the same situation into a typed RATE_LIMITED error", async () => {
    // The alternative a user should reach for when pacing is the wrong answer:
    // fail loudly at the eleventh call instead of silently adding latency.
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [okProvider('p')],
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        rateLimit: { onExhaustion: 'reject' },
      },
      runtime,
    });

    for (let i = 0; i < 10; i++) {
      await settle(runtime, layer.call('chat', { prompt: String(i) }));
    }
    const error = await rejectionOf(runtime, layer.call('chat', { prompt: '10' }));

    expect(error.code).toBe('RATE_LIMITED');
    if (hasCode(error, 'RATE_LIMITED')) {
      expect(error.retryAfterMs, 'a usable Retry-After, not a bare refusal').toBe(100);
    }
    expect(runtime.now(), 'nothing waited').toBe(0);
    await layer.close();
  });

  it('rateLimit: false removes the pacing entirely', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [okProvider('p')],
      resilience: { retry: false, breaker: false, timeout: false, rateLimit: false },
      runtime,
    });

    const throttles: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttles.push(e));

    for (let i = 0; i < 25; i++) {
      await settle(runtime, layer.call('chat', { prompt: String(i) }));
    }
    expect(throttles).toEqual([]);
    expect(runtime.now()).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Two defects these tests were written to report, now FIXED
 * ------------------------------------------------------------------ */

describe('integration — two defects in the assembled defaults', () => {
  it('an open breaker fails fast WITHOUT buying a rate-limit token', async () => {
    // `POLICY_ORDER` used to put RateLimit OUTSIDE CircuitBreaker, so every call
    // paid for a token before the breaker was even asked. Two consequences, and
    // the second one was the bug:
    //
    //   1. A call the breaker rejects still DRAINED the provider's quota, even
    //      though no physical request was ever made. The limiter's own comment
    //      says tokens are never refunded because "the physical call has been
    //      made" — here it has not.
    //   2. Once that quota was exhausted, the fail-fast path WAITED. In `'wait'`
    //      mode — the default — a CIRCUIT_OPEN arrived a full refill interval
    //      late. A circuit breaker that sleeps before failing fast has lost the
    //      only property it exists for: shedding load instantly. Under the
    //      shipped defaults that was 100 ms late per call; with a quota-shaped
    //      limiter (as configured here) a full second, and a queue of such calls
    //      could burn the entire 30 s total budget.
    //
    // Fixed by `Retry > CircuitBreaker > RateLimit`; see `POLICY_ORDER`.
    const runtime = createFakeRuntime();
    const calls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [deadProvider('p', calls)],
      resilience: {
        retry: false,
        timeout: false,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 60_000 },
        rateLimit: { capacity: 2, refillPerSec: 1 },
      },
      runtime,
    });

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect(calls.count, 'two physical calls tripped the breaker').toBe(2);
    expect(runtime.now(), 'both were paid for out of the initial burst').toBe(0);

    const open = await rejectionOf(runtime, layer.call('chat', { prompt: '3' }));
    expect(open.code).toBe('CIRCUIT_OPEN');
    expect(calls.count, 'the provider was never touched').toBe(2);
    expect(runtime.now(), 'a fail-fast must not wait for a token it does not need to spend').toBe(
      0,
    );
    await layer.close();
  });

  it('`capability({ idempotent: false })` stops retries', async () => {
    // `CapabilityMeta.idempotent` is documented as "Safe to re-issue. Retry and
    // routing read this". Retry did NOT read it: `createLayer` built one retry
    // policy from `resilience.retry` alone and never consulted
    // `capabilityMeta(contract, name)`, so `retryPolicy`'s `idempotent` option
    // kept its default of `true` for every capability in the contract.
    //
    // R6 §2 rule 3 states the intended behaviour outright: "the layer never
    // retries a non-idempotent capability unless told to."
    //
    // The scenario below is the exact case `passesIdempotencyGate` exists to
    // refuse — a NON-IDEMPOTENT write whose response never arrived. The gate's
    // rule for it is unconditional ("NEVER on a timeout: the write may have
    // succeeded invisibly"). Fixed by giving each capability its own retry
    // instance in `createLayer`; the charge is now issued once.
    const runtime = createFakeRuntime();
    const charges = counter();
    const payments = defineContract({
      charge: capability<{ amountCents: number }, { receiptId: string }>({ idempotent: false }),
    });
    const gateway = defineProvider(payments, {
      id: 'gateway',
      capabilities: {
        charge: async (_input, ctx) => {
          charges.bump();
          // The gateway accepted the charge; the response never comes back.
          await ctx.runtime.sleep(1_000, ctx.signal);
          return { receiptId: 'r-1' };
        },
      },
    });
    const layer = createLayer({
      contract: payments,
      providers: [gateway],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 10_000 },
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    await rejectionOf(runtime, layer.call('charge', { amountCents: 4_999 }));

    expect(charges.count, 'a non-idempotent capability must be issued at most once').toBe(1);
    await layer.close();
  });

  it('and stops them on an ordinary transient failure too, not only on a timeout', async () => {
    // The gate is not timeout-specific: a non-idempotent capability retries
    // ONLY on the connect-level failures the request provably never survived
    // (`retryNonIdempotentOnConnectFailure`). A `TransportError` is not one of
    // those — the request may well have reached the gateway — so the write is
    // issued once. Pinned here because the shared `ai` contract in this file
    // declares `chat` idempotent, so nothing else exercises the general rule
    // through the facade.
    const runtime = createFakeRuntime();
    const charges = counter();
    const payments = defineContract({
      charge: capability<{ amountCents: number }, { receiptId: string }>({ idempotent: false }),
      quote: capability<{ amountCents: number }, { feeCents: number }>({ idempotent: true }),
    });
    const quotes = counter();
    const gateway = defineProvider(payments, {
      id: 'gateway',
      capabilities: {
        charge: () => {
          charges.bump();
          return Promise.reject(new TransportError('gateway unreachable'));
        },
        quote: () => {
          quotes.bump();
          return Promise.reject(new TransportError('gateway unreachable'));
        },
      },
    });
    const layer = createLayer({
      contract: payments,
      providers: [gateway],
      resilience: {
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 10 },
        timeout: false,
        breaker: false,
        rateLimit: false,
      },
      runtime,
    });

    await rejectionOf(runtime, layer.call('charge', { amountCents: 4_999 }));
    expect(charges.count, 'non-idempotent: issued once').toBe(1);

    // The control, on the SAME layer and the same retry configuration: an
    // idempotent capability in the same contract still gets all three tries, so
    // the gate is keyed on the capability and not on the layer.
    await rejectionOf(runtime, layer.call('quote', { amountCents: 4_999 }));
    expect(quotes.count, 'idempotent: the full retry budget').toBe(3);
    await layer.close();
  });
});
