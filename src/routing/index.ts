/**
 * U6 — routing & fallback. The unit's public face.
 *
 * `src/index.ts` (the real barrel) is written by the orchestrator after the
 * wave; this file is what it re-exports from.
 *
 * This unit imports from `src/core/**` and from its own subtree, and from
 * nothing else — in particular nothing from `src/resilience/**` or
 * `src/registry/**`. It consumes the `Policy` INTERFACE and never an
 * implementation of one.
 */

export {
  abortErrorFor,
  defaultShouldFallback,
  type FallbackAttempt,
  type FallbackOptions,
  isCancellation,
  runFallbackChain,
  type ShouldFallback,
} from './fallback.ts';
export {
  type AttemptRunner,
  type CreateRouterOptions,
  createRouter,
  type PolicyRouter,
} from './router.ts';
export {
  byHints,
  chainSelectors,
  defaultSelector,
  filterByTags,
  inOrder,
  type RoundRobinOptions,
  roundRobin,
  type StickyOptions,
  sticky,
  type WeightedOptions,
  weighted,
} from './selectors.ts';
