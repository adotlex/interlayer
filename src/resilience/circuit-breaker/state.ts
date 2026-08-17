/**
 * THE CIRCUIT-BREAKER STATE MACHINE — a PURE reducer, `(state, event, now) => state`.
 *
 * Nothing in this file reads a clock, schedules a timer, touches I/O or mutates
 * its input. `now` is always a parameter. That is deliberate: the entire machine
 * is exercisable from a plain array of transitions (see `state.test.ts`), which
 * is the only way to get honest coverage of a state machine whose interesting
 * behaviour is concurrency- and time-dependent.
 *
 * Transcribed from R4 §3.1/§3.4 and the validated implementation in R4 §5.3,
 * turned inside out from a mutating class into a reducer.
 *
 * THE ONE RULE THAT IS EASY TO GET WRONG (R4 §3.4, checklist CB-6):
 *
 *   The trip condition is evaluated after EVERY recorded outcome, success and
 *   failure alike — never only in the failure path. R4 found this by running its
 *   own draft: with `minimumThroughput: 10`, a 5-fail-then-5-succeed sequence
 *   leaves `total === 5` when the last FAILURE arrives, so a failure-only check
 *   never re-runs and the breaker silently never trips at a true 50 % failure
 *   rate. `settle()` below funnels both outcomes through `evaluate()`.
 *
 * The lazy `open -> half-open` transition (R4 §3.1) lives in `tick()`. There is
 * no background timer anywhere: a timer would hold the event loop open and need
 * disposal. The consequence, which R4 tells us to document: a state read that
 * does not pass `now` can report a stale `'open'`. Read through
 * {@link currentState}, which takes `now`.
 */

import { ConfigError } from '../../core/errors.ts';
import { type BreakerOptions, DEFAULTS, type ResolvedBreakerOptions } from '../../core/policy.ts';
import type { BreakerState, Issue } from '../../core/types.ts';

/* ------------------------------------------------------------------ *
 * 1. Shape
 * ------------------------------------------------------------------ */

/**
 * One fixed-width slice of the rolling window.
 *
 * `windowMs / bucketMs` buckets bound the memory at O(1) regardless of
 * throughput (R4 §3.2). Per-call timestamp lists were rejected: unbounded.
 */
export interface Bucket {
  /** Bucket id — the outcome's timestamp floored to `bucketMs`. */
  readonly t: number;
  /** Successes recorded in this slice. */
  readonly s: number;
  /** Failures recorded in this slice. */
  readonly f: number;
}

/**
 * The complete machine state. Immutable: every reducer returns a new value.
 *
 * `state` reuses `BreakerState` from `src/core/types.ts` — the same union the
 * `breaker:transition` event payload carries, so no translation layer can drift.
 * It is a string-literal union, never an `enum` (`erasableSyntaxOnly`).
 */
export interface BreakerData {
  readonly state: BreakerState;
  /** `now` at the moment the breaker last entered `'open'`. */
  readonly openedAt: number;
  /** Rolling window, oldest first. Cleared on every state transition. */
  readonly buckets: readonly Bucket[];
  /** Trials admitted but not yet settled. Only meaningful in `'half-open'`. */
  readonly halfOpenInFlight: number;
  /** Trials settled successfully since entering `'half-open'`. */
  readonly halfOpenSuccesses: number;
  /** Drives `mode: 'consecutive'`. Reset by any success and by any transition. */
  readonly consecutiveFailures: number;
  /**
   * Bumped by EVERY state transition. A call captures it at admission and hands
   * it back at settlement; a mismatch means the breaker moved underneath the
   * call, and the settlement is discarded (R4 §3.7). Without this, a slow
   * half-open trial resolving after the breaker already re-opened would wrongly
   * close the circuit.
   */
  readonly generation: number;
}

/**
 * Everything that can move the machine.
 *
 * `generation` is optional so table tests can write `{ type: 'failure' }` and
 * mean "a settlement that is definitely current". The policy in `breaker.ts`
 * always supplies it.
 */
export type BreakerEvent =
  | { readonly type: 'tick' }
  | { readonly type: 'admit' }
  | { readonly type: 'success'; readonly generation?: number | undefined }
  | { readonly type: 'failure'; readonly generation?: number | undefined }
  | { readonly type: 'trip' }
  | { readonly type: 'reset' };

/** Window totals at an instant. */
export interface BreakerTotals {
  readonly successes: number;
  readonly failures: number;
  readonly total: number;
}

/** The curried reducer — literally `(state, event, now) => state`. */
export type BreakerReducer = (state: BreakerData, event: BreakerEvent, now: number) => BreakerData;

/* ------------------------------------------------------------------ *
 * 2. Construction
 * ------------------------------------------------------------------ */

/** A fresh breaker: `'closed'`, empty window, generation 0 (CB-1). */
export function initialBreakerData(): BreakerData {
  return {
    state: 'closed',
    openedAt: 0,
    buckets: [],
    halfOpenInFlight: 0,
    halfOpenSuccesses: 0,
    consecutiveFailures: 0,
    generation: 0,
  };
}

/**
 * Resolves and VALIDATES options against `DEFAULTS.circuitBreaker`.
 *
 * Validation lives here, in the option-resolution step the policy builder runs,
 * not in the reducer: R4 CB-27 is explicit that the machine itself does not
 * validate (constructing it with `minimumThroughput: 1` must not throw), and the
 * checklist puts the test on the builder. Throws `ConfigError` rather than
 * `RangeError` because `src/core/errors.ts` is the project's error taxonomy and
 * units do not invent error types.
 */
export function resolveBreakerOptions(options: BreakerOptions = {}): ResolvedBreakerOptions {
  const d = DEFAULTS.circuitBreaker;
  const resolved: ResolvedBreakerOptions = {
    mode: options.mode ?? d.mode,
    failureRatio: options.failureRatio ?? d.failureRatio,
    minimumThroughput: options.minimumThroughput ?? d.minimumThroughput,
    windowMs: options.windowMs ?? d.windowMs,
    bucketMs: options.bucketMs ?? d.bucketMs,
    resetMs: options.resetMs ?? d.resetMs,
    halfOpenMaxConcurrent: options.halfOpenMaxConcurrent ?? d.halfOpenMaxConcurrent,
    halfOpenSuccessesToClose: options.halfOpenSuccessesToClose ?? d.halfOpenSuccessesToClose,
  };

  const issues: Issue[] = [];
  const require = (ok: boolean, field: string, message: string): void => {
    if (!ok) issues.push({ path: ['breaker', field], message, code: 'invalid_value' });
  };

  require(resolved.mode === 'ratio' ||
    resolved.mode === 'consecutive', 'mode', "must be 'ratio' or 'consecutive'");
  require(resolved.failureRatio > 0 &&
    resolved.failureRatio <= 1, 'failureRatio', 'must be in (0, 1]');
  // Floor of 2 — Polly enforces exactly this. Without it `failureRatio: 0.5`
  // trips on the first single failed request (1/1 = 100 %), which is the single
  // most common circuit-breaker misconfiguration (R4 §3.2).
  require(Number.isInteger(resolved.minimumThroughput) &&
    resolved.minimumThroughput >= 2, 'minimumThroughput', 'must be an integer >= 2');
  require(resolved.windowMs > 0, 'windowMs', 'must be > 0');
  require(resolved.bucketMs > 0, 'bucketMs', 'must be > 0');
  require(resolved.bucketMs <= resolved.windowMs, 'bucketMs', 'must be <= windowMs');
  require(resolved.resetMs >= 0, 'resetMs', 'must be >= 0');
  require(resolved.halfOpenMaxConcurrent >= 1, 'halfOpenMaxConcurrent', 'must be >= 1');
  require(resolved.halfOpenSuccessesToClose >= 1, 'halfOpenSuccessesToClose', 'must be >= 1');

  if (issues.length > 0) {
    throw new ConfigError(
      `Invalid circuit-breaker options: ${issues.map((i) => `${String(i.path.at(-1))} ${i.message}`).join('; ')}`,
      issues,
    );
  }
  return resolved;
}

/** Binds options so the reducer has exactly the `(state, event, now)` shape. */
export function createBreakerReducer(options: ResolvedBreakerOptions): BreakerReducer {
  return (state: BreakerData, event: BreakerEvent, now: number): BreakerData =>
    reduceBreaker(state, event, now, options);
}

/* ------------------------------------------------------------------ *
 * 3. Queries — pure, total, side-effect free
 * ------------------------------------------------------------------ */

/** Successes/failures still inside the rolling window at `now`. */
export function windowTotals(
  state: BreakerData,
  now: number,
  options: ResolvedBreakerOptions,
): BreakerTotals {
  let successes = 0;
  let failures = 0;
  for (const b of prune(state.buckets, now, options.windowMs)) {
    successes += b.s;
    failures += b.f;
  }
  return { successes, failures, total: successes + failures };
}

/**
 * The state as of `now`, INCLUDING the lazy `open -> half-open` transition.
 * A read that does not pass `now` can report a stale `'open'` (R4 §3.1).
 */
export function currentState(
  state: BreakerData,
  now: number,
  options: ResolvedBreakerOptions,
): BreakerState {
  return tick(state, now, options).state;
}

/** `now` at which an `'open'` breaker will admit its next probe. */
export function halfOpenAt(state: BreakerData, options: ResolvedBreakerOptions): number {
  return state.openedAt + options.resetMs;
}

/**
 * Whether a call arriving at `now` is admitted.
 *
 * The caller MUST check this and then dispatch `{ type: 'admit' }` with no
 * `await` in between — R4 §3.7 makes the synchronous check-then-increment a hard
 * requirement, and it is the only thing keeping `halfOpenMaxConcurrent` honest
 * under a burst. `admit` is a no-op on a state that fails this check, so the two
 * cannot disagree.
 */
export function canAdmit(
  state: BreakerData,
  now: number,
  options: ResolvedBreakerOptions,
): boolean {
  const ticked = tick(state, now, options);
  if (ticked.state === 'closed') return true;
  if (ticked.state === 'open') return false;
  return ticked.halfOpenInFlight < options.halfOpenMaxConcurrent;
}

/* ------------------------------------------------------------------ *
 * 4. The reducer
 * ------------------------------------------------------------------ */

/** `(state, event, now) => state`. Pure. No clock, no timers, no I/O. */
export function reduceBreaker(
  state: BreakerData,
  event: BreakerEvent,
  now: number,
  options: ResolvedBreakerOptions,
): BreakerData {
  switch (event.type) {
    case 'tick':
      return tick(state, now, options);
    case 'admit':
      return admit(state, now, options);
    case 'success':
      return settle(state, event.generation, now, options, true);
    case 'failure':
      return settle(state, event.generation, now, options, false);
    case 'trip':
      return open(state, now);
    case 'reset':
      return close(state);
  }
}

/** Lazy `open -> half-open`. `>=`, not `>`, so `now - openedAt === resetMs` moves (CB-10). */
function tick(state: BreakerData, now: number, options: ResolvedBreakerOptions): BreakerData {
  if (state.state !== 'open') return state;
  if (now - state.openedAt < options.resetMs) return state;
  return toHalfOpen(state);
}

/**
 * Takes a trial slot when one is free. A no-op in every case {@link canAdmit}
 * rejects, so admitting without checking cannot corrupt the counter.
 */
function admit(state: BreakerData, now: number, options: ResolvedBreakerOptions): BreakerData {
  const ticked = tick(state, now, options);
  // `'closed'` passes through without bookkeeping; `'open'` is a rejection.
  if (ticked.state !== 'half-open') return ticked;
  if (ticked.halfOpenInFlight >= options.halfOpenMaxConcurrent) return ticked;
  return { ...ticked, halfOpenInFlight: ticked.halfOpenInFlight + 1 };
}

function settle(
  state: BreakerData,
  generation: number | undefined,
  now: number,
  options: ResolvedBreakerOptions,
  ok: boolean,
): BreakerData {
  // Stale settlement: the breaker transitioned while this call was in flight,
  // so its outcome describes a machine that no longer exists. Discard it.
  if (generation !== undefined && generation !== state.generation) return state;

  if (state.state === 'half-open') {
    const halfOpenInFlight = Math.max(0, state.halfOpenInFlight - 1);
    // Demotion: ANY single failed trial re-opens, and `openedAt` restarts the
    // full cooldown. There is no "3 strikes in half-open" (R4 §3.4, CB-15/16).
    if (!ok) return open({ ...state, halfOpenInFlight }, now);
    const halfOpenSuccesses = state.halfOpenSuccesses + 1;
    if (halfOpenSuccesses >= options.halfOpenSuccessesToClose) {
      return close({ ...state, halfOpenInFlight });
    }
    return { ...state, halfOpenInFlight, halfOpenSuccesses, consecutiveFailures: 0 };
  }

  // `'open'`: the call was admitted before the breaker opened and its generation
  // was not supplied. Nothing is recorded — the window belongs to the next
  // generation now (R4 §3.7: an in-flight call's outcome is discarded).
  if (state.state !== 'closed') return state;

  const recorded: BreakerData = {
    ...state,
    buckets: record(state.buckets, now, ok, options),
    consecutiveFailures: ok ? 0 : state.consecutiveFailures + 1,
  };
  // CB-6. EVERY outcome, success and failure alike. Not just failures.
  return evaluate(recorded, now, options);
}

/** The trip condition. Runs after every recorded outcome — see the file header. */
function evaluate(state: BreakerData, now: number, options: ResolvedBreakerOptions): BreakerData {
  if (options.mode === 'consecutive') {
    // `BreakerOptions` carries no dedicated N for this mode, so
    // `minimumThroughput` doubles as the consecutive-failure threshold.
    return state.consecutiveFailures >= options.minimumThroughput ? open(state, now) : state;
  }
  const { failures, total } = windowTotals(state, now, options);
  // The minimum-throughput guard is the whole point and is not optional.
  if (total < options.minimumThroughput) return state;
  // `>=`, not `>`: a ratio exactly equal to `failureRatio` trips (CB-4).
  return failures / total >= options.failureRatio ? open(state, now) : state;
}

/* ------------------------------------------------------------------ *
 * 5. Window bookkeeping
 * ------------------------------------------------------------------ */

/** Drops buckets that fell out of the window. Returns the input when nothing expired. */
function prune(buckets: readonly Bucket[], now: number, windowMs: number): readonly Bucket[] {
  const cutoff = now - windowMs;
  let first = 0;
  while (first < buckets.length) {
    const b = buckets[first];
    if (b === undefined || b.t > cutoff) break;
    first++;
  }
  return first === 0 ? buckets : buckets.slice(first);
}

function record(
  buckets: readonly Bucket[],
  now: number,
  ok: boolean,
  options: ResolvedBreakerOptions,
): readonly Bucket[] {
  const pruned = prune(buckets, now, options.windowMs);
  const id = Math.floor(now / options.bucketMs) * options.bucketMs;
  const last = pruned.at(-1);
  if (last !== undefined && id <= last.t) {
    // Same bucket — or the clock jumped backwards. Folding into the newest
    // bucket keeps the array sorted oldest-first, which `prune` depends on; a
    // backwards jump must never resurrect an expired bucket (R4 §3.7).
    const merged: Bucket = { t: last.t, s: last.s + (ok ? 1 : 0), f: last.f + (ok ? 0 : 1) };
    return [...pruned.slice(0, -1), merged];
  }
  return [...pruned, { t: id, s: ok ? 1 : 0, f: ok ? 0 : 1 }];
}

/* ------------------------------------------------------------------ *
 * 6. Transitions — each one clears the window and voids in-flight calls
 * ------------------------------------------------------------------ */

function open(state: BreakerData, now: number): BreakerData {
  return { ...clearWindow(state), state: 'open', openedAt: now };
}

function close(state: BreakerData): BreakerData {
  return { ...clearWindow(state), state: 'closed' };
}

function toHalfOpen(state: BreakerData): BreakerData {
  return { ...clearWindow(state), state: 'half-open' };
}

/**
 * Clears the rolling window and bumps the generation.
 *
 * The window is cleared on `'closed'` AND `'open'` entry alike: carrying
 * pre-outage failures into a freshly closed breaker would re-trip it on the
 * first hiccup (R4 §3.4, CB-17).
 */
function clearWindow(state: BreakerData): BreakerData {
  return {
    ...state,
    buckets: [],
    halfOpenInFlight: 0,
    halfOpenSuccesses: 0,
    consecutiveFailures: 0,
    generation: state.generation + 1,
  };
}
