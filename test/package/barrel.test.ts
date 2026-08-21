/**
 * THE PUBLISHED SURFACE — barrel completeness and internal leakage.
 *
 * `src/index.ts` is the whole contract with the outside world. Everything under
 * it is an implementation detail that may be rewritten between patch releases;
 * everything named in it is frozen until a major. The two failure modes are
 * symmetrical and both silent:
 *
 *  - a symbol a consumer needs never reaches the barrel — the library is
 *    unusable for that case and nobody notices, because the unit tests import
 *    from the unit;
 *  - a symbol nobody meant to publish reaches it — and now it is API, because
 *    someone out there imported it.
 *
 * These tests are the inventory that makes both loud. They read the barrels
 * with the TypeScript checker (see `reflect.ts`), so TYPES count as surface,
 * which is most of it: 141 exported names, of which only 61 exist at runtime.
 */

import { describe, expect, it } from 'vitest';
import {
  exportsOf,
  hasDefaultExport,
  ROOT_BARREL,
  UNIT_BARRELS,
  unreachablePublicTypes,
} from './reflect.ts';

/**
 * THE API LOCKFILE. Every name a consumer can import from `interlayer`.
 *
 * A diff here is never a test failure to "just update" — it is either an
 * addition that needs a minor version and a doc entry, or a removal that needs
 * a major. Reproduce with:
 *   `node -e "import('./dist/index.js').then(m => console.log(Object.keys(m).sort()))"`
 * for the runtime half; the type half only the checker can see.
 */
const PUBLIC_API: readonly string[] = [
  'AllProvidersFailedError',
  'AnyInterlayerError',
  'AttemptContext',
  'AttemptPolicy',
  'BackoffStrategy',
  'BreakerMode',
  'BreakerOptions',
  'BreakerState',
  'BulkheadFullError',
  'CallContext',
  'CallMiddleware',
  'CallOptions',
  'CallPolicy',
  'CallResult',
  'CallStats',
  'CancelledError',
  'Capability',
  'CapabilityMeta',
  'CapabilityName',
  'CircuitOpenError',
  'Clock',
  'ConfigError',
  'Contract',
  'DEFAULTS',
  'DeadlineHandle',
  'Emitter',
  'ErasedHandler',
  'ErrorCode',
  'ErrorContext',
  'EventMap',
  'Handler',
  'Handlers',
  'HealthProbe',
  'HealthStatus',
  'ImplementedBy',
  'ImplementedCapabilities',
  'Infer',
  'InputOf',
  'InterlayerEmitter',
  'InterlayerError',
  'InterlayerEventName',
  'InterlayerEvents',
  'Issue',
  'Layer',
  'LayerConfig',
  'Middleware',
  'Next',
  'NoProviderError',
  'OutputOf',
  'POLICY_ORDER',
  'POLICY_SCOPE',
  'PathSegment',
  'PerCapabilityResilience',
  'Policy',
  'PolicyFactory',
  'PolicyKind',
  'PolicyScope',
  'Provider',
  'ProviderDefinition',
  'ProviderError',
  'ProviderId',
  'ProviderIds',
  'ProviderRecord',
  'ProviderTraits',
  'Random',
  'RateLimitExhaustion',
  'RateLimitOptions',
  'RateLimitPolicyOptions',
  'RateLimitedError',
  'RegisterOptions',
  'Registrable',
  'Registry',
  'RegistryOptions',
  'ResilienceOptions',
  'ResolvedBreakerOptions',
  'ResolvedRateLimitOptions',
  'ResolvedRetryOptions',
  'ResolvedTimeoutOptions',
  'Result',
  'RetryExhaustedError',
  'RetryOptions',
  'RetryPolicyOptions',
  'RoundRobinOptions',
  'Runtime',
  'Selector',
  'ShouldFallback',
  'StickyOptions',
  'Terminal',
  'TestRuntime',
  'TimeoutError',
  'TimeoutOptions',
  'Timers',
  'TransportError',
  'Unsubscribe',
  'UnsupportedCapabilityError',
  'ValidationError',
  'ValidationResult',
  'Validator',
  'WeightedOptions',
  'attemptTimeout',
  'capability',
  'capabilityMeta',
  'capabilityNames',
  'chainSelectors',
  'circuitBreaker',
  'createEmitter',
  'createLayer',
  'createRegistry',
  'createSystemRuntime',
  'declaresCapability',
  'defaultSelector',
  'defaultShouldFallback',
  'defineContract',
  'definePolicy',
  'defineProvider',
  'err',
  'filterByTags',
  'formatErrorChain',
  'hasCode',
  'implementedCapabilities',
  'inOrder',
  'isCancellation',
  'isErr',
  'isInterlayerError',
  'isOk',
  'isTransient',
  'ok',
  'orderPolicies',
  'policyStack',
  'rateLimit',
  'retryPolicy',
  'roundRobin',
  'sticky',
  'systemClock',
  'systemRandom',
  'systemRuntime',
  'systemTimers',
  'toInterlayerError',
  'totalTimeout',
  'unwrap',
  'unwrapOr',
  'v',
  'weighted',
];

/**
 * THE WITHHELD INVENTORY — every name a unit barrel exports that the published
 * barrel deliberately does not, grouped by the reason given in the barrel's own
 * header comment.
 *
 * The point of enumerating all 87 rather than spot-checking a few: a new export
 * added to a unit barrel lands in NEITHER list and fails
 * `every unit-barrel export is either published or deliberately withheld`. That
 * is the whole guard — it converts "nobody thought about it" into a red test.
 */
const WITHHELD: Readonly<Record<string, readonly string[]>> = {
  /**
   * Barrel bullet 1 — the type-erasure seam. One call site, in the registry.
   *
   * `ErasedHandler` used to be here and is now PUBLISHED: it is the value type
   * of `ProviderRecord.capabilities`, and `ProviderRecord` is what
   * `layer.providers` and every custom `Selector` are handed. Withholding the
   * name did not hide the seam, it just stopped consumers writing down a type
   * the library hands them. The *functions* that perform the erasure stay
   * withheld, which is what the bullet is actually protecting.
   */
  erasureSeam: ['ProviderLike', 'ResolvedRegistration', 'toProviderRecord'],

  /** Barrel bullet 2 — the composition machinery `createLayer` is made of. */
  composition: [
    'AttemptMiddleware',
    'AttemptRunner',
    'Composed',
    'CreateRouterOptions',
    'FallbackAttempt',
    'FallbackOptions',
    'PolicyRouter',
    'Router',
    'RouterOptions',
    'abortErrorFor',
    'compose',
    'createRouter',
    'disposePolicies',
    'pipeline',
    'policyRank',
    'runFallbackChain',
  ],

  /** Barrel bullet 3 — pure algorithm internals, exported for their own tests. */
  algorithms: [
    // retry / backoff curves
    'Backoff',
    'BackoffParams',
    'CONNECT_FAILURE_CODES',
    'RETRYABLE_STATUS_CODES',
    'RETRYABLE_SYSCALL_CODES',
    'ResolvedRetryPolicyOptions',
    'backoffDelay',
    'createBackoff',
    'decorrelatedDelay',
    'errorStatusOf',
    'exponentialTerm',
    'isConnectFailure',
    'passesIdempotencyGate',
    'resolveRetryOptions',
    'retryAfterMsOf',
    // circuit-breaker reducer
    'BreakerData',
    'BreakerEvent',
    'BreakerReducer',
    'BreakerTotals',
    'Bucket',
    'DEFAULT_IS_FAILURE',
    'canAdmit',
    'createBreakerReducer',
    'currentState',
    'halfOpenAt',
    'initialBreakerData',
    'reduceBreaker',
    'resolveBreakerOptions',
    'windowTotals',
    // token bucket
    'DEFAULT_COST',
    'TokenBucketConfig',
    'TokenBucketOutcome',
    'TokenBucketState',
    'assertValidBucketConfig',
    'consume',
    'createTokenBucket',
    'isSatisfiable',
    'msUntil',
    'refill',
    'waitMsFor',
    // deadline arithmetic
    'ComposeDeadlineInput',
    'DeadlineComposition',
    'DeadlineSource',
    'TimeoutPatch',
    'UNBOUNDED_MS',
    'assertTimeoutMs',
    'composeDeadline',
    'deadlineAtFrom',
    'describeMs',
    'earliestDeadline',
    'isExpired',
    'remainingMs',
    'resolveTimeoutOptions',
    'tighterTimeoutMs',
  ],

  /** Barrel bullet 4 — `Result` combinators too generically named for a namespace. */
  resultCombinators: ['andThen', 'fromPromise', 'fromThrowable', 'map', 'mapError', 'unwrapOrElse'],

  /**
   * Withheld, but NOT named in the barrel's header comment. Each is a decision
   * this suite has audited rather than a name that slipped through:
   *
   *  - `AnyContract` — the erased, index-signature contract form. Appears only
   *    as the DEFAULT type argument of `createRegistry`, which a consumer never
   *    has to write; core marks it "internal use only; never a public
   *    constraint" and the F-bounded `Contract<C>` is the public one.
   *  - `byHints` — the router already prepends it
   *    (`chainSelectors(byHints(), …)` in `routing/router.ts`), so hints are
   *    applied whether or not the caller passes a selector. Publishing it would
   *    invite applying it twice.
   *  - `supports` — the standalone `(provider, name) => boolean` helper.
   *    `Layer.supports()` is the consumer-facing form, and the public
   *    `implementedCapabilities` rebuilds this in one line.
   *  - `RETRY_FAILURES_KEY` / `RetryFailureRecord` / `retryFailures` — the retry
   *    loop's per-call failure record. `retry.ts` documents it as being for
   *    "middleware and telemetry", and `layer.use(middleware)` is public, so
   *    consumer middleware is the intended reader and cannot import it.
   *    Advisory: `CallResult.errors` covers the common case.
   */
  auditedButUndocumented: [
    'AnyContract',
    'RETRY_FAILURES_KEY',
    'RetryFailureRecord',
    'byHints',
    'retryFailures',
    'supports',
  ],
};

/**
 * The names the barrel's header comment calls out by name. Verifying the prose
 * matters: a withholding note that has drifted from the code is worse than no
 * note, because it is believed.
 */
const DOCUMENTED_WITHHOLDINGS: readonly string[] = [
  'toProviderRecord',
  'ProviderLike',
  'createRouter',
  'runFallbackChain',
  'compose',
  'pipeline',
  'backoffDelay',
  'map',
  'andThen',
  'fromPromise',
];

const rootExports = exportsOf(ROOT_BARREL);
const unitExports = new Map(UNIT_BARRELS.map((barrel) => [barrel, exportsOf(barrel)] as const));
const allUnitExports = new Set([...unitExports.values()].flat());
const withheldNames = new Set(Object.values(WITHHELD).flat());

describe('the published barrel', () => {
  it('exports exactly the locked public API', () => {
    expect(rootExports).toEqual([...PUBLIC_API].sort());
  });

  it('has no default export', () => {
    expect(hasDefaultExport(ROOT_BARREL)).toBe(false);
  });

  it('publishes the four headline functions', () => {
    // The barrel's own usage example. If any of these four moves, the README,
    // the doc comment and every getting-started snippet are wrong at once.
    expect(rootExports).toEqual(
      expect.arrayContaining(['capability', 'defineContract', 'defineProvider', 'createLayer']),
    );
  });

  it('names every export exactly once', () => {
    expect(new Set(rootExports).size).toBe(rootExports.length);
  });
});

describe('unit barrels', () => {
  it.each([...unitExports.keys()])('%s exports at least one name', (barrel) => {
    expect((unitExports.get(barrel) ?? []).length).toBeGreaterThan(0);
  });

  it('are collision-free — no name is exported by two units', () => {
    const owners = new Map<string, string[]>();
    for (const [barrel, names] of unitExports) {
      for (const name of names) owners.set(name, [...(owners.get(name) ?? []), barrel]);
    }
    const collisions = [...owners].filter(([, barrels]) => barrels.length > 1);
    expect(collisions).toEqual([]);
  });

  it('never export a `default`', () => {
    const withDefault = UNIT_BARRELS.filter((barrel) => hasDefaultExport(barrel));
    expect(withDefault).toEqual([]);
  });
});

describe('completeness — everything reachable is accounted for', () => {
  it('every unit-barrel export is either published or deliberately withheld', () => {
    const unaccounted = [...allUnitExports]
      .filter((name) => !rootExports.includes(name) && !withheldNames.has(name))
      .sort();
    // A name here is a NEW export in a unit barrel that nobody has decided
    // about. Publish it (add to PUBLIC_API and to src/index.ts) or withhold it
    // (add to WITHHELD, with the reason).
    expect(unaccounted).toEqual([]);
  });

  it('the withheld inventory has no stale entries', () => {
    const stale = [...withheldNames].filter((name) => !allUnitExports.has(name)).sort();
    expect(stale).toEqual([]);
  });

  it('withheld and published are disjoint', () => {
    const both = [...withheldNames].filter((name) => rootExports.includes(name)).sort();
    expect(both).toEqual([]);
  });

  it('accounts for every published name that comes from a unit barrel', () => {
    // The five that do not: `src/layer.ts` has no unit barrel of its own.
    const rootOnly = rootExports.filter((name) => !allUnitExports.has(name));
    expect(rootOnly).toEqual([
      'ImplementedCapabilities',
      'LayerConfig',
      'PerCapabilityResilience',
      'ProviderIds',
      'createLayer',
    ]);
  });
});

describe('no internal leakage', () => {
  it.each(DOCUMENTED_WITHHOLDINGS)('%s is documented as withheld and really is', (name) => {
    expect(rootExports).not.toContain(name);
  });

  it.each(DOCUMENTED_WITHHOLDINGS)('%s exists, so the note is not stale', (name) => {
    // A withholding note naming a symbol that no longer exists reads as a
    // deliberate decision when it is really a leftover.
    expect(allUnitExports.has(name)).toBe(true);
  });

  it.each(Object.keys(WITHHELD))('the %s group is entirely absent from the barrel', (group) => {
    const leaked = (WITHHELD[group] ?? []).filter((name) => rootExports.includes(name));
    expect(leaked).toEqual([]);
  });

  it('publishes no name matching an internal naming convention', () => {
    // Belt and braces for a future export: nothing prefixed `_`, nothing
    // suffixed `Internal`, no `Erased*` and no `Any*` — each with exactly one
    // DELIBERATE exception, listed here so a genuinely accidental leak still
    // fails this test:
    //
    //   `AnyInterlayerError` — the public union of the error classes.
    //   `ErasedHandler`      — the value type of `ProviderRecord.capabilities`.
    //                          `ProviderRecord` is public (it is what
    //                          `layer.providers` and every `Selector` receive),
    //                          so a consumer writing a selector has to be able
    //                          to name what they are handed. The heuristic is
    //                          about names nobody decided on; this one was
    //                          decided on.
    const intentional = new Set(['AnyInterlayerError', 'ErasedHandler']);
    const suspicious = rootExports.filter(
      (name) =>
        !intentional.has(name) &&
        (name.startsWith('_') ||
          name.endsWith('Internal') ||
          name.startsWith('Erased') ||
          name.startsWith('Any')),
    );
    expect(suspicious).toEqual([]);
  });
});

describe('the published surface is self-contained', () => {
  /**
   * FAILING — documents BUG-T6-02.
   *
   * A public type whose signature names an unexported type hands the consumer a
   * value they cannot write down. Under `isolatedDeclarations` (which this
   * package requires of itself and which any modern consumer may also enable)
   * "cannot write down" escalates to "cannot re-export", so this is not merely
   * an ergonomic wart.
   *
   * Two holes, both one `export type` line from being closed:
   *
   *   ProviderDefinition.health?: HealthProbe
   *     — `ProviderDefinition` IS published; `HealthProbe` is not, so nobody can
   *       declare a shared health probe and annotate it. Note that core's own
   *       `ProviderRecord.health` INLINES the same function type rather than
   *       naming it, which is why this was never noticed.
   *
   *   ProviderRecord.capabilities: ReadonlyMap<string, ErasedHandler>
   *     — `ProviderRecord` is what `layer.providers` and every custom `Selector`
   *       receive, so it is squarely on the public path.
   *
   * `AnyContract` is accepted below: it appears ONLY as the default type
   * argument of `createRegistry<C extends Contract<C> = AnyContract>`, which a
   * consumer never has to name.
   */
  it('names no type a consumer cannot import', () => {
    const accepted = new Set(['AnyContract']);
    const leaks = unreachablePublicTypes().filter((leak) => !accepted.has(leak.type));
    expect(leaks).toEqual([]);
  });
});
