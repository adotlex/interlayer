/**
 * U5 — timeout & deadline. The unit's public face.
 *
 * ```ts
 * import { attemptTimeout, totalTimeout } from './resilience/timeout/index.ts';
 *
 * const policies = [
 *   totalTimeout({ totalTimeoutMs: 30_000 }),   // scope 'call',    outermost
 *   attemptTimeout({ attemptTimeoutMs: 10_000 }), // scope 'attempt', innermost
 * ];
 * ```
 *
 * Both factories return a `Policy` from `src/core/policy.ts` and nothing else:
 * this unit imports core only, and no other unit imports this one. Wiring into
 * the canonical order happens post-wave in `src/layer.ts`.
 */

export {
  assertTimeoutMs,
  type ComposeDeadlineInput,
  composeDeadline,
  type DeadlineComposition,
  type DeadlineSource,
  deadlineAtFrom,
  describeMs,
  earliestDeadline,
  isExpired,
  remainingMs,
  resolveTimeoutOptions,
  tighterTimeoutMs,
  UNBOUNDED_MS,
} from './deadline.ts';
export { attemptTimeout, type TimeoutPatch, totalTimeout } from './timeout.ts';
