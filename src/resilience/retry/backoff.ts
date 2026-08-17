/**
 * BACKOFF CURVES — pure, side-effect free, and deterministic.
 *
 * Transcribed from R4 §1.1 / §5.5 (`computeDelay` + `decorrelated`), which were
 * executed and validated before publication. Every function here is a pure
 * function of `(retryIndex, options, random())`, which is exactly what lets a
 * test pin an EXACT delay with `scriptedRandom([...])` instead of asserting a
 * range.
 *
 * TWO THINGS ARE LOAD-BEARING AND EASY TO GET WRONG:
 *
 *  1. **`retryIndex` is 1-based and counts RETRIES, not attempts.** `1` is the
 *     wait *before* attempt 2. The exponential term is `factor ** (n - 1)`, so
 *     the first retry waits exactly `baseDelayMs`.
 *
 *  2. **The cap is applied BEFORE the jitter, never after** (R4 §1.4):
 *     `rand() * min(cap, base * factor ** (n - 1))`, *not*
 *     `min(cap, rand() * base * factor ** (n - 1))`. Capping afterwards
 *     compresses the distribution against the cap and destroys the very
 *     decorrelation the jitter exists to provide.
 *
 * `random()` is drawn EXACTLY ONCE for `'full'`, `'equal'` and `'decorrelated'`,
 * and ZERO times for `'fixed'` and `'exponential'`. Tests that script the PRNG
 * depend on that draw count, so it is part of the contract.
 */

import type { Random } from '../../core/clock.ts';
import type { ResolvedRetryOptions } from '../../core/policy.ts';

/**
 * The subset of {@link ResolvedRetryOptions} the curves actually read.
 *
 * Derived from the core type with `Pick` rather than restated, so it can never
 * drift from the contract, and narrow enough that a test can pass a literal.
 */
export type BackoffParams = Pick<
  ResolvedRetryOptions,
  'strategy' | 'baseDelayMs' | 'factor' | 'maxDelayMs'
>;

/** Delays are milliseconds: never negative, never `NaN`. */
function clamp(ms: number): number {
  return Number.isNaN(ms) ? 0 : Math.max(0, ms);
}

/**
 * `exp(n) = min(cap, base * factor ** (n - 1))` — the CAPPED, UNJITTERED term
 * every exponential-family strategy is built on.
 *
 * `retryIndex` is clamped to `>= 1` so a caller that passes `0` gets the first
 * retry's delay rather than a nonsensical `base / factor`.
 */
export function exponentialTerm(retryIndex: number, options: BackoffParams): number {
  const n = Math.max(1, Math.floor(retryIndex));
  // Cap FIRST (R4 §1.4). An overflow to Infinity collapses onto the cap, which
  // is exactly the desired behaviour.
  return clamp(Math.min(options.maxDelayMs, options.baseDelayMs * options.factor ** (n - 1)));
}

/**
 * Decorrelated jitter — `min(cap, randBetween(base, prev * 3))`, seeded with
 * `prev = baseDelayMs` (R4 §5.5, verbatim).
 *
 * This is the one STATEFUL curve: the next delay is a function of the previous
 * one, so the cap is necessarily applied after the draw. That is inherent to
 * the algorithm and is not the "cap before jitter" rule being broken — that
 * rule governs the exponential term, which decorrelated does not use.
 */
export function decorrelatedDelay(
  previousDelayMs: number,
  options: BackoffParams,
  random: Random,
): number {
  const prev = clamp(previousDelayMs);
  return clamp(
    Math.min(options.maxDelayMs, options.baseDelayMs + random() * (prev * 3 - options.baseDelayMs)),
  );
}

/**
 * The delay before retry `retryIndex` (1-based), for any strategy.
 *
 * `previousDelayMs` is only read by `'decorrelated'`; it defaults to
 * `baseDelayMs`, the seed R4 specifies. Use {@link createBackoff} when you need
 * that value threaded across a whole retry loop.
 */
export function backoffDelay(
  retryIndex: number,
  options: BackoffParams,
  random: Random,
  previousDelayMs?: number,
): number {
  const exp = exponentialTerm(retryIndex, options);
  switch (options.strategy) {
    case 'fixed':
      // Flat `base`, still subject to the cap so `maxDelayMs` always wins.
      return clamp(Math.min(options.maxDelayMs, options.baseDelayMs));
    case 'exponential':
      return exp;
    case 'full':
      // [0, exp) — the DEFAULT. Least total work under contention (R4 §1.1).
      return clamp(random() * exp);
    case 'equal':
      // [exp/2, exp]
      return clamp(exp / 2 + random() * (exp / 2));
    case 'decorrelated':
      return decorrelatedDelay(previousDelayMs ?? options.baseDelayMs, options, random);
  }
}

/**
 * A backoff sequencer that threads the previous delay for you.
 *
 * Only `'decorrelated'` needs the state; the other four strategies ignore it.
 * Holding one of these per retry loop keeps `retry.ts` free of strategy
 * special-casing.
 */
export interface Backoff {
  /** Delay before retry `retryIndex` (1-based). Advances the internal state. */
  next(retryIndex: number): number;
  /** Re-seeds the decorrelated chain back to `baseDelayMs`. */
  reset(): void;
  /** The last value {@link Backoff.next} produced; the decorrelated seed. */
  readonly previousDelayMs: number;
}

export function createBackoff(options: BackoffParams, random: Random): Backoff {
  let previous = clamp(options.baseDelayMs);
  return {
    next(retryIndex: number): number {
      const delay = backoffDelay(retryIndex, options, random, previous);
      previous = delay;
      return delay;
    },
    reset(): void {
      previous = clamp(options.baseDelayMs);
    },
    get previousDelayMs(): number {
      return previous;
    },
  };
}
