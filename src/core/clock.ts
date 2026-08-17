/**
 * The time and randomness PORTS. Nothing else in the library may read the wall
 * clock or the global PRNG directly.
 *
 * `src/core/runtime.ts` is the ONLY file allowed to call `Date.now()`,
 * `Math.random()`, `setTimeout`, `clearTimeout` or `crypto.randomUUID()`.
 * Everything else takes a `Clock` / `Timers` / `Random` / `Runtime` as an
 * argument. Tests inject the fakes from `test/support/`.
 */

/** Monotonic-ish millisecond clock. The only source of time. */
export interface Clock {
  /** Epoch milliseconds. */
  now(): number;
}

/**
 * Injectable timer pair. `handle` is deliberately `unknown`: Node returns a
 * `Timeout` object, a fake returns a number, and no caller should care.
 */
export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

/** Uniform in `[0, 1)`. The only source of randomness. */
export type Random = () => number;

/** Wall-clock implementation of {@link Clock}. */
export const systemClock: Clock = {
  now: (): number => Date.now(),
};

/** Global-timer implementation of {@link Timers}. */
export const systemTimers: Timers = {
  setTimeout: (fn: () => void, ms: number): unknown => setTimeout(fn, ms),
  clearTimeout: (handle: unknown): void => {
    clearTimeout(handle as ReturnType<typeof setTimeout>);
  },
};

/** `Math.random`, wrapped so the seam is explicit at every call site. */
export const systemRandom: Random = () => Math.random();
