/**
 * U4 — rate limiting. The unit's public face.
 *
 * `rateLimit()` is the `Policy` factory the barrel (`src/index.ts`, ORCH-POST)
 * re-exports; the pure token bucket is exported alongside it because it is
 * useful on its own and because it is where the algorithm actually lives.
 */

export { type RateLimitPolicyOptions, rateLimit } from './rate-limit.ts';
export {
  assertValidBucketConfig,
  consume,
  createTokenBucket,
  DEFAULT_COST,
  isSatisfiable,
  msUntil,
  refill,
  type TokenBucketConfig,
  type TokenBucketOutcome,
  type TokenBucketState,
  waitMsFor,
} from './token-bucket.ts';
