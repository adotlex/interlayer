/**
 * WHERE THE RATE-LIMIT QUEUE WAIT IS SPENT (ORD-7, ORD-8, ORD-9).
 *
 * ```
 *   TotalTimeout > Retry > [ RateLimit ] > Breaker > AttemptTimeout > call
 *                            ^ the queue wait happens HERE
 * ```
 *
 * Two consequences, and they pull in opposite directions:
 *
 *   the wait is INSIDE the total timeout  ⇒ queueing burns the caller's budget,
 *                                           and a call that expires while
 *                                           queued must leave without paying;
 *   the wait is OUTSIDE the attempt one   ⇒ the per-attempt clock starts at
 *                                           ADMISSION, so a long queue never
 *                                           makes a fast provider look slow.
 *
 * The bucket arithmetic here is fractional and IEEE-754 exact-but-ugly (the
 * token-bucket header says so). Every instant below was therefore derived from
 * the published refill formula and is asserted as an exact virtual instant;
 * token counts are compared with a tolerance, never with `===`.
 */

import { describe, expect, it } from 'vitest';
import { hasCode } from '../../src/core/errors.ts';
import type { InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime, fakeProvider, testContext } from '../support/index.ts';
import {
  atVirtual,
  buildComposition,
  echoContract,
  queueDepthOf,
  rejectionOf,
  scripted,
  settle,
  tokensOf,
} from './harness.ts';

/** `Math.abs(actual - expected) < 1e-9` — the tolerance the bucket demands. */
function expectTokens(actual: number, expected: number): void {
  expect(
    Math.abs(actual - expected) < 1e-9,
    `expected ~${String(expected)} tokens, got ${String(actual)}`,
  ).toBe(true);
}

/* ------------------------------------------------------------------ *
 * 1. ORD-7 — the attempt timeout starts AFTER admission
 * ------------------------------------------------------------------ */

describe('AttemptTimeout inside RateLimit (ORD-7)', () => {
  it('a 900 ms queue wait does not blow a 200 ms attempt timeout', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p', delayMs: 100 });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } })],
      resilience: {
        retry: false,
        breaker: false,
        // One token, refilling at 1/s: the second call must queue ~900 ms.
        rateLimit: { capacity: 1, refillPerSec: 1 },
        timeout: { attemptTimeoutMs: 200, totalTimeoutMs: 60_000 },
      },
      runtime,
    });

    const first = await settle(runtime, layer.call('echo', { n: 1 }));
    expect(first).toEqual({ n: 1, by: 'p' });
    expect(runtime.now()).toBe(100);

    const second = await settle(runtime, layer.call('echo', { n: 2 }));

    expect(second, 'the queue wait was 4.5x the attempt timeout and nothing fired').toEqual({
      n: 2,
      by: 'p',
    });
    expect(provider.at, 'admitted at t=1000, then 100 ms of provider time').toEqual([0, 1_000]);
    expect(runtime.now()).toBe(1_100);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('arms the attempt timer at admission, not at entry to the limiter', async () => {
    // The sharp version: the provider is slower than the attempt timeout, so a
    // timer really does fire. WHEN it fires says where it was armed.
    const runtime = createFakeRuntime();
    const provider = fakeProvider('p').succeed('first', 1, 100).succeed('second', 1, 250);
    const handle = testContext({ runtime, provider });
    const composition = buildComposition({
      retry: false,
      breaker: false,
      rateLimit: { capacity: 1, refillPerSec: 1, key: 'p' },
      timeout: { attemptTimeoutMs: 200, totalTimeoutMs: 60_000 },
    });

    await settle(handle.runtime, composition.run(handle.call, handle.provider, provider.handler()));
    expect(handle.runtime.now()).toBe(100);

    const error = await rejectionOf(
      handle.runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope).toBe('attempt');
      expect(error.timeoutMs, 'the full 200 ms, un-eroded by 900 ms of queueing').toBe(200);
    }
    expect(
      handle.runtime.now(),
      'admitted at 1000 + 200 ms of attempt budget; NOT 100 + 200 = 300',
    ).toBe(1_200);
    await composition.dispose();
  });
});

/* ------------------------------------------------------------------ *
 * 2. ORD-8 / ORD-9 — the queue wait is inside the total budget
 * ------------------------------------------------------------------ */

describe('RateLimit inside TotalTimeout (ORD-8, ORD-9)', () => {
  it('kills a queued call at the total deadline, unqueues it, and spends no token', async () => {
    const runtime = createFakeRuntime();
    const provider = fakeProvider('p').alwaysSucceed('ok', 100);
    // Two contexts on ONE clock and ONE limiter: a 400 ms budget for the calls
    // under test, and a generous one for the probe that follows them.
    const tight = testContext({ runtime, provider });
    const generous = testContext({ runtime, provider, hints: { timeoutMs: 60_000 } });
    const composition = buildComposition({
      retry: false,
      breaker: false,
      rateLimit: { capacity: 1, refillPerSec: 1, key: 'p' },
      timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 400 },
    });
    const limiter = composition.limiter;
    if (limiter === undefined) throw new Error('limiter was not built');

    // 1. Drains the only token.
    await settle(runtime, composition.run(tight.call, tight.provider, provider.handler()));
    expect(runtime.now()).toBe(100);
    expectTokens(tokensOf(limiter), 0);

    // 2. Queues for ~900 ms against a 400 ms budget. The budget wins.
    const error = await rejectionOf(
      runtime,
      composition.run(tight.call, tight.provider, provider.handler()),
    );

    expect(error.code, 'the wait counted against the CALL budget').toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope).toBe('call');
      expect(error.timeoutMs).toBe(400);
    }
    expect(runtime.now(), '100 + 400, not the 1000 the queue would have taken').toBe(500);
    expect(provider.callCount, 'it never reached the provider').toBe(1);

    // ORD-9: removed from the queue, and NOT charged for the wait.
    expect(queueDepthOf(limiter), 'the waiter was spliced out on abort').toBe(0);
    expectTokens(tokensOf(limiter), 0.1); // refilled to t=100 and left alone
    expect(runtime.pendingTimers, 'the head-of-queue timer was disarmed too').toBe(0);

    // The behavioural half of the same claim: the un-spent token matures on
    // schedule. Had the abort consumed one, the bucket would be at -0.9 and
    // this probe would not be admitted until t=2000.
    const value = await settle(
      runtime,
      composition.run(generous.call, generous.provider, provider.handler()),
    );
    expect(value).toBe('ok');
    expect(provider.calls[1]?.at, 'admitted the instant the first token matured').toBe(1_000);
    await composition.dispose();
  });

  it('surfaces the same TIMEOUT through the facade, with the queue wait counted', async () => {
    const runtime = createFakeRuntime();
    const provider = scripted({ id: 'p', delayMs: 100 });
    const layer = createLayer({
      contract: echoContract,
      providers: [defineProvider(echoContract, { id: 'p', capabilities: { echo: provider.handler } })],
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: { capacity: 1, refillPerSec: 1 },
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 400 },
      },
      runtime,
    });

    const throttled: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (e) => throttled.push(e));

    await settle(runtime, layer.call('echo', { n: 1 }));
    const error = await rejectionOf(runtime, layer.call('echo', { n: 2 }));

    expect(throttled.map((t) => t.waitMs), 'priced at ceil((1 - 0.1) / 1 * 1000)').toEqual([900]);
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');
    expect(runtime.now()).toBe(500);
    expect(provider.callCount).toBe(1);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a caller abort while queued also leaves without paying', async () => {
    const runtime = createFakeRuntime();
    const provider = fakeProvider('p').alwaysSucceed('ok', 100);
    const handle = testContext({ runtime, provider });
    const composition = buildComposition({
      retry: false,
      breaker: false,
      rateLimit: { capacity: 1, refillPerSec: 1, key: 'p' },
      timeout: false,
    });
    const limiter = composition.limiter;
    if (limiter === undefined) throw new Error('limiter was not built');

    await settle(runtime, composition.run(handle.call, handle.provider, provider.handler()));
    expect(runtime.now()).toBe(100);
    // Relative to NOW, i.e. 200 ms into a ~900 ms queue wait.
    atVirtual(runtime, 200, () => {
      handle.abort();
    });

    const error = await rejectionOf(
      runtime,
      composition.run(handle.call, handle.provider, provider.handler()),
    );

    expect(error.code, 'a caller withdrawal, not a limiter rejection').toBe('CANCELLED');
    expect(runtime.now()).toBe(300);
    expect(queueDepthOf(limiter)).toBe(0);
    expectTokens(tokensOf(limiter), 0.1);
    expect(runtime.pendingTimers).toBe(0);
    await composition.dispose();
  });
});
