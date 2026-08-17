/**
 * Deadline arithmetic — U5. PURE: no timers, no promises, no clock reads.
 *
 * Every entry point takes `now` as an argument, which is what makes the
 * composition rules table-testable and is the reason `timeout.ts` contains no
 * arithmetic of its own.
 *
 * Two representations, deliberately distinct:
 *
 *   - a TIMEOUT  is a RELATIVE budget in ms — `10_000`, or `Infinity` for none.
 *   - a DEADLINE is an ABSOLUTE epoch-ms instant — `ctx.deadlineAt`, or
 *     `undefined` for none.
 *
 * {@link composeDeadline} is the one rule that matters: **the tighter of the
 * two wins.** A per-attempt timeout may never outlive the call-level deadline
 * it sits inside. That clamp is also why `attemptTimeoutMs >= totalTimeoutMs`
 * is harmless rather than a configuration error — it enforces at runtime the
 * invariant `TimeoutOptions` only documents.
 *
 * Sign conventions (R4 §2.5):
 *   - `0` is a legal timeout and means "already out of budget".
 *   - `Infinity` is a legal timeout and means "unbounded, create no timer".
 *   - negative and `NaN` are programmer errors ⇒ `RangeError`.
 */

import { DEFAULTS, type ResolvedTimeoutOptions, type TimeoutOptions } from '../../core/policy.ts';

/** A timeout budget meaning "no bound at all". No timer is ever created for it. */
export const UNBOUNDED_MS: number = Number.POSITIVE_INFINITY;

/** Which constraint ended up binding — surfaced in `TimeoutError.details.source`. */
export type DeadlineSource = 'timeout' | 'deadline' | 'unbounded';

export interface ComposeDeadlineInput {
  /** Current time, from `Runtime.now()`. Never `Date.now()`. */
  readonly now: number;
  /** The relative budget this policy was configured with. May be `Infinity`. */
  readonly timeoutMs: number;
  /** An absolute deadline already in force upstream, if any. */
  readonly deadlineAt?: number | undefined;
}

export interface DeadlineComposition {
  /** Effective relative budget: `min(timeoutMs, remaining)`. `Infinity` ⇒ no timer. */
  readonly timeoutMs: number;
  /** Effective absolute deadline, or `undefined` when the result is unbounded. */
  readonly deadlineAt: number | undefined;
  /** `true` when there is no budget left; the caller must fail fast, not start a timer. */
  readonly expired: boolean;
  /** Which of the two inputs was binding. */
  readonly source: DeadlineSource;
}

/**
 * Validates a millisecond budget. `0` and `Infinity` are legal; negatives,
 * `NaN` and non-numbers are not (R4 TO-8: `RangeError`, at construction).
 */
export function assertTimeoutMs(value: number, label: string): number {
  if (typeof value !== 'number' || Number.isNaN(value) || value < 0) {
    throw new RangeError(
      `${label} must be a non-negative number of milliseconds (0 and Infinity allowed); received ${String(value)}`,
    );
  }
  return value;
}

/** Milliseconds left before `deadlineAt`, clamped at `0`. Unbounded ⇒ `Infinity`. */
export function remainingMs(deadlineAt: number | undefined, now: number): number {
  if (deadlineAt === undefined) return UNBOUNDED_MS;
  return Math.max(0, deadlineAt - now);
}

/** `true` once `now` has reached an existing deadline. An absent deadline never expires. */
export function isExpired(deadlineAt: number | undefined, now: number): boolean {
  return deadlineAt !== undefined && now >= deadlineAt;
}

/** The absolute instant a relative budget ends at. Unbounded budgets have no instant. */
export function deadlineAtFrom(now: number, timeoutMs: number): number | undefined {
  return Number.isFinite(timeoutMs) ? now + timeoutMs : undefined;
}

/** The tighter of two relative budgets. */
export function tighterTimeoutMs(a: number, b: number): number {
  return Math.min(a, b);
}

/**
 * The earlier of two absolute deadlines, treating `undefined` as "no deadline"
 * rather than as `0` — this is the composition an attempt-scope policy performs
 * against the call-scope one.
 */
export function earliestDeadline(a: number | undefined, b: number | undefined): number | undefined {
  if (a === undefined) return b;
  if (b === undefined) return a;
  return Math.min(a, b);
}

/**
 * Compose a relative timeout with an inherited absolute deadline. The tighter
 * of the two wins, and the result is expressed both ways so the caller can arm
 * a timer (`timeoutMs`) and hand a narrowed deadline downstream (`deadlineAt`).
 */
export function composeDeadline(input: ComposeDeadlineInput): DeadlineComposition {
  const { now, deadlineAt } = input;
  const timeoutMs = assertTimeoutMs(input.timeoutMs, 'timeoutMs');
  const remaining = remainingMs(deadlineAt, now);
  const budget = tighterTimeoutMs(timeoutMs, remaining);

  if (!Number.isFinite(budget)) {
    return { timeoutMs: UNBOUNDED_MS, deadlineAt: undefined, expired: false, source: 'unbounded' };
  }
  return {
    timeoutMs: budget,
    deadlineAt: earliestDeadline(deadlineAt, deadlineAtFrom(now, budget)),
    expired: budget <= 0,
    // Ties go to the policy's own timeout: it is the one the user configured.
    source: remaining < timeoutMs ? 'deadline' : 'timeout',
  };
}

/**
 * Applies {@link DEFAULTS}`.timeout` and validates. Called at FACTORY time, so
 * a bad option is a `RangeError` at construction rather than a surprise on the
 * first call (R4 TO-8).
 *
 * No cross-field check that `attemptTimeoutMs < totalTimeoutMs`: each factory
 * reads only its own field, and `composeDeadline` clamps the attempt budget to
 * whatever the call deadline left, so an inverted pair degrades gracefully
 * instead of throwing.
 */
export function resolveTimeoutOptions(options: TimeoutOptions = {}): ResolvedTimeoutOptions {
  return {
    attemptTimeoutMs: assertTimeoutMs(
      options.attemptTimeoutMs ?? DEFAULTS.timeout.attemptTimeoutMs,
      'attemptTimeoutMs',
    ),
    totalTimeoutMs: assertTimeoutMs(
      options.totalTimeoutMs ?? DEFAULTS.timeout.totalTimeoutMs,
      'totalTimeoutMs',
    ),
  };
}

/** Human-readable budget for policy names and traces. */
export function describeMs(ms: number): string {
  return Number.isFinite(ms) ? `${ms}ms` : 'unbounded';
}
