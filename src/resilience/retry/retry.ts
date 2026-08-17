/**
 * THE RETRY POLICY — R4 §1, §5.5.
 *
 * A re-entrant piece of onion middleware: it calls `next()` up to
 * `maxAttempts` times, SEQUENTIALLY, sleeping a jittered backoff in between.
 * `compose()` deliberately relaxes koa's single-call guard so this is
 * expressible as an ordinary `Policy` (see `src/core/compose.ts`).
 *
 * ┌─ THE ONE THING TO GET RIGHT ────────────────────────────────────────────┐
 * │ `maxAttempts` is the TOTAL number of invocations INCLUDING THE FIRST.   │
 * │ `maxAttempts: 3` ⇒ at most 3 calls ⇒ at most 2 retries ⇒ at most 2      │
 * │ sleeps. This is cockatiel's meaning. It is deliberately NOT Polly's     │
 * │ `MaxRetryAttempts` ("retries in addition to the original call") and not │
 * │ p-retry's `retries`. R4 §1.2 settled it because the two major libraries │
 * │ genuinely disagree and an off-by-one here is the single most likely bug │
 * │ in this stream. Do not reinterpret it.                                  │
 * └─────────────────────────────────────────────────────────────────────────┘
 *
 * Determinism: every clock read, every sleep and every jitter draw goes through
 * `ctx.runtime`. Nothing here touches `Date.now`, `Math.random` or `setTimeout`.
 *
 * KNOWN GAP, REPORTED NOT WORKED AROUND: `src/core/errors.ts` has no
 * `RetryExhaustedError` and `ErrorCode` is a closed union with no
 * `'RETRY_EXHAUSTED'` member, so R4 §1.6's "aggregate at >= 2 failures" cannot
 * be expressed. This policy therefore throws the LAST error UNWRAPPED in every
 * exhaustion case — which also keeps `err.code` and `err.retryable` intact for
 * the routing layer above (guide §4/U6: wrapping buries the real error). The
 * full chronological error list is preserved on `ctx.state`; read it with
 * {@link retryFailures}.
 */

import { CancelledError, hasCode, isInterlayerError } from '../../core/errors.ts';
import type {
  Policy,
  PolicyFactory,
  ResolvedRetryOptions,
  RetryOptions,
} from '../../core/policy.ts';
import { DEFAULTS, definePolicy } from '../../core/policy.ts';
import type { AttemptContext, CallContext, Next } from '../../core/types.ts';
import { createBackoff } from './backoff.ts';

/* ------------------------------------------------------------------ *
 * 1. Options
 * ------------------------------------------------------------------ */

/**
 * `RetryOptions` (core, frozen) plus the one knob core cannot yet supply.
 *
 * Everything stays optional, so a `RetryOptions` value is assignable to this
 * and `PolicyFactory<RetryPolicyOptions>` is assignable to
 * `PolicyFactory<RetryOptions>`. The orchestrator can wire
 * `createLayer({ resilience: { retry } })` straight through.
 */
export interface RetryPolicyOptions extends RetryOptions {
  /**
   * Whether the operation is safe to repeat. R4 §1.3 puts this on the
   * capability descriptor, but `AttemptContext` carries only
   * `capability: string` — no descriptor, and `CallOptions` has no
   * `idempotencyKey` — so the policy cannot read it from the context today.
   * Until core grows that seam, it is a per-policy option. Default `true`.
   *
   * When `false`, retries are suppressed entirely EXCEPT for connection-level
   * failures the request provably never survived (`retryNonIdempotentOnConnectFailure`),
   * and NEVER on a timeout — a timeout is precisely the case where the write
   * may have landed invisibly.
   */
  readonly idempotent?: boolean | undefined;
}

/** {@link ResolvedRetryOptions} with every default filled in. */
export interface ResolvedRetryPolicyOptions extends ResolvedRetryOptions {
  readonly idempotent: boolean;
}

function requireIntegerAtLeast(name: string, value: number, min: number): void {
  if (!Number.isInteger(value) || value < min) {
    throw new RangeError(`${name} must be an integer >= ${min} (received ${String(value)})`);
  }
}

function requireNonNegative(name: string, value: number): void {
  if (typeof value !== 'number' || Number.isNaN(value) || value < 0) {
    throw new RangeError(`${name} must be a number >= 0 (received ${String(value)})`);
  }
}

function requirePositive(name: string, value: number): void {
  if (typeof value !== 'number' || Number.isNaN(value) || value <= 0) {
    throw new RangeError(`${name} must be a number > 0 (received ${String(value)})`);
  }
}

/**
 * Fills in {@link DEFAULTS}.retry and validates eagerly.
 *
 * Validation happens HERE — at construction — not on the first call, so a
 * misconfigured pipeline fails at wiring time rather than in production
 * (R4 §1.7 / RT-5: `maxAttempts: 0` is a `RangeError`, never a silent coercion).
 */
export function resolveRetryOptions(options: RetryPolicyOptions = {}): ResolvedRetryPolicyOptions {
  const resolved: ResolvedRetryPolicyOptions = {
    maxAttempts: options.maxAttempts ?? DEFAULTS.retry.maxAttempts,
    strategy: options.strategy ?? DEFAULTS.retry.strategy,
    baseDelayMs: options.baseDelayMs ?? DEFAULTS.retry.baseDelayMs,
    factor: options.factor ?? DEFAULTS.retry.factor,
    maxDelayMs: options.maxDelayMs ?? DEFAULTS.retry.maxDelayMs,
    budgetMs: options.budgetMs ?? DEFAULTS.retry.budgetMs,
    retryNonIdempotentOnConnectFailure:
      options.retryNonIdempotentOnConnectFailure ??
      DEFAULTS.retry.retryNonIdempotentOnConnectFailure,
    idempotent: options.idempotent ?? true,
  };

  requireIntegerAtLeast('maxAttempts', resolved.maxAttempts, 1);
  requireNonNegative('baseDelayMs', resolved.baseDelayMs);
  requirePositive('factor', resolved.factor);
  requireNonNegative('maxDelayMs', resolved.maxDelayMs);
  if (resolved.budgetMs !== undefined) requireNonNegative('budgetMs', resolved.budgetMs);

  return resolved;
}

/* ------------------------------------------------------------------ *
 * 2. The default retryable-error predicate — R4 §1.3
 * ------------------------------------------------------------------ */

/** HTTP-ish statuses worth another attempt (R4 §1.3 rule 7). */
export const RETRYABLE_STATUS_CODES: ReadonlySet<number> = new Set([408, 429, 500, 502, 503, 504]);

/** Node network error codes worth another attempt (R4 §1.3 rule 6). */
export const RETRYABLE_SYSCALL_CODES: ReadonlySet<string> = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'ETIMEDOUT',
  'EPIPE',
  'EAI_AGAIN',
  'ENOTFOUND',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'EBUSY',
]);

/**
 * Connection-level failures where the request PROVABLY never left the client.
 * An explicit opt-in list, not a general exemption (R4 §1.3).
 */
export const CONNECT_FAILURE_CODES: ReadonlySet<string> = new Set([
  'ECONNREFUSED',
  'ENOTFOUND',
  'EAI_AGAIN',
]);

function stringProp(error: unknown, key: 'code' | 'name'): string | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  const value = (error as Record<'code' | 'name', unknown>)[key];
  return typeof value === 'string' ? value : undefined;
}

/** The `status` / `statusCode` an HTTP-ish error carries, if any. */
export function errorStatusOf(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  const raw =
    (error as { status?: unknown }).status ?? (error as { statusCode?: unknown }).statusCode;
  return typeof raw === 'number' ? raw : undefined;
}

/** A server- or limiter-supplied `Retry-After` hint, in ms. */
export function retryAfterMsOf(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  const raw = (error as { retryAfterMs?: unknown }).retryAfterMs;
  return typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 ? raw : undefined;
}

/** True for the connect-level codes a non-idempotent call may still retry. */
export function isConnectFailure(error: unknown): boolean {
  const code = stringProp(error, 'code');
  return code !== undefined && CONNECT_FAILURE_CODES.has(code);
}

/**
 * The default `isRetryable`, in R4 §1.3's exact order. The ORDER is what makes
 * it correct:
 *
 *  1. `CancelledError` (and a bare `AbortError`) — never. The caller asked us
 *     to stop.
 *  2. `CircuitOpenError` — never. Spinning against an open breaker is pure
 *     waste; the routing layer above should move to another provider. This
 *     MUST come before the `retryable` flag check, because core constructs
 *     `CircuitOpenError` with `retryable: true` (it is retryable *elsewhere*,
 *     not *here*).
 *  3/4. An explicit `retryable` flag on an Interlayer error wins.
 *  5. A foreign `TimeoutError` (e.g. a `DOMException` from an attempt timeout).
 *  6. Node network error codes.
 *  7. HTTP-ish status.
 *  8. Everything else: NO. A `TypeError` in our own code must not run 3x.
 */
export function isTransient(error: unknown, _attempt: number): boolean {
  const name = stringProp(error, 'name');
  if (hasCode(error, 'CANCELLED') || name === 'AbortError') return false;
  if (hasCode(error, 'CIRCUIT_OPEN')) return false;
  if (isInterlayerError(error)) return error.retryable;
  if (name === 'TimeoutError') return true;

  const code = stringProp(error, 'code');
  if (code !== undefined && RETRYABLE_SYSCALL_CODES.has(code)) return true;

  const status = errorStatusOf(error);
  if (status !== undefined) return RETRYABLE_STATUS_CODES.has(status);

  return false;
}

/**
 * The idempotency gate, applied BEFORE the retryable predicate (R4 §1.3).
 * An idempotent operation passes straight through.
 */
export function passesIdempotencyGate(
  error: unknown,
  options: Pick<ResolvedRetryPolicyOptions, 'idempotent' | 'retryNonIdempotentOnConnectFailure'>,
): boolean {
  if (options.idempotent) return true;
  // NEVER on a timeout: the write may have succeeded invisibly.
  if (hasCode(error, 'TIMEOUT') || stringProp(error, 'name') === 'TimeoutError') return false;
  return options.retryNonIdempotentOnConnectFailure && isConnectFailure(error);
}

/* ------------------------------------------------------------------ *
 * 3. The per-call failure record kept on `ctx.state`
 * ------------------------------------------------------------------ */

/** Key under which the retry loop parks its chronological error list. */
export const RETRY_FAILURES_KEY = 'interlayer.retry.failures';

export interface RetryFailureRecord {
  readonly providerId: string;
  /** Physical invocations made by THIS retry loop. */
  readonly attempts: number;
  /** Every error, in chronological order. `.at(-1)` is the one that was thrown. */
  readonly errors: readonly unknown[];
  /** Errors thrown BY a buggy `isRetryable` predicate (R4 §1.7). Normally empty. */
  readonly predicateErrors: readonly unknown[];
}

/**
 * Reads back what the last retry loop on this call recorded.
 *
 * This exists because core has no `RetryExhaustedError` to carry the earlier
 * errors (see the file header). Discarding them would be a debuggability
 * regression: the first failure is often the real one and the later ones are
 * `ECONNREFUSED` from a now-dead process.
 */
export function retryFailures(ctx: Pick<CallContext, 'state'>): RetryFailureRecord | undefined {
  const value = ctx.state.get(RETRY_FAILURES_KEY);
  if (typeof value !== 'object' || value === null) return undefined;
  return value as RetryFailureRecord;
}

/* ------------------------------------------------------------------ *
 * 4. The policy
 * ------------------------------------------------------------------ */

function cancellationFrom(ctx: AttemptContext, message: string): unknown {
  const reason: unknown = ctx.signal.reason;
  if (isInterlayerError(reason)) return reason;
  return new CancelledError(message, {
    callId: ctx.callId,
    capability: ctx.capability,
    providerId: ctx.provider.id,
    attempt: ctx.attempt,
    cause: reason,
  });
}

/**
 * Builds the retry `Policy`.
 *
 * Throws `RangeError` at CONSTRUCTION for an invalid `maxAttempts` — R4 §1.7
 * is explicit that this is not deferred to call time.
 */
export const retryPolicy: PolicyFactory<RetryPolicyOptions> = (
  options?: RetryPolicyOptions,
): Policy<AttemptContext, unknown> => {
  const resolved = resolveRetryOptions(options);
  const predicate = options?.isRetryable ?? isTransient;

  const execute = async (
    ctx: AttemptContext,
    next: Next<AttemptContext, unknown>,
  ): Promise<unknown> => {
    const { runtime } = ctx;
    const startedAt = runtime.now();
    const baseAttempt = ctx.attempt;
    const backoff = createBackoff(resolved, runtime.random);
    const errors: unknown[] = [];
    const predicateErrors: unknown[] = [];

    // R4 §1.4: `budgetMs` is the cooperative optimisation; a total timeout
    // sitting outside this loop is the pre-emptive guarantee. When no explicit
    // budget is configured, derive one from the call deadline so the loop never
    // sleeps into a deadline it cannot meet.
    const budgetMs =
      resolved.budgetMs ?? (ctx.deadlineAt === undefined ? undefined : ctx.deadlineAt - startedAt);

    const shouldRetry = (error: unknown, attempt: number): boolean => {
      if (!passesIdempotencyGate(error, resolved)) return false;
      try {
        return predicate(error, attempt);
      } catch (predicateError) {
        // R4 §1.7: a buggy predicate must NEVER mask the real failure. Treat it
        // as "not retryable" and keep the original error on the throw path.
        predicateErrors.push(predicateError);
        return false;
      }
    };

    for (let n = 1; ; n++) {
      // RT-17: abort before the first attempt ⇒ zero invocations.
      if (ctx.signal.aborted) throw cancellationFrom(ctx, 'Aborted before attempt');

      try {
        // `next(ctx')` REPLACES the downstream context (never mutates it), so
        // the attempt number advances without touching shared state.
        return await next(n === 1 ? undefined : { ...ctx, attempt: baseAttempt + n - 1 });
      } catch (error) {
        // RT-19: a cancellation is rethrown unwrapped and is never retried,
        // never aggregated.
        if (hasCode(error, 'CANCELLED')) throw error;
        if (ctx.signal.aborted) throw cancellationFrom(ctx, 'Aborted during attempt');

        errors.push(error);

        // TOTAL-attempts semantics (R4 §1.2). `n` counts invocations, so
        // `n >= maxAttempts` means we have already made them all.
        if (n >= resolved.maxAttempts) break;
        if (!shouldRetry(error, n)) break;

        const computed = backoff.next(n);
        // R4 §1.3: a server-supplied `Retry-After` beats our curve, but is
        // still bounded by the cap.
        const hinted = retryAfterMsOf(error);
        const delayMs =
          hinted === undefined
            ? computed
            : Math.min(resolved.maxDelayMs, Math.max(computed, hinted));

        if (budgetMs !== undefined) {
          const remaining = budgetMs - (runtime.now() - startedAt);
          // Stop NOW rather than sleep into a deadline we cannot meet.
          if (remaining <= 0 || delayMs >= remaining) break;
        }

        ctx.events.emit('retry:scheduled', {
          callId: ctx.callId,
          providerId: ctx.provider.id,
          // The attempt this sleep is scheduling, i.e. the UPCOMING one.
          attempt: baseAttempt + n,
          delayMs,
          at: runtime.now(),
        });

        // Interruptible (R4 §1.5 / §5.1): rejects with `CancelledError` and
        // CLEARS the timer if the signal aborts mid-backoff. An uncleared timer
        // holds the event loop open for its full duration — verified by R4.
        await runtime.sleep(delayMs, ctx.signal);
      }
    }

    const record: RetryFailureRecord = {
      providerId: ctx.provider.id,
      attempts: errors.length,
      errors,
      predicateErrors,
    };
    ctx.state.set(RETRY_FAILURES_KEY, record);

    // The LAST error, UNWRAPPED — see the file header for why this is not
    // `RetryExhaustedError`.
    throw errors.at(-1);
  };

  return definePolicy<AttemptContext, unknown>({
    kind: 'retry',
    name: `retry(${resolved.strategy}, ${resolved.maxAttempts})`,
    scope: 'attempt',
    execute,
    describe: (): Readonly<Record<string, unknown>> => ({ ...resolved }),
  });
};
