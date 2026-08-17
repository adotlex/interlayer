/**
 * U2 — RETRY & BACKOFF. The public face of this unit.
 *
 * `retryPolicy` is the factory the orchestrator wires into `src/layer.ts`; the
 * pure backoff curves and the default predicate are exported alongside it
 * because they are useful on their own and trivially testable.
 *
 * This barrel re-exports NOTHING from another unit. Retry composes with the
 * breaker, the limiter and the timeouts through the `Policy` interface only
 * (setup guide §2.1).
 */

export {
  type Backoff,
  type BackoffParams,
  backoffDelay,
  createBackoff,
  decorrelatedDelay,
  exponentialTerm,
} from './backoff.ts';
export {
  CONNECT_FAILURE_CODES,
  errorStatusOf,
  isConnectFailure,
  isTransient,
  passesIdempotencyGate,
  RETRY_FAILURES_KEY,
  RETRYABLE_STATUS_CODES,
  RETRYABLE_SYSCALL_CODES,
  type ResolvedRetryPolicyOptions,
  type RetryFailureRecord,
  type RetryPolicyOptions,
  resolveRetryOptions,
  retryAfterMsOf,
  retryFailures,
  retryPolicy,
} from './retry.ts';
