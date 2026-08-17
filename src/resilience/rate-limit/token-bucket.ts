/**
 * THE TOKEN BUCKET — pure, allocation-only, zero I/O.
 *
 * Transcribed from R4 §5.4 with R4 §4.2's refill math, restated as pure
 * functions so the whole algorithm is table-testable without a clock, a timer
 * or a promise. The policy in `rate-limit.ts` owns the mutable cell; this file
 * owns the arithmetic and nothing else.
 *
 * Why token bucket and not the alternatives (R4 §4.1, DECIDED — do not swap it):
 *  - leaky bucket forbids bursts, and real client workloads are bursty;
 *  - fixed window admits 2x the limit across a window edge;
 *  - sliding window log is O(n) memory — a timestamp per request;
 *  - sliding window counter only approximates, which buys nothing in-process.
 * Token bucket is the only one that is O(1) in memory AND answers
 * "how long until N tokens exist?" in CLOSED FORM — which is what makes
 * queue-and-wait implementable with one timer per queue instead of polling.
 *
 * TWO ANTI-DRIFT RULES, BOTH LOAD-BEARING (R4 §4.2):
 *  1. Accrue in continuous FRACTIONAL tokens, never in whole-token steps. A
 *     design that only adds a token once a whole one has accrued loses the
 *     remainder on every call and drifts systematically slow — at 10 calls/s
 *     against a 10 tok/s bucket it would deliver ~0 tokens forever.
 *  2. Advance `last` to the observed `now` on EVERY refill, before any early
 *     return. Never advance it by a computed whole-token quantum, and never
 *     skip the update when the rate is 0. Both variants accumulate error.
 *
 * FLOATING POINT (R4 §4.2 caveat, checklist RL-6). Fractional accrual
 * accumulates IEEE-754 error: 1000 successive 1 ms advances against a
 * 10 tok/s bucket yield `9.999999999999831`, not `10`. That is CORRECT — the
 * drift is ~1.7e-13 and bounded. Tests must compare token counts with a
 * tolerance (`Math.abs(actual - expected) < 1e-9`) and NEVER with `===`.
 */

/** Static configuration of a bucket. Both fields are >= 0 and finite. */
export interface TokenBucketConfig {
  /** Max burst. The bucket starts FULL, so `capacity` grants are free at t=0. */
  readonly capacity: number;
  /** Continuous accrual rate. `0` means "never refills". */
  readonly refillPerSec: number;
}

/**
 * An immutable bucket snapshot. Config travels WITH the state so every
 * operation keeps the `(state, now, cost)` shape and no caller has to thread a
 * second argument through the queue machinery.
 *
 * `last` is a reading of a MONOTONIC clock (`Runtime.now()`), never
 * `Date.now()`. Monotonicity is the reason this algorithm survives a clock
 * jump: an NTP step forward on a wall clock would mint a whole burst
 * instantly — the exact quota violation the limiter exists to prevent.
 */
export interface TokenBucketState extends TokenBucketConfig {
  /** Fractional by design. See the anti-drift rules above. */
  readonly tokens: number;
  /** Monotonic ms at which `tokens` was last computed. */
  readonly last: number;
}

/** The result of an attempted take: `(state, now, cost) => outcome`. */
export interface TokenBucketOutcome {
  readonly allowed: boolean;
  /** `0` when allowed; `Number.POSITIVE_INFINITY` when the rate is 0. */
  readonly waitMs: number;
  /** The successor state. Tokens are deducted only when `allowed`. */
  readonly state: TokenBucketState;
}

/** Tokens spent per call when the caller does not say otherwise. */
export const DEFAULT_COST = 1;

/**
 * Rejects a configuration that cannot describe a bucket. Called once, by the
 * policy factory — the hot path never re-validates.
 */
export function assertValidBucketConfig(config: TokenBucketConfig): void {
  if (!Number.isFinite(config.capacity) || config.capacity < 0) {
    throw new RangeError(`capacity must be a finite number >= 0, got ${String(config.capacity)}`);
  }
  if (!Number.isFinite(config.refillPerSec) || config.refillPerSec < 0) {
    throw new RangeError(
      `refillPerSec must be a finite number >= 0, got ${String(config.refillPerSec)}`,
    );
  }
}

/** A fresh bucket. It starts **FULL** — a burst of `capacity` is free at t=0 (R4 §4.5). */
export function createTokenBucket(config: TokenBucketConfig, now: number): TokenBucketState {
  return {
    capacity: config.capacity,
    refillPerSec: config.refillPerSec,
    tokens: config.capacity,
    last: now,
  };
}

/**
 * Accrue tokens up to `now`. Pure: returns a successor, mutates nothing.
 *
 * The `now <= last` branch is the monotonic guard. A backwards clock advances
 * `last` and mints NOTHING, so a rewind cannot conjure tokens; the limiter
 * recovers on the next forward tick. The guard stays even though the real
 * clock is monotonic, because tests inject clocks and a test may rewind one.
 */
export function refill(state: TokenBucketState, now: number): TokenBucketState {
  if (now <= state.last) return { ...state, last: now };
  const elapsedSec = (now - state.last) / 1000;
  // `last` advances FIRST and unconditionally — including when the rate is 0.
  if (state.refillPerSec <= 0) return { ...state, last: now };
  return {
    ...state,
    last: now,
    tokens: Math.min(state.capacity, state.tokens + elapsedSec * state.refillPerSec),
  };
}

/**
 * Closed-form "time until `cost` tokens exist", on an ALREADY-REFILLED state.
 *
 * `Math.ceil` is deliberate (R4 §4.2): rounding down would wake a waiter a
 * fraction of a millisecond early, it would find insufficient tokens, and it
 * would re-queue — a busy-wait loop. Round up, always.
 *
 * `cost` above `capacity` is answered honestly rather than clamped: the queue
 * uses this to price its own drain time, where cumulative demand legitimately
 * exceeds one bucketful because tokens are consumed as they arrive.
 */
export function waitMsFor(state: TokenBucketState, cost: number = DEFAULT_COST): number {
  if (state.tokens >= cost) return 0;
  if (state.refillPerSec <= 0) return Number.POSITIVE_INFINITY;
  return Math.ceil(((cost - state.tokens) / state.refillPerSec) * 1000);
}

/** `refill` + {@link waitMsFor}, for callers holding a stale state. */
export function msUntil(state: TokenBucketState, now: number, cost: number = DEFAULT_COST): number {
  return waitMsFor(refill(state, now), cost);
}

/**
 * THE primitive: `(state, now, cost) => { allowed, waitMs, state }`.
 *
 * `>=`, not `>`: at the exact boundary the token IS available (R4 §4.5).
 * On refusal the returned state is the refilled one — the clock reading is
 * never thrown away, which is half of rule 2 above.
 */
export function consume(
  state: TokenBucketState,
  now: number,
  cost: number = DEFAULT_COST,
): TokenBucketOutcome {
  const refilled = refill(state, now);
  if (refilled.tokens >= cost) {
    return { allowed: true, waitMs: 0, state: { ...refilled, tokens: refilled.tokens - cost } };
  }
  return { allowed: false, waitMs: waitMsFor(refilled, cost), state: refilled };
}

/**
 * Whether `cost` could EVER be granted. A request for more than `capacity`
 * would block a FIFO queue forever, so the policy rejects it up front with a
 * `RangeError` instead of enqueueing it (R4 §4.3, head-of-line rule).
 */
export function isSatisfiable(state: TokenBucketConfig, cost: number): boolean {
  return cost <= state.capacity;
}
