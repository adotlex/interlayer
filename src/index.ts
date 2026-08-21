/**
 * THE PUBLISHED API of `interlayer`.
 *
 * Everything a consumer can import is named here, once, explicitly. There is no
 * `export *` from a unit: a barrel that re-exports blindly publishes whatever
 * the next commit happens to add, and the difference between "public API" and
 * "everything that happened to be exported" is the difference between a
 * versionable package and an accident.
 *
 * NO DEFAULT EXPORT (R6 §5.3): default exports break `import *` ergonomics and
 * rename badly at call sites.
 *
 * ── THE FOUR FUNCTIONS ────────────────────────────────────────────────────
 *
 * ```ts
 * import { capability, defineContract, defineProvider, createLayer } from 'interlayer';
 *
 * const ai = defineContract({ chat: capability<ChatRequest, ChatReply>() });
 * const openai = defineProvider(ai, { id: 'openai', capabilities: { chat: callOpenAi } });
 * const layer  = createLayer({ contract: ai, providers: [openai] });
 * const reply  = await layer.call('chat', { messages });
 * ```
 *
 * Deliberately NOT exported, in case their absence looks like an oversight:
 *
 *  - `toProviderRecord`, `ProviderLike` — the type-erasure seam. One call site,
 *    inside the registry; exposing it invites a second erasure site.
 *  - `createRouter`, `runFallbackChain`, `compose`, `pipeline` — the internal
 *    composition machinery `createLayer` is made of. `use()` and `definePolicy`
 *    are the supported ways in.
 *  - the pure algorithm internals (`backoffDelay`, token-bucket, breaker
 *    reducer). They are exported from their unit barrels for their own tests
 *    and would freeze implementation details into the public contract.
 *  - `map` / `andThen` / `fromPromise` and friends on `Result`. Names that
 *    generic do not belong at the top level of a package namespace; the type,
 *    its constructors and its guards are here, which is what `tryCall` needs.
 */

/* ------------------------------------------------------------------ *
 * 1. Contracts, capabilities, providers
 * ------------------------------------------------------------------ */

export type {
  AttemptContext,
  CallContext,
  CallOptions,
  CallResult,
  CallStats,
  Capability,
  CapabilityName,
  Contract,
  /**
   * Named by `ProviderRecord.capabilities`, which is what `layer.providers` and
   * every custom `Selector` are handed — squarely on the public path.
   */
  ErasedHandler,
  Handler,
  Handlers,
  HealthStatus,
  ImplementedBy,
  InputOf,
  Layer,
  OutputOf,
  Provider,
  ProviderId,
  ProviderRecord,
  ProviderTraits,
  RegisterOptions,
  Registrable,
  Registry,
  Selector,
} from './core/types.ts';
export type {
  CapabilityMeta,
  /**
   * Named by `ProviderDefinition.health`. Without it a consumer can pass a
   * health probe but cannot declare a shared one and annotate it — and under
   * `isolatedDeclarations` "cannot annotate" is "cannot re-export".
   */
  HealthProbe,
  ProviderDefinition,
  RegistryOptions,
} from './registry/index.ts';
export {
  capability,
  capabilityMeta,
  capabilityNames,
  createRegistry,
  declaresCapability,
  defineContract,
  defineProvider,
  implementedCapabilities,
} from './registry/index.ts';

/* ------------------------------------------------------------------ *
 * 2. The layer
 * ------------------------------------------------------------------ */

export type {
  ImplementedCapabilities,
  LayerConfig,
  PerCapabilityResilience,
  ProviderIds,
} from './layer.ts';
export { createLayer } from './layer.ts';

/* ------------------------------------------------------------------ *
 * 3. Errors — `code` is the discriminant; the classes exist for `instanceof`
 * ------------------------------------------------------------------ */

export type { AnyInterlayerError } from './core/errors.ts';
export {
  AllProvidersFailedError,
  BulkheadFullError,
  CancelledError,
  CircuitOpenError,
  ConfigError,
  formatErrorChain,
  hasCode,
  InterlayerError,
  isInterlayerError,
  NoProviderError,
  ProviderError,
  RateLimitedError,
  RetryExhaustedError,
  TimeoutError,
  TransportError,
  toInterlayerError,
  UnsupportedCapabilityError,
  ValidationError,
} from './core/errors.ts';
export type { ErrorCode, ErrorContext } from './core/types.ts';

/* ------------------------------------------------------------------ *
 * 4. Result — the non-throwing edge (`layer.tryCall`)
 * ------------------------------------------------------------------ */

export { err, isErr, isOk, ok, unwrap, unwrapOr } from './core/result.ts';
export type { Result } from './core/types.ts';

/* ------------------------------------------------------------------ *
 * 5. Events
 * ------------------------------------------------------------------ */

export type { InterlayerEmitter } from './core/events.ts';
export { createEmitter } from './core/events.ts';
export type {
  BreakerState,
  Emitter,
  EventMap,
  InterlayerEventName,
  InterlayerEvents,
  Unsubscribe,
} from './core/types.ts';

/* ------------------------------------------------------------------ *
 * 6. Determinism seams (C3) — inject a fake clock and the suite never sleeps
 * ------------------------------------------------------------------ */

export type { Clock, Random, Timers } from './core/clock.ts';
export { systemClock, systemRandom, systemTimers } from './core/clock.ts';
export { createSystemRuntime, systemRuntime } from './core/runtime.ts';
export type { DeadlineHandle, Runtime, TestRuntime } from './core/types.ts';

/* ------------------------------------------------------------------ *
 * 7. Resilience policies and their options
 * ------------------------------------------------------------------ */

export type {
  AttemptPolicy,
  BackoffStrategy,
  BreakerMode,
  BreakerOptions,
  CallPolicy,
  Policy,
  PolicyFactory,
  PolicyKind,
  PolicyScope,
  RateLimitExhaustion,
  RateLimitOptions,
  ResilienceOptions,
  ResolvedBreakerOptions,
  ResolvedRateLimitOptions,
  ResolvedRetryOptions,
  ResolvedTimeoutOptions,
  RetryOptions,
  TimeoutOptions,
} from './core/policy.ts';
export {
  DEFAULTS,
  definePolicy,
  orderPolicies,
  POLICY_ORDER,
  POLICY_SCOPE,
  policyStack,
} from './core/policy.ts';
export type { CallMiddleware, Middleware, Next, Terminal } from './core/types.ts';

export { circuitBreaker } from './resilience/circuit-breaker/index.ts';
export { type RateLimitPolicyOptions, rateLimit } from './resilience/rate-limit/index.ts';
export { isTransient, type RetryPolicyOptions, retryPolicy } from './resilience/retry/index.ts';
export { attemptTimeout, totalTimeout } from './resilience/timeout/index.ts';

/* ------------------------------------------------------------------ *
 * 8. Routing — selectors and the fallback rule
 * ------------------------------------------------------------------ */

export {
  chainSelectors,
  defaultSelector,
  defaultShouldFallback,
  filterByTags,
  inOrder,
  isCancellation,
  type RoundRobinOptions,
  roundRobin,
  type ShouldFallback,
  type StickyOptions,
  sticky,
  type WeightedOptions,
  weighted,
} from './routing/index.ts';

/* ------------------------------------------------------------------ *
 * 9. Validation combinators — zero-dependency, used for config and inputs
 * ------------------------------------------------------------------ */

export type { Infer, Issue, PathSegment, ValidationResult, Validator } from './core/types.ts';
/** Namespaced because `string()`, `number()` and `object()` cannot be top-level. */
export * as v from './core/validate.ts';
