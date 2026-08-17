/**
 * U3 — CIRCUIT BREAKER. The unit's public face.
 *
 * `circuitBreaker` is the `Policy` factory the barrel needs; everything else is
 * the pure state machine, exported because a rolling-window breaker is worth
 * introspecting from telemetry and worth table-testing from outside.
 *
 * This unit imports from `src/core/**` and its own subtree, and nothing else.
 * Retry, rate limiting and timeouts compose with it through the `Policy`
 * interface, wired post-wave in `src/layer.ts`.
 */

export { circuitBreaker, DEFAULT_IS_FAILURE } from './breaker.ts';
export {
  type BreakerData,
  type BreakerEvent,
  type BreakerReducer,
  type BreakerTotals,
  type Bucket,
  canAdmit,
  createBreakerReducer,
  currentState,
  halfOpenAt,
  initialBreakerData,
  reduceBreaker,
  resolveBreakerOptions,
  windowTotals,
} from './state.ts';
