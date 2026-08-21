/**
 * THE POLICY CONTRACT — the file that makes six parallel units possible.
 *
 * This file exists for exactly one reason (setup guide §2.1, R5 §7.4):
 *
 *   U2 (retry), U3 (breaker), U4 (rate limit) and U5 (timeout) each export a
 *   FACTORY RETURNING A `Policy`. U6 (routing) composes `Policy` VALUES and
 *   never learns that those units exist. Concrete wiring happens post-wave in
 *   `src/layer.ts`. A dependency graph becomes six independent lanes.
 *
 * A `Policy` is a piece of onion middleware (`src/core/compose.ts`) plus the
 * metadata needed to order, describe and dispose of it. Because `compose()`
 * deliberately relaxes koa's single-call guard, this one shape is enough for
 * all four:
 *
 *   retry           calls `next()` up to N times, SEQUENTIALLY  (re-entrant)
 *   attempt timeout calls `next(ctx')` once with a narrowed AbortSignal
 *   breaker         calls `next()` once, or throws `CircuitOpenError` first
 *   rate limit      awaits a token, then calls `next()` once
 *
 * NOTE ON THE WORD "POLICY". R6 §5.3 used `RetryPolicy` / `BreakerPolicy` to
 * mean an *options record*. Here — and in the setup guide, which is
 * authoritative — a `Policy` is the *middleware*, and the option records are
 * named `…Options`. One word, one meaning.
 */

import type { AttemptContext, CallContext, Middleware } from './types.ts';

/* ------------------------------------------------------------------ *
 * 1. The interface
 * ------------------------------------------------------------------ */

/**
 * Which stack a policy belongs on.
 *
 *  - `'call'`    runs ONCE per `layer.call()`, outside provider selection.
 *  - `'attempt'` runs ONCE per provider, inside the fallback chain.
 */
export type PolicyScope = 'call' | 'attempt';

/**
 * No `enum` (`erasableSyntaxOnly`). `'custom'` is the escape hatch for
 * user-supplied middleware; it always sorts innermost, in declaration order.
 */
export type PolicyKind =
  | 'total-timeout'
  | 'retry'
  | 'rate-limit'
  | 'circuit-breaker'
  | 'attempt-timeout'
  | 'custom';

/**
 * A composable resilience step.
 *
 * `execute` IS the middleware. Everything else is metadata: `kind` drives the
 * canonical ordering, `name` shows up in errors and traces, `describe()` lets
 * tests and telemetry read internal state without a downcast, and `dispose()`
 * releases anything long-lived (a breaker's window, a limiter's queue).
 */
export interface Policy<Ctx = AttemptContext, R = unknown> {
  readonly kind: PolicyKind;
  /** Stable, human-readable identity, e.g. `retry(openai)`. */
  readonly name: string;
  readonly scope: PolicyScope;
  /** The onion middleware. May call `next()` zero, one or many times. */
  readonly execute: Middleware<Ctx, R>;
  /** Optional introspection for tests and telemetry. Must be side-effect free. */
  readonly describe?: (() => Readonly<Record<string, unknown>>) | undefined;
  /** Optional teardown. Idempotent. */
  readonly dispose?: (() => void | Promise<void>) | undefined;
}

/** A policy on the per-provider stack — what U2–U5 return. */
export type AttemptPolicy = Policy<AttemptContext, unknown>;
/** A policy on the per-call stack (total deadline, logging, validation). */
export type CallPolicy = Policy<CallContext, unknown>;

/**
 * The shape every unit's public factory must have.
 *
 * Options are ALWAYS optional and always `| undefined` per field, so a caller
 * on `exactOptionalPropertyTypes` can forward a partial config straight
 * through. Defaults come from {@link DEFAULTS}.
 */
export type PolicyFactory<Options, Ctx = AttemptContext> = (
  options?: Options,
) => Policy<Ctx, unknown>;

/**
 * Tiny constructor so five units build the same object shape. Nothing here is
 * magic — it exists to stop five independent hand-rolled literals drifting.
 */
export function definePolicy<Ctx = AttemptContext, R = unknown>(
  spec: Policy<Ctx, R>,
): Policy<Ctx, R> {
  return spec;
}

/* ------------------------------------------------------------------ *
 * 2. Canonical ordering — R4 §5.6, DECIDED. Do not guess.
 * ------------------------------------------------------------------ */

/**
 * Outermost first — the exact array order `compose()` expects.
 *
 * ```
 *   ┌─ TotalTimeout        (outermost)  bounds the whole logical operation
 *   │  ┌─ Retry                         the retry loop
 *   │  │  ┌─ RateLimit                  every physical attempt spends a token
 *   │  │  │  ┌─ CircuitBreaker          every physical attempt is recorded
 *   │  │  │  │  ┌─ AttemptTimeout       bounds ONE provider call
 *   │  │  │  │  │  └── provider handler (innermost)
 * ```
 *
 * Three answers implementers otherwise get wrong (R4 §5.6):
 *  - Timeout is per-attempt AND overall; the per-attempt one is strictly smaller.
 *  - Retries DO consume rate-limit tokens: a retry is a physical call.
 *  - Each retry's failure DOES count toward the breaker: a dead provider should
 *    trip it fast, not 3× slower because the loop hid the failures.
 *
 * `RateLimit` sits INSIDE `Retry`, unlike Polly, because ours is a *quota*
 * limiter counting physical calls, not a *concurrency* limiter admitting
 * logical operations. Deliberate divergence — do not "fix" it back.
 */
export const POLICY_ORDER: readonly PolicyKind[] = [
  'total-timeout',
  'retry',
  'rate-limit',
  'circuit-breaker',
  'attempt-timeout',
  'custom',
] as const;

/** Which stack each kind belongs on. */
export const POLICY_SCOPE: Readonly<Record<PolicyKind, PolicyScope>> = {
  'total-timeout': 'call',
  retry: 'attempt',
  'rate-limit': 'attempt',
  'circuit-breaker': 'attempt',
  'attempt-timeout': 'attempt',
  custom: 'attempt',
};

/** Position in {@link POLICY_ORDER}; unknown kinds sort last. */
export function policyRank(kind: PolicyKind): number {
  const i = POLICY_ORDER.indexOf(kind);
  return i === -1 ? POLICY_ORDER.length : i;
}

/**
 * Stable sort into the canonical nesting order. Policies of the same kind keep
 * their declaration order, so two breakers (say, per-provider and global) nest
 * predictably.
 */
export function orderPolicies<Ctx, R>(
  policies: readonly Policy<Ctx, R>[],
): readonly Policy<Ctx, R>[] {
  return policies
    .map((policy, index) => ({ policy, index }))
    .sort((a, b) => policyRank(a.policy.kind) - policyRank(b.policy.kind) || a.index - b.index)
    .map((e) => e.policy);
}

/** Ordered policies of one scope, as the middleware array `compose()` takes. */
export function policyStack<Ctx, R>(
  policies: readonly Policy<Ctx, R>[],
  scope: PolicyScope,
): readonly Middleware<Ctx, R>[] {
  return orderPolicies(policies.filter((p) => p.scope === scope)).map((p) => p.execute);
}

/** Disposes every policy that declares a teardown. Never rejects. */
export async function disposePolicies<Ctx, R>(policies: readonly Policy<Ctx, R>[]): Promise<void> {
  for (const p of policies) {
    if (p.dispose === undefined) continue;
    try {
      await p.dispose();
    } catch {
      /* teardown must never mask the real failure */
    }
  }
}

/* ------------------------------------------------------------------ *
 * 3. Option records — R4 §0.1. Every duration carries an `Ms` suffix;
 *    every "off" is `false`, never `0` or `undefined` (R6).
 * ------------------------------------------------------------------ */

/** Which delay curve to walk. Default is `'full'` — exponential + full jitter. */
export type BackoffStrategy = 'fixed' | 'exponential' | 'full' | 'equal' | 'decorrelated';

export interface RetryOptions {
  /**
   * TOTAL invocations INCLUDING the first — cockatiel's meaning, not Polly's
   * and not p-retry's. `maxAttempts: 3` performs at most 2 retries. This is
   * settled (R4 §1.2); do not reinterpret it.
   */
  readonly maxAttempts?: number | undefined;
  readonly strategy?: BackoffStrategy | undefined;
  readonly baseDelayMs?: number | undefined;
  readonly factor?: number | undefined;
  /** Cap on the exponential term, applied BEFORE jitter (R4 §1.4). */
  readonly maxDelayMs?: number | undefined;
  /** Total wall-clock budget for the retry loop; derived from the deadline when unset. */
  readonly budgetMs?: number | undefined;
  readonly retryNonIdempotentOnConnectFailure?: boolean | undefined;
  /** Overrides the default `error.retryable` predicate. */
  readonly isRetryable?: ((error: unknown, attempt: number) => boolean) | undefined;
}

export interface TimeoutOptions {
  /** Bounds ONE provider call. Must be strictly smaller than `totalTimeoutMs`. */
  readonly attemptTimeoutMs?: number | undefined;
  /** Bounds the whole logical operation, across every provider tried. */
  readonly totalTimeoutMs?: number | undefined;
}

export type BreakerMode = 'ratio' | 'consecutive';

export interface BreakerOptions {
  readonly mode?: BreakerMode | undefined;
  /** `mode: 'ratio'` only. Trips at >= this failure ratio within the window. */
  readonly failureRatio?: number | undefined;
  /**
   * `mode: 'ratio'` only. Floor of 2. Guards against tripping on a single
   * request — evaluate the trip condition after EVERY outcome, not only
   * failures (R4 CB-6).
   */
  readonly minimumThroughput?: number | undefined;
  /**
   * `mode: 'consecutive'` only. Trips after this many failures IN A ROW; any
   * success resets the count. Default 5.
   *
   * Its own field because `minimumThroughput` used to double as this threshold,
   * which silently coupled two unrelated numbers: raising the ratio mode's
   * volume guard from 10 to 100 also demanded 100 consecutive failures before a
   * consecutive-mode breaker would trip. The two modes now share nothing but
   * the window bookkeeping.
   */
  readonly consecutiveFailureThreshold?: number | undefined;
  readonly windowMs?: number | undefined;
  /** Bucket granularity; `windowMs / bucketMs` buckets bounds the memory. */
  readonly bucketMs?: number | undefined;
  /** OPEN -> HALF_OPEN cooldown. Evaluated lazily, on the next call. */
  readonly resetMs?: number | undefined;
  readonly halfOpenMaxConcurrent?: number | undefined;
  readonly halfOpenSuccessesToClose?: number | undefined;
  /**
   * Decides which errors count against the breaker. Default: every error does.
   *
   * An error this returns `false` for is recorded as a SUCCESS and rethrown
   * unchanged — R4 §3.7 / CB-20, and what the breaker actually implements. (The
   * doc here once said "not counted at all"; it was the comment that was wrong,
   * not the code.) The point is that a caller's own bad input — an HTTP 404,
   * a validation failure — must not be evidence that the provider is unhealthy,
   * and in a ratio breaker "no evidence" would still shift the ratio unless the
   * call lands on the healthy side of it.
   *
   * A `CancelledError` never reaches this predicate: the breaker discards
   * caller-cancelled outcomes entirely rather than scoring them either way.
   */
  readonly isFailure?: ((error: unknown) => boolean) | undefined;
}

export type RateLimitExhaustion = 'wait' | 'reject';

export interface RateLimitOptions {
  /** Max burst. The bucket starts FULL. */
  readonly capacity?: number | undefined;
  readonly refillPerSec?: number | undefined;
  readonly onExhaustion?: RateLimitExhaustion | undefined;
  /** Mandatory bound on the FIFO queue. */
  readonly maxQueueDepth?: number | undefined;
  /** Mandatory bound on how long one caller may sit in that queue. */
  readonly maxQueueWaitMs?: number | undefined;
  /** Tokens spent per call. Default 1. */
  readonly cost?: number | undefined;
}

/**
 * The aggregate a user passes to `createLayer({ resilience })`.
 * `false` turns a policy off explicitly; `undefined` means "use the default".
 */
export interface ResilienceOptions {
  readonly timeout?: TimeoutOptions | false | undefined;
  readonly retry?: RetryOptions | false | undefined;
  readonly breaker?: BreakerOptions | false | undefined;
  readonly rateLimit?: RateLimitOptions | false | undefined;
}

/* ------------------------------------------------------------------ *
 * 4. The defaults — R4 §0.1, verbatim.
 * ------------------------------------------------------------------ */

export const DEFAULTS = {
  retry: {
    maxAttempts: 3, // TOTAL invocations incl. the first => at most 2 retries (R4 §1.2)
    strategy: 'full' as const, // exponential + full jitter (R4 §1.1)
    baseDelayMs: 100,
    factor: 2,
    maxDelayMs: 30_000, // cap applied BEFORE jitter (R4 §1.4)
    budgetMs: undefined, // derived from the remaining deadline when one exists
    retryNonIdempotentOnConnectFailure: true, // (R4 §1.3)
  },
  timeout: {
    attemptTimeoutMs: 10_000, // bounds ONE provider call
    totalTimeoutMs: 30_000, // bounds the whole operation
  },
  circuitBreaker: {
    mode: 'ratio' as const, // 'ratio' | 'consecutive'          (R4 §3.2)
    failureRatio: 0.5, // trips at >= 50%
    minimumThroughput: 10, // floor 2; guards against tripping on 1 request
    consecutiveFailureThreshold: 5, // mode 'consecutive' only; its own knob
    windowMs: 30_000,
    bucketMs: 1_000, // => 30 buckets; bounds memory      (R4 §3.2)
    resetMs: 10_000, // OPEN -> HALF_OPEN cooldown, lazy  (R4 §3.1)
    halfOpenMaxConcurrent: 1, // one probe at a time              (R4 §3.4)
    halfOpenSuccessesToClose: 1,
  },
  rateLimit: {
    capacity: 10, // max burst; bucket starts FULL     (R4 §4.5)
    refillPerSec: 10,
    onExhaustion: 'wait' as const, // 'wait' | 'reject'                (R4 §4.3)
    maxQueueDepth: 100, // mandatory bound
    maxQueueWaitMs: 30_000, // mandatory bound
  },
} as const;

/* ------------------------------------------------------------------ *
 * 5. Resolved shapes.
 *
 * The `exactOptionalPropertyTypes` convention (R1 §5.2): INPUT option types
 * declare `readonly x?: T | undefined`; RESOLVED config types declare
 * `readonly x: T`. That is what makes "unset" and "explicitly disabled" a
 * compile-time distinction instead of a runtime guess.
 * ------------------------------------------------------------------ */

export interface ResolvedRetryOptions {
  readonly maxAttempts: number;
  readonly strategy: BackoffStrategy;
  readonly baseDelayMs: number;
  readonly factor: number;
  readonly maxDelayMs: number;
  readonly budgetMs: number | undefined;
  readonly retryNonIdempotentOnConnectFailure: boolean;
}

export interface ResolvedTimeoutOptions {
  readonly attemptTimeoutMs: number;
  readonly totalTimeoutMs: number;
}

export interface ResolvedBreakerOptions {
  readonly mode: BreakerMode;
  readonly failureRatio: number;
  readonly minimumThroughput: number;
  readonly consecutiveFailureThreshold: number;
  readonly windowMs: number;
  readonly bucketMs: number;
  readonly resetMs: number;
  readonly halfOpenMaxConcurrent: number;
  readonly halfOpenSuccessesToClose: number;
}

export interface ResolvedRateLimitOptions {
  readonly capacity: number;
  readonly refillPerSec: number;
  readonly onExhaustion: RateLimitExhaustion;
  readonly maxQueueDepth: number;
  readonly maxQueueWaitMs: number;
  readonly cost: number;
}
