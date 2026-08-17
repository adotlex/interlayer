/**
 * Deterministic randomness for tests — READ-ONLY to Wave 2.
 *
 * `Random` is `() => number` in `[0, 1)`. Because every jittered delay in this
 * library is a PURE function of `(n, rand())`, pinning `rand()` pins the delay
 * exactly — so tests assert equality, not a range:
 *
 * ```ts
 * const rand = scriptedRandom([0.5, 0.25]);
 * // full jitter = rand() * min(cap, base * factor ** (n - 1))
 * expect(delayFor(1, opts, rand)).toBe(50);   // 0.5  * 100
 * expect(delayFor(2, opts, rand)).toBe(50);   // 0.25 * 200
 * ```
 *
 * Use `scriptedRandom` when the test asserts an exact delay, and
 * `seededRandom` when it only needs reproducibility across runs.
 */

import type { Random } from '../../src/core/clock.ts';

/**
 * mulberry32 — 4 lines, uniform enough for jitter, fully reproducible.
 * Same seed => byte-identical sequence, on every machine, forever.
 */
export function seededRandom(seed = 1): Random {
  let a = seed >>> 0;
  return (): number => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface ScriptedRandom {
  (): number;
  /** How many values have been drawn so far. */
  readonly drawn: number;
  /** Values still queued. */
  readonly remaining: number;
}

/**
 * Returns the given values in order. When the script runs out it repeats the
 * LAST value rather than throwing, so a test that only cares about the first
 * two delays does not have to enumerate the rest. An empty script yields 0.
 */
export function scriptedRandom(values: readonly number[]): ScriptedRandom {
  let i = 0;
  const fn = (): number => {
    const v = values.length === 0 ? 0 : (values[Math.min(i, values.length - 1)] ?? 0);
    i++;
    return v;
  };
  Object.defineProperties(fn, {
    drawn: { get: () => i, enumerable: true },
    remaining: { get: () => Math.max(0, values.length - i), enumerable: true },
  });
  return fn as ScriptedRandom;
}

/** A `Random` that always returns the same value. `constantRandom(0)` kills jitter. */
export function constantRandom(value: number): Random {
  return (): number => value;
}
