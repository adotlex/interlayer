import { describe, expect, it } from 'vitest';
import {
  assertValidBucketConfig,
  consume,
  createTokenBucket,
  DEFAULT_COST,
  isSatisfiable,
  msUntil,
  refill,
  type TokenBucketState,
  waitMsFor,
} from './token-bucket.ts';

/**
 * R4 §4.2 / checklist RL-6: fractional accrual accumulates IEEE-754 error of
 * ~1.7e-13. Every token-count assertion in this file goes through this helper.
 * `expect(state.tokens).toBe(n)` is BANNED — it produces a failure that looks
 * flaky and is not a bug.
 *
 * Note the asymmetry, which is deliberate: `waitMs` values ARE compared
 * exactly, because `waitMsFor` rounds up to a whole millisecond and so returns
 * an exact integer.
 */
const TOLERANCE = 1e-9;

function expectTokens(actual: number, expected: number): void {
  expect(Math.abs(actual - expected)).toBeLessThan(TOLERANCE);
}

function bucket(capacity: number, refillPerSec: number, now = 0): TokenBucketState {
  return createTokenBucket({ capacity, refillPerSec }, now);
}

function drained(capacity: number, refillPerSec: number, now = 0): TokenBucketState {
  return { ...bucket(capacity, refillPerSec, now), tokens: 0 };
}

describe('token bucket — burst and exhaustion', () => {
  it('starts FULL: capacity 5 grants exactly 5 immediate acquisitions at t=0 (RL-1)', () => {
    let state = bucket(5, 10);
    expectTokens(state.tokens, 5);
    for (let i = 0; i < 5; i++) {
      const outcome = consume(state, 0, 1);
      expect(outcome.allowed).toBe(true);
      expect(outcome.waitMs).toBe(0);
      state = outcome.state;
    }
    expectTokens(state.tokens, 0);
  });

  it('refuses the 6th acquisition at t=0 and prices the wait (RL-2)', () => {
    const state = { ...bucket(5, 10), tokens: 0 };
    const outcome = consume(state, 0, 1);
    expect(outcome.allowed).toBe(false);
    expect(outcome.waitMs).toBe(100);
    expectTokens(outcome.state.tokens, 0); // a refusal never deducts
  });

  it('msUntil(1) on an empty 10 tok/s bucket is exactly 100 (RL-3)', () => {
    expect(msUntil(drained(10, 10), 0, 1)).toBe(100);
  });

  it('after exactly 100ms one token is available and a second is not (RL-4)', () => {
    const first = consume(drained(10, 10), 100, 1);
    expect(first.allowed).toBe(true);
    expectTokens(first.state.tokens, 0);
    expect(consume(first.state, 100, 1).allowed).toBe(false);
  });

  it('grants at the exact boundary — `>=`, not `>` (RL-5)', () => {
    const state: TokenBucketState = { capacity: 10, refillPerSec: 10, tokens: 1, last: 0 };
    const outcome = consume(state, 0, 1);
    expect(outcome.allowed).toBe(true);
    expectTokens(outcome.state.tokens, 0);
    expect(waitMsFor(state, 1)).toBe(0);
  });

  it('rounds the wait UP so a waiter is never woken early (334, not 333)', () => {
    // 1 token at 3 tok/s is 333.33ms; ceil is what stops a wake-requeue busy loop.
    expect(msUntil(drained(10, 3), 0, 1)).toBe(334);
    expect(consume(drained(10, 3), 333, 1).allowed).toBe(false);
    expect(consume(drained(10, 3), 334, 1).allowed).toBe(true);
  });
});

describe('token bucket — accrual and drift', () => {
  it('1000 successive 1ms ticks accrue 10 tokens with NO systematic drift (RL-6)', () => {
    let state = drained(10, 10);
    for (let t = 1; t <= 1000; t++) state = refill(state, t);

    // The whole point of RL-6: this is 9.999999999999831, not 10.
    expectTokens(state.tokens, 10);
    expect(state.tokens).not.toBe(10);
    expect(Math.abs(state.tokens - 10)).toBeLessThan(1e-12);
    expect(state.last).toBe(1000);

    // Same elapsed time in one step is exact — the difference IS the drift, and
    // it is bounded rather than systematic.
    const oneStep = refill(drained(10, 10), 1000);
    expect(oneStep.tokens).toBe(10);
    expectTokens(state.tokens, oneStep.tokens);
  });

  it('accrues fractionally rather than in whole-token steps', () => {
    // A whole-token design would report 0 here forever and deliver ~0 tok/s.
    const state = refill(drained(10, 10), 10);
    expectTokens(state.tokens, 0.1);
    expectTokens(refill(state, 20).tokens, 0.2);
  });

  it('never exceeds capacity however far the clock advances (RL-7)', () => {
    expectTokens(refill(drained(10, 10), 1_000_000).tokens, 10);
    expectTokens(refill(bucket(10, 10), 1e12).tokens, 10);
  });

  it('clamps a far-forward clock jump at one full burst (RL-11)', () => {
    const jumped = refill(drained(4, 100), 60 * 60 * 1000);
    expectTokens(jumped.tokens, 4);
    expect(jumped.last).toBe(3_600_000);
  });

  it('mints nothing on a backwards clock and recovers on the next forward tick (RL-10)', () => {
    const start = refill(drained(10, 10), 1000);
    expectTokens(start.tokens, 10);
    const spent = consume(start, 1000, 10).state;
    expectTokens(spent.tokens, 0);

    const rewound = refill(spent, 500); // NTP step backwards
    expectTokens(rewound.tokens, 0);
    expect(rewound.last).toBe(500); // `last` is still moved to the observed reading

    const recovered = refill(rewound, 600);
    expectTokens(recovered.tokens, 1);
  });

  it('advances `last` even when the rate is 0, so no error accumulates', () => {
    const state = refill(drained(10, 0), 5000);
    expectTokens(state.tokens, 0);
    expect(state.last).toBe(5000);
  });

  it('is pure — the input state is never mutated', () => {
    const start = bucket(5, 10);
    const snapshot = { ...start };
    consume(start, 500, 3);
    refill(start, 500);
    msUntil(start, 500, 3);
    expect(start).toEqual(snapshot);
  });
});

describe('token bucket — degenerate configurations', () => {
  it('capacity 0 refuses every acquisition and is never satisfiable (RL-8)', () => {
    const state = bucket(0, 10);
    expectTokens(state.tokens, 0);
    const outcome = consume(state, 10_000, 1);
    expect(outcome.allowed).toBe(false);
    expect(isSatisfiable(state, 1)).toBe(false);
    expect(isSatisfiable(state, 0)).toBe(true);
  });

  it('refillPerSec 0 never refills and reports an infinite wait (RL-9)', () => {
    const state = drained(10, 0);
    expect(msUntil(state, 10_000, 1)).toBe(Number.POSITIVE_INFINITY);
    expect(consume(state, 10_000, 1).allowed).toBe(false);
    // ...but a bucket that starts full still grants its initial burst.
    const full = bucket(2, 0);
    const first = consume(full, 0, 1);
    expect(first.allowed).toBe(true);
    expect(consume(consume(first.state, 5000, 1).state, 10_000, 1).allowed).toBe(false);
  });

  it('reports a cost above capacity as unsatisfiable (RL-12)', () => {
    expect(isSatisfiable(bucket(5, 10), 6)).toBe(false);
    expect(isSatisfiable(bucket(5, 10), 5)).toBe(true);
    // The closed form still answers honestly, which is what prices a queue.
    expect(waitMsFor(drained(5, 10), 8)).toBe(800);
  });

  it('honours a fractional cost (RL-13)', () => {
    const outcome = consume({ capacity: 1, refillPerSec: 10, tokens: 0.5, last: 0 }, 0, 0.5);
    expect(outcome.allowed).toBe(true);
    expectTokens(outcome.state.tokens, 0);
    expect(msUntil(drained(1, 10), 0, 0.5)).toBe(50);
    expectTokens(refill(drained(1, 10), 50).tokens, 0.5);
  });

  it('defaults the cost to one token', () => {
    expect(DEFAULT_COST).toBe(1);
    expect(consume(drained(10, 10), 100).allowed).toBe(true);
    expect(msUntil(drained(10, 10), 0)).toBe(100);
  });

  it('rejects an impossible configuration with RangeError', () => {
    expect(() => assertValidBucketConfig({ capacity: -1, refillPerSec: 10 })).toThrow(RangeError);
    expect(() => assertValidBucketConfig({ capacity: Number.NaN, refillPerSec: 10 })).toThrow(
      RangeError,
    );
    expect(() =>
      assertValidBucketConfig({ capacity: Number.POSITIVE_INFINITY, refillPerSec: 10 }),
    ).toThrow(RangeError);
    expect(() => assertValidBucketConfig({ capacity: 10, refillPerSec: -1 })).toThrow(RangeError);
    expect(() => assertValidBucketConfig({ capacity: 0, refillPerSec: 0 })).not.toThrow();
  });
});
