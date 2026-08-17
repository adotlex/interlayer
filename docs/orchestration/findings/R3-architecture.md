# R3 — Core Architecture & Type Design

**Status:** complete. **Verified:** every literal type and function in §5 was compiled under
`tsc 6.0.2` with `strict + exactOptionalPropertyTypes + noUncheckedIndexedAccess` (0 errors) and
executed as a 9-block runtime smoke suite (all pass). This is not a sketch — it ran.

> Prototype lives in the scratchpad, not the repo, per instructions. Wave 2 transcribes §5.

---

## 0. Executive summary

| Question | Decision |
|---|---|
| Primitives | `Contract`, `Provider`, `Registry`, `Selector`, `Router`, `Middleware`, `CallContext`/`AttemptContext`, `Runtime`, `Emitter`, `Validator` |
| Capability model | Literal **string keys** indexed into an **F-bounded** contract type; optional branded `CapabilityRef` token |
| Errors | **Both** — class hierarchy *whose instances form a discriminated union* via a literal `code`; `invoke` throws, `tryInvoke` returns `Result` |
| Composition | **Koa-style onion**, with two deliberate deviations: `next()` returns the value, and `next()` is re-entrant |
| Validation | Hand-rolled combinators (~184 LOC), `Infer<>`, path-qualified messages, assertion functions |
| Determinism | One `Runtime` object injected at construction **and** carried on every context |
| Observability | Zero-dep typed emitter over a `type` (not `interface`) event map |
| Decomposition | 2 pre-seeded frozen files + **6 disjoint build units**, 24 files |

---

## 1. Options considered

### 1.1 Capability / provider model

| Option | Call-site inference | Runtime selection | Config & telemetry | Cross-package | Verdict |
|---|---|---|---|---|---|
| **String keys → contract type map** | Full (`invoke('chat.complete', in)` infers `ChatOut`) | Trivial (`Map<string, …>`) | Serializable everywhere | Needs shared type import | **CHOSEN** |
| Symbol keys | Full | Trivial | **Not serializable** — dead in JSON config, log labels, metrics dimensions | Collision-free | Rejected |
| Structural typing (provider = object of methods) | Excellent | **Poor** — "who can do X?" needs key reflection; no priority/traits metadata | n/a | n/a | Rejected |
| Branded capability tokens | Full, without threading `C` | Trivial (brand erases to string) | Serializable | **Best** | **CHOSEN as secondary** |
| Runtime schema registry (zod-style) | Full | Trivial | Serializable | Good | Rejected under C4 |

### 1.2 Contract declaration form — *a trap we hit and measured*

| Declaration | Satisfies `C extends Record<string, CapabilitySpec>` | `keyof C` | Safe? |
|---|---|---|---|
| `interface X extends Contract {…}` | yes | **widens to `string`** | **SILENTLY BROKEN** |
| `interface X {…}` (no extends) | **no** — interfaces have no implicit index signature | literal union | won't compile |
| `type X = {…}` | yes | literal union | works |
| any of the above with **F-bounded** `C extends Contract<C>` | yes | literal union | **CHOSEN** |

Row 1 is the important one. `interface AiContract extends Contract` inherits the string index
signature, so `CapabilityKey<C>` collapses to `string` and `il.invoke('typo-here', {})`
**compiles clean**. The entire value proposition evaporates with no error. Measured directly:
with the naive constraint a `@ts-expect-error` on a bogus capability name reported
`TS2578: Unused '@ts-expect-error' directive` — i.e. no error was raised. The F-bounded form
`type Contract<C> = { readonly [K in keyof C]: CapabilitySpec }` imposes the same shape,
preserves literal keys, **and** accepts `interface` and `type` alike.

### 1.3 Error model

| Option | Ergonomics | Testability | Exhaustiveness | Cause chain | Verdict |
|---|---|---|---|---|---|
| Class hierarchy only | `try/catch` + `instanceof` natural | `instanceof` breaks across realms / duplicate installs | No | Native `cause` | Partial |
| Discriminated union `Result` only | Forces handling; verbose; every provider throw must be caught & converted | Great — plain data | Yes | Manual | Partial |
| **Both: classes with literal `code`, thrown internally, `Result` at the edge** | Best of both | Assert on `code` (string, realm-proof) | Yes, via `switch (err.code)` | Native `cause` + `AggregateError`-style `errors[]` | **CHOSEN** |
| `Effect`-style typed error channel | Excellent | Excellent | Yes | Yes | Rejected — needs Effect (C4) |

Prior art: [Effect's `Data.TaggedError`](https://effect.website/docs/error-management/expected-errors/)
uses a `_tag` discriminant so errors narrow precisely; we use `code` for the same reason but keep
real `Error` subclasses so `try/catch`, stack traces and Node's `cause` printing all still work.
[Cockatiel](https://github.com/connor4312/cockatiel) ships plain classes
(`BrokenCircuitError`, `TaskCancelledError`, `BulkheadRejectedError`) — good names, no discriminant,
so consumers are stuck with `instanceof` chains. The AI SDK's `AISDKError` uses a
`Symbol.for()` marker + static `isInstance` precisely because `instanceof` is unreliable when two
copies of a package are installed; we copy that and add the discriminant Cockatiel lacks.

### 1.4 Middleware composition

| Option | Ordering clarity | Retry expressible? | Typed result | Cancellation scoping | Verdict |
|---|---|---|---|---|---|
| Function wrapping / decorators (`retry(timeout(fn))`) | Reads inside-out | Yes | Yes | Yes | Rejected — no shared context, no uniform hook point |
| Explicit pipeline array of `(input) => output` transforms | Flat | **No** — cannot re-run downstream | Yes | No | Rejected |
| Cockatiel-style `Policy.wrap(a, b, c).execute(fn)` | Good | Yes | Yes | Yes (`execute(fn, signal)`) | Rejected — a *second* composition mechanism alongside middleware; C1 wants exactly one |
| **Koa onion `(ctx, next)`** | Explicit array order, before/after in one function | Only with a relaxed guard (below) | Only if `next()` returns the value | Only if `next()` can replace ctx | **CHOSEN, with 2 deviations** |

**Deviation 1 — `next()` returns the downstream value.** Koa's `next()` resolves to `void`;
the result is smuggled through `ctx.body`. That is untypeable. Ours is `Next<Ctx,R> = (ctx?) => Promise<R>`.

**Deviation 2 — `next()` is re-entrant.** [koa-compose 4.1.0](https://github.com/koajs/compose)
guards with `if (i <= index) return Promise.reject(new Error('next() called multiple times'))`.
That guard makes `retry` **impossible** as middleware — retry's whole job is to call downstream
again. Cockatiel dodges this by using `execute(fn)` wrappers instead of an onion. We keep the onion
and relax the guard to *sequential re-entry allowed, concurrent re-entry rejected*. This still
catches koa's real target bug (the missing `await`), while making retry ordinary middleware. Verified
in the smoke suite: a 3-iteration retry loop over `next()` succeeds; `{ void next(); return next(); }`
rejects with a `ConfigError`.

**Deviation 3 (consequence) — `next(replacementCtx)`.** Koa passes one `ctx` object down the whole
chain. `timeout` must give *downstream only* a narrower `AbortSignal`, and `retry` must bump
`attempt` per iteration. Optional context replacement solves both without mutation. Shared mutable
state (`stats`, `failures`, `state`) is held behind object references so it survives `{...ctx}`.

### 1.5 Config validation without zod

| Option | Error messages | Type narrowing | LOC | Verdict |
|---|---|---|---|---|
| Bare type predicates `is X` | Boolean only — "invalid config" | Yes | ~0 | Rejected — unusable messages |
| Assertion functions, hand-written per shape | Good but duplicated per field | Yes | O(n) forever | Rejected |
| **Combinator validators + `Infer<>` + assertion terminals** | Path-qualified per issue, all issues at once | Yes, inferred from the schema | ~184 | **CHOSEN** |
| Depend on zod/valibot | Excellent | Excellent | 0 | **Rejected — violates C4** |
| Accept any [Standard Schema](https://standardschema.dev/) (`~standard`) | Excellent | Excellent | ~20 type-only | **CHOSEN as optional P2 adapter** |

Standard Schema deserves a note: it is a **type-only** interface (`StandardSchemaV1` with a
`~standard` property) that zod, valibot, arktype, yup and joi all implement. Copying its ~20-line
`.d.ts` costs **zero runtime dependency** and lets a user who *already* has zod pass their schema in.
Ship our own combinators as the default; add the adapter later.

### 1.6 Determinism injection

| Option | Parallel-test safe | Discoverable | Middleware access | Verdict |
|---|---|---|---|---|
| Module-level singleton + `setClock()` override | **No** — global mutable state; test files running in parallel workers corrupt each other | Poor | Import | Rejected (C1 × C3) |
| Fake-timer library patching globals | No (same reason) + a dependency | Poor | Implicit | Rejected (C4) |
| Constructor param only | Yes | Good | Must be closed over | Partial |
| **Constructor param with default, *also* on every context** | Yes | Good | `ctx.runtime` | **CHOSEN** |

---

## 2. Recommendation

1. **Contract-first, string-keyed, F-bounded.** `type Contract<C> = { readonly [K in keyof C]: CapabilitySpec }`,
   used only as `C extends Contract<C>`. Never `interface X extends Contract`.
2. **Errors: classes + literal `code` + `Result` at the edge.** `invoke()` throws; `tryInvoke()` returns
   `Result<T, AnyInterlayerError>`. One normalisation funnel, `toInterlayerError()`.
3. **One composition mechanism: the relaxed onion.** Two stacks (`call`, `attempt`) built by the same
   `compose()`. The router is the terminal of the call stack and drives the attempt stack per provider.
4. **One `Runtime` object** for `now/sleep/random/uuid/deadline`, defaulted at construction, carried on
   every context. `createTestRuntime({ seed })` gives a virtual clock and a 4-line seeded PRNG.
5. **Hand-rolled combinator validators.**
6. **Zero-dep typed emitter** over a `type` event map.

### 2.1 The two-stack pipeline (the load-bearing structural decision)

```
invoke(cap, input, hints)
└── call stack        compose<CallContext>   ── runs ONCE per invoke
    ├── logging / tracing
    ├── call deadline
    ├── input validation
    └── TERMINAL: Router.route(ctx, candidates, attempt)
        └── for each candidate provider, in selector order:
            └── attempt stack   compose<AttemptContext>  ── runs ONCE per provider
                ├── bulkhead(provider)
                ├── breaker(provider)      ← sees every physical try
                ├── retry(provider)        ← calls next() N times (re-entrant)
                ├── ratelimit(provider)    ← one token per physical try
                ├── timeout(attempt)       ← narrows the signal via next(ctx')
                └── TERMINAL: provider.capabilities.get(cap)(input, ctx)
```

Rationale for the ordering: retry **inside** the breaker means the breaker observes each physical
attempt (Polly's canonical `retry.Wrap(circuitBreaker)` arrangement, mirrored by
[Cockatiel's `wrap(retry, breaker, timeout)`](https://github.com/connor4312/cockatiel)); rate limit
**inside** retry so each retry consumes a token; timeout innermost so it bounds one physical try.
Call-level deadline is separate and outermost, so a call cannot outlive its budget no matter how
many providers the chain walks.

---

## 3. Rationale against C1 / C3 / C4

**C1 — parallel, file-disjoint agents.**
The whole cross-unit surface is two pre-seeded frozen files, `src/types.ts` (declarations only, zero
runtime code) and `src/errors.ts`. Everything else imports from those and from nothing owned by a
peer unit except through those signatures. Six units then never touch each other's files. Having
exactly **one** composition mechanism matters here: if resilience were `Policy.wrap` while logging
were middleware, Unit 4 and Unit 5 would have to agree on a second contract mid-flight.

**C3 — deterministic and unit-testable.**
`Runtime` is the only door to time, randomness and identity, and it is a constructor parameter, not a
global. Two consequences we measured: the same seed reproduces retry delays byte-for-byte
(`26.05, 160.96, 216.35` ms), a different seed diverges; and a one-hour `sleep` completes in under
100 ms of wall time. The breaker state machine is specified as a pure reducer over
`(state, event, now)` with no timers of its own, so R4 can table-test transitions directly. Every
emitted event carries `at: number` from the injected clock, so event logs are assertable verbatim.

**C4 — zero runtime dependencies.**
Nothing in §5 imports anything. Node 22.22.2 was probed and supplies every primitive used:
`AbortSignal.timeout`/`.any`, `Error.cause`, `Error.captureStackTrace`, `crypto.randomUUID`,
`Promise.withResolvers`, `AggregateError`, `structuredClone`. Validation is 184 lines of
combinators; the PRNG is 4 lines of mulberry32; the emitter is 78 lines. `@types/node` is a
devDependency only.

---

## 4. Rejected, and why (do not revisit)

| Rejected | Why |
|---|---|
| `interface MyContract extends Contract` | Inherits a string index signature; `keyof` widens to `string`; all call-site safety silently lost. Use `C extends Contract<C>`. |
| Symbol capability keys | Not serializable — breaks JSON config, log fields, metric labels, cross-process routing. |
| Strict koa-compose semantics | Its `next() called multiple times` guard makes `retry` unexpressible as middleware. |
| `ctx.body`-style void `next()` | Result cannot be typed; forces `any` through the pipeline. |
| Cockatiel-style `Policy.wrap` **as well as** middleware | Two composition mechanisms → two contracts for parallel agents to agree on (C1). |
| Result-only error model (no throws) | Every provider `throw` still needs catching and converting; you get the ceremony without losing the exception path. |
| `instanceof`-only error guards | False negatives with duplicate package copies or across realms. Use the `Symbol.for` brand; keep `instanceof` as a convenience. |
| Wrapping a *single* provider failure in `AllProvidersFailedError` | Buries the real error one level down and breaks `err.code` assertions. Aggregate only when ≥2 providers actually failed. The smoke suite caught this. |
| zod / valibot / arktype as a runtime dep | C4. Standard Schema's type-only interface gives the same interop for free. |
| rxjs / EventEmitter3 for events | C4; and Node's `EventEmitter` throws on unhandled `'error'`, which would let an observer crash a call. |
| Module-global clock with `setClock()` | Global mutable state; unsafe once test files run in parallel workers (C1 × C3). |
| Fake-timer library patching globals | Same, plus a dependency. The injected `Runtime` *is* the seam. |
| `interface` for the event map | Interfaces lack an implicit index signature and fail the `EventMap` constraint. Must be a `type`. |

---

## 5. Concrete specifics — literal TypeScript

All of the following compiles clean under
`strict + exactOptionalPropertyTypes + noUncheckedIndexedAccess`, `target/lib ES2023`,
`module NodeNext`. Import specifiers use the `.js` extension (NodeNext ESM).

### 5.1 `src/types.ts` — FROZEN. Declarations only, zero runtime code.

```ts
/* ---------- 1. Contract & capabilities ---------- */

export interface CapabilitySpec {
  readonly input: unknown;
  readonly output: unknown;
}

/**
 * F-BOUNDED constraint. Always write `C extends Contract<C>`, never
 * `C extends AnyContract`, and never `interface MyContract extends AnyContract`.
 *
 * Why: `Readonly<Record<string, CapabilitySpec>>` carries a string index
 * signature. An interface that `extends` it inherits that signature, so
 * `keyof C` widens from `'chat.complete' | 'embed.text'` to `string` and every
 * call site silently loses type checking. The F-bounded form imposes the same
 * shape requirement while preserving the literal key union, and it accepts
 * BOTH `interface` and `type` declarations (a bare `interface` does not satisfy
 * an index-signature constraint at all). Verified against tsc 6.x, strict.
 */
export type Contract<C> = { readonly [K in keyof C]: CapabilitySpec };

/** Erased, index-signature form. Internal use only; never a public constraint. */
export type AnyContract = Readonly<Record<string, CapabilitySpec>>;

export type CapabilityKey<C> = Extract<keyof C, string>;
export type InputOf<C, K extends keyof C> = C[K] extends CapabilitySpec ? C[K]['input'] : never;
export type OutputOf<C, K extends keyof C> = C[K] extends CapabilitySpec ? C[K]['output'] : never;

declare const capabilityBrand: unique symbol;

/** Branded, serialisable capability token. Erases to its own string at runtime. */
export type CapabilityRef<K extends string, In, Out> = K & {
  readonly [capabilityBrand]: readonly [In, Out];
};

export type RefKey<R>    = R extends CapabilityRef<infer K, infer _I, infer _O> ? K : never;
export type RefInput<R>  = R extends CapabilityRef<infer _K, infer I, infer _O> ? I : never;
export type RefOutput<R> = R extends CapabilityRef<infer _K, infer _I, infer O> ? O : never;

/* ---------- 2. Determinism seams ---------- */

export interface Runtime {
  /** Epoch milliseconds. The ONLY source of time in the library. */
  now(): number;
  /** Cancellable delay. Rejects with CancelledError if `signal` aborts. */
  sleep(ms: number, signal?: AbortSignal): Promise<void>;
  /** Uniform [0, 1). The ONLY source of randomness (jitter, weighted routing). */
  random(): number;
  /** Correlation id for a call. The ONLY source of identity. */
  uuid(): string;
  /** Signal that aborts after `ms`, linked to `parent`. `dispose` MUST be called. */
  deadline(ms: number, parent?: AbortSignal): DeadlineHandle;
}

export interface DeadlineHandle {
  readonly signal: AbortSignal;
  dispose(): void;
}

export interface TestRuntime extends Runtime {
  /** Fires every timer due at or before now+ms, in due order, ties by insertion. */
  advance(ms: number): Promise<void>;
  /** Runs all pending timers regardless of due time. */
  runAll(): Promise<void>;
  readonly pendingTimers: number;
  setTime(epochMs: number): void;
}

/* ---------- 3. Typed events ---------- */

export type Unsubscribe = () => void;
export type EventMap = Readonly<Record<string, object>>;

export interface Emitter<E extends EventMap> {
  on<K extends Extract<keyof E, string>>(event: K, fn: (payload: E[K]) => void): Unsubscribe;
  once<K extends Extract<keyof E, string>>(event: K, fn: (payload: E[K]) => void): Unsubscribe;
  off<K extends Extract<keyof E, string>>(event: K, fn: (payload: E[K]) => void): void;
  onAny(fn: <K extends Extract<keyof E, string>>(event: K, payload: E[K]) => void): Unsubscribe;
  emit<K extends Extract<keyof E, string>>(event: K, payload: E[K]): void;
  listenerCount(event: Extract<keyof E, string>): number;
  removeAllListeners(event?: Extract<keyof E, string>): void;
}

export type BreakerState = 'closed' | 'open' | 'half-open';

/** MUST be a `type`, not an `interface`: interfaces have no implicit index
 *  signature and so do not satisfy the `EventMap` constraint. */
export type InterlayerEvents = {
  'call:start': { readonly callId: string; readonly capability: string; readonly at: number };
  'call:success': {
    readonly callId: string; readonly capability: string; readonly providerId: string;
    readonly attempts: number; readonly durationMs: number; readonly at: number;
  };
  'call:failure': {
    readonly callId: string; readonly capability: string; readonly error: AnyInterlayerError;
    readonly attempts: number; readonly durationMs: number; readonly at: number;
  };
  'provider:selected': {
    readonly callId: string; readonly capability: string; readonly providerId: string;
    readonly candidateIds: readonly string[]; readonly index: number; readonly at: number;
  };
  'attempt:start': {
    readonly callId: string; readonly providerId: string; readonly attempt: number; readonly at: number;
  };
  'attempt:success': {
    readonly callId: string; readonly providerId: string; readonly attempt: number;
    readonly durationMs: number; readonly at: number;
  };
  'attempt:failure': {
    readonly callId: string; readonly providerId: string; readonly attempt: number;
    readonly durationMs: number; readonly error: AnyInterlayerError;
    readonly willRetry: boolean; readonly at: number;
  };
  'retry:scheduled': {
    readonly callId: string; readonly providerId: string; readonly attempt: number;
    readonly delayMs: number; readonly at: number;
  };
  'breaker:transition': {
    readonly key: string; readonly from: BreakerState; readonly to: BreakerState;
    readonly failures: number; readonly at: number;
  };
  'ratelimit:throttled': { readonly key: string; readonly waitMs: number; readonly at: number };
  'fallback:advance': {
    readonly callId: string; readonly fromProviderId: string; readonly toProviderId: string;
    readonly reason: ErrorCode; readonly at: number;
  };
  'listener:error': { readonly event: string; readonly error: unknown; readonly at: number };
};

/* ---------- 4. Error codes (classes live in errors.ts) ---------- */

export type ErrorCode =
  | 'VALIDATION' | 'CONFIG' | 'UNSUPPORTED_CAPABILITY' | 'NO_PROVIDER'
  | 'TRANSPORT' | 'TIMEOUT' | 'CANCELLED' | 'RATE_LIMITED'
  | 'CIRCUIT_OPEN' | 'BULKHEAD_FULL' | 'PROVIDER' | 'ALL_FAILED';

export interface ErrorContext {
  readonly providerId?: string;
  readonly capability?: string;
  readonly callId?: string;
  readonly attempt?: number;
  readonly details?: Readonly<Record<string, unknown>>;
  readonly cause?: unknown;
  readonly retryable?: boolean;
}

/* Forward reference; errors.ts supplies the concrete union. */
export type AnyInterlayerError = import('./errors.js').AnyInterlayerError;

/* ---------- 5. Result ---------- */

export type Result<T, E = AnyInterlayerError> =
  | { readonly ok: true;  readonly value: T }
  | { readonly ok: false; readonly error: E };

/* ---------- 6. Contexts ---------- */

export interface CallContext {
  readonly callId: string;
  readonly capability: string;
  /** Erased at the middleware boundary; re-typed at the facade. */
  readonly input: unknown;
  readonly startedAt: number;
  readonly deadlineAt: number | undefined;
  /** Call-scoped cancellation. Attempts get a narrower signal. */
  readonly signal: AbortSignal;
  readonly runtime: Runtime;
  readonly events: Emitter<InterlayerEvents>;
  /** Mutable per-call scratch space for middleware. */
  readonly state: Map<string, unknown>;
  /** Caller-supplied routing hints (tags, sticky key, pinned provider). */
  readonly hints: InvokeHints;
  /** Errors from providers already tried, in order. Shared by reference. */
  readonly failures: AnyInterlayerError[];
  /** Shared mutable counters. Survives `{...ctx}` derivation by reference. */
  readonly stats: CallStats;
}

export interface CallStats {
  /** Physical handler invocations across all providers and retries. */
  attempts: number;
  /** Distinct providers entered. */
  providersTried: number;
  /** Provider that produced the successful result, if any. */
  winnerId: string | undefined;
}

export interface AttemptContext extends CallContext {
  readonly provider: ProviderRecord;
  /** 1-based, per-provider. */
  readonly attempt: number;
  /** Attempt-scoped signal: call signal ∧ deadline ∧ per-attempt timeout. */
  readonly signal: AbortSignal;
}

export interface InvokeHints {
  readonly timeoutMs?: number;
  readonly deadlineMs?: number;
  readonly signal?: AbortSignal;
  /** Restrict candidates to these provider ids, in this order. */
  readonly providers?: readonly string[];
  /** Require providers carrying all of these tags. */
  readonly tags?: readonly string[];
  /** Stable key for sticky/consistent selection. */
  readonly stickyKey?: string;
  readonly metadata?: Readonly<Record<string, unknown>>;
  /**
   * ESCAPE HATCH, keyed by provider id. Lets a caller reach a provider-specific
   * feature the canonical contract does not model, without widening the
   * contract to the lowest common denominator. Unvalidated by design.
   */
  readonly providerOptions?: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
}

/* ---------- 7. Providers & registry ---------- */

export type Handler<C, K extends keyof C> = (
  input: InputOf<C, K>,
  ctx: AttemptContext,
) => Promise<OutputOf<C, K>>;

export type ErasedHandler = (input: unknown, ctx: AttemptContext) => Promise<unknown>;

export interface ProviderTraits {
  readonly tags?: readonly string[];
  /** Lower is preferred by cost-aware selectors. Arbitrary units. */
  readonly costPerCall?: number;
  /** Advisory p50 latency in ms, used by latency-aware selectors. */
  readonly latencyMs?: number;
  readonly region?: string;
}

export interface Provider<C extends Contract<C>, K extends CapabilityKey<C> = CapabilityKey<C>> {
  readonly id: string;
  readonly capabilities: { readonly [P in K]: Handler<C, P> };
  readonly traits?: ProviderTraits;
  /** Optional liveness probe; never called on the hot path. */
  readonly health?: (ctx: { runtime: Runtime; signal: AbortSignal }) => Promise<HealthStatus>;
  readonly dispose?: () => void | Promise<void>;
}

export interface HealthStatus {
  readonly healthy: boolean;
  readonly detail?: string;
}

/** Runtime-side, type-erased provider entry held by the registry. */
export interface ProviderRecord {
  readonly id: string;
  readonly capabilities: ReadonlyMap<string, ErasedHandler>;
  readonly traits: ProviderTraits;
  readonly priority: number;
  enabled: boolean;
  readonly health?: (ctx: { runtime: Runtime; signal: AbortSignal }) => Promise<HealthStatus>;
  readonly dispose?: () => void | Promise<void>;
}

export interface RegisterOptions {
  /** Lower runs first in the default `inOrder` selector. Default: registration index. */
  readonly priority?: number;
  readonly enabled?: boolean;
  readonly traits?: ProviderTraits;
}

export interface Registry<C extends Contract<C>> {
  register<K extends CapabilityKey<C>>(provider: Provider<C, K>, opts?: RegisterOptions): this;
  unregister(providerId: string): boolean;
  get(providerId: string): ProviderRecord | undefined;
  /** Enabled providers declaring `capability`, sorted by priority then registration order. */
  candidatesFor(capability: string): readonly ProviderRecord[];
  list(): readonly ProviderRecord[];
  setEnabled(providerId: string, enabled: boolean): boolean;
  dispose(): Promise<void>;
}

/* ---------- 8. Selection & routing ---------- */

export type Selector = (
  candidates: readonly ProviderRecord[],
  ctx: CallContext,
) => readonly ProviderRecord[];

export interface Router {
  /** Drives the fallback chain; runs `attempt` once per selected provider. */
  route(
    ctx: CallContext,
    candidates: readonly ProviderRecord[],
    attempt: (provider: ProviderRecord) => Promise<unknown>,
  ): Promise<unknown>;
}

export interface RouterOptions {
  readonly selector?: Selector;
  /** Max providers tried per call. Default: Infinity. */
  readonly maxProviders?: number;
  /** Decides whether the next provider should be tried. Default: `err.retryable`. */
  readonly shouldFallback?: (error: AnyInterlayerError, ctx: CallContext) => boolean;
}

/* ---------- 9. Middleware & composition ---------- */

/**
 * `next()` optionally accepts a REPLACEMENT context for everything downstream.
 * This is how `timeout` narrows the signal and how `retry` bumps the attempt
 * number without mutating shared state. Omit the argument to pass `ctx` through.
 */
export type Next<Ctx, R> = (ctx?: Ctx) => Promise<R>;
export type Middleware<Ctx, R = unknown> = (ctx: Ctx, next: Next<Ctx, R>) => Promise<R>;
export type Terminal<Ctx, R> = (ctx: Ctx) => Promise<R>;
export type Composed<Ctx, R> = (ctx: Ctx, terminal: Terminal<Ctx, R>) => Promise<R>;

export type CallMiddleware = Middleware<CallContext, unknown>;
export type AttemptMiddleware = Middleware<AttemptContext, unknown>;

/* ---------- 10. Validation ---------- */

export type PathSegment = string | number;

export interface Issue {
  readonly path: readonly PathSegment[];
  readonly message: string;
  readonly code: string;
}

export type ValidationResult<T> =
  | { readonly ok: true;  readonly value: T }
  | { readonly ok: false; readonly issues: readonly Issue[] };

export interface Validator<T> {
  /** Human-readable type name used in messages, e.g. `string`, `{ a: number }`. */
  readonly typeName: string;
  validate(value: unknown, path?: readonly PathSegment[]): ValidationResult<T>;
}

export type Infer<V> = V extends Validator<infer T> ? T : never;

/* ---------- 11. Public facade ---------- */

export interface InterlayerOptions {
  readonly runtime?: Runtime;
  readonly events?: Emitter<InterlayerEvents>;
  readonly call?: readonly CallMiddleware[];
  readonly attempt?: readonly AttemptMiddleware[];
  readonly router?: RouterOptions;
  readonly defaults?: InvokeHints;
  readonly validators?: Readonly<
    Record<string, { input?: Validator<unknown>; output?: Validator<unknown> }>
  >;
}

export interface Interlayer<C extends Contract<C>> {
  readonly registry: Registry<C>;
  readonly events: Emitter<InterlayerEvents>;
  readonly runtime: Runtime;

  /** Throwing form. Rejects with an `InterlayerError` subclass, always. */
  invoke<R extends CapabilityRef<string, unknown, unknown>>(
    capability: R, input: RefInput<R>, hints?: InvokeHints,
  ): Promise<RefOutput<R>>;
  invoke<K extends CapabilityKey<C>>(
    capability: K, input: InputOf<C, K>, hints?: InvokeHints,
  ): Promise<OutputOf<C, K>>;

  /** Non-throwing form. Never rejects for domain failures. */
  tryInvoke<R extends CapabilityRef<string, unknown, unknown>>(
    capability: R, input: RefInput<R>, hints?: InvokeHints,
  ): Promise<Result<RefOutput<R>>>;
  tryInvoke<K extends CapabilityKey<C>>(
    capability: K, input: InputOf<C, K>, hints?: InvokeHints,
  ): Promise<Result<OutputOf<C, K>>>;

  /** Pre-bound callable for a capability; zero per-call lookup cost. */
  bind<K extends CapabilityKey<C>>(
    capability: K,
  ): (input: InputOf<C, K>, hints?: InvokeHints) => Promise<OutputOf<C, K>>;

  supports(capability: string): boolean;
  dispose(): Promise<void>;
}
```

### 5.2 `src/compose.ts` — the literal implementation

```ts
import type { Composed, Middleware, Next, Terminal } from './types.js';
import { ConfigError } from './errors.js';

/**
 * Compose middleware into a single callable, koa-style.
 *
 * Differences from `koa-compose`, all deliberate:
 *  1. `next()` RESOLVES TO THE DOWNSTREAM VALUE (koa returns void and mutates
 *     `ctx.body`). This keeps the result in the type system.
 *  2. `next()` is RE-ENTRANT: a middleware may call it several times in
 *     sequence, each call re-running a fresh downstream chain. That is what
 *     makes `retry` expressible as ordinary middleware. Concurrent (unawaited)
 *     re-entry is still rejected — it catches koa's classic "missing await".
 *  3. `next(ctx')` may REPLACE the context for everything downstream, which is
 *     how `timeout` narrows the AbortSignal without mutating shared state.
 */
export function compose<Ctx, R>(middleware: readonly Middleware<Ctx, R>[]): Composed<Ctx, R> {
  for (let i = 0; i < middleware.length; i++) {
    if (typeof middleware[i] !== 'function') {
      throw new ConfigError(`Middleware[${i}] is not a function`, [
        { path: ['middleware', i], message: 'expected a function', code: 'invalid_type' },
      ]);
    }
  }
  const stack = middleware.slice();

  return function run(ctx: Ctx, terminal: Terminal<Ctx, R>): Promise<R> {
    return dispatch(0, ctx);

    function dispatch(i: number, current: Ctx): Promise<R> {
      if (i === stack.length) {
        try {
          return Promise.resolve(terminal(current));
        } catch (err) {
          return Promise.reject(err);
        }
      }
      const fn = stack[i] as Middleware<Ctx, R>;
      let pending = false;
      const next: Next<Ctx, R> = (replacement?: Ctx) => {
        if (pending) {
          return Promise.reject(
            new ConfigError(
              `Middleware[${i}] called next() again before the previous call settled. ` +
                'Await the previous next() (retry loops must be sequential).',
              [],
            ),
          );
        }
        pending = true;
        return dispatch(i + 1, replacement ?? current).then(
          (v) => { pending = false; return v; },
          (e: unknown) => { pending = false; throw e; },
        );
      };
      try {
        return Promise.resolve(fn(current, next));
      } catch (err) {
        return Promise.reject(err);
      }
    }
  };
}

/** `compose` for the common case where the terminal is already bound. */
export function pipeline<Ctx, R>(
  middleware: readonly Middleware<Ctx, R>[],
  terminal: Terminal<Ctx, R>,
): (ctx: Ctx) => Promise<R> {
  const run = compose(middleware);
  return (ctx) => run(ctx, terminal);
}
```

**Verified execution order.** For `[A, B]` with `B` calling `next({n: c.n+1})`:
`['a>', 'b>1', 't2', 'b<', 'a<']` — proper onion, replacement context reaching the terminal.

### 5.3 `src/errors.ts` — FROZEN

```ts
import type { ErrorCode, ErrorContext, Issue } from './types.js';

/** Cross-realm / duplicate-copy safe brand. `instanceof` alone is not reliable. */
const MARKER = 'interlayer.error.v1';
const kInterlayer = Symbol.for(MARKER);   // `const` + Symbol.for() ⇒ `unique symbol`

export abstract class InterlayerError extends Error {
  abstract readonly code: ErrorCode;

  readonly providerId: string | undefined;
  readonly capability: string | undefined;
  readonly callId: string | undefined;
  readonly attempt: number | undefined;
  readonly details: Readonly<Record<string, unknown>>;
  /** Whether a *different* provider or a later attempt could plausibly succeed. */
  readonly retryable: boolean;

  protected readonly [kInterlayer] = true;

  protected constructor(message: string, ctx: ErrorContext = {}, retryableDefault = false) {
    super(message, ctx.cause !== undefined ? { cause: ctx.cause } : undefined);
    this.name = new.target.name;
    this.providerId = ctx.providerId;
    this.capability = ctx.capability;
    this.callId    = ctx.callId;
    this.attempt   = ctx.attempt;
    this.details   = ctx.details ?? {};
    this.retryable = ctx.retryable ?? retryableDefault;
    // V8 only; keeps the constructor frame out of the stack. No Object.setPrototypeOf
    // needed because we target ES2023, not ES5.
    Error.captureStackTrace?.(this, new.target);
  }

  /**
   * Returns a COPY with blank context slots filled in. Never overwrites a value
   * the thrower already supplied. Preserves prototype (so `instanceof` and the
   * brand survive), `stack`, `message` and `cause`.
   *
   * Needed because providers legitimately throw `new TransportError('down')`
   * with no idea of their own id; the router stamps identity on the way out.
   */
  withContext(ctx: ErrorContext): this {
    if (
      (ctx.providerId === undefined || this.providerId !== undefined) &&
      (ctx.capability === undefined || this.capability !== undefined) &&
      (ctx.callId     === undefined || this.callId     !== undefined) &&
      (ctx.attempt    === undefined || this.attempt    !== undefined)
    ) {
      return this;
    }
    const next = Object.create(Object.getPrototypeOf(this) as object) as this;
    for (const key of Reflect.ownKeys(this)) {
      const desc = Object.getOwnPropertyDescriptor(this, key);
      if (desc !== undefined) Object.defineProperty(next, key, desc);
    }
    const fill = (k: 'providerId' | 'capability' | 'callId' | 'attempt', v: unknown): void => {
      if (v !== undefined && (next as unknown as Record<string, unknown>)[k] === undefined) {
        Object.defineProperty(next, k, { value: v, enumerable: true, writable: false, configurable: true });
      }
    };
    fill('providerId', ctx.providerId);
    fill('capability', ctx.capability);
    fill('callId', ctx.callId);
    fill('attempt', ctx.attempt);
    return next;
  }

  /** Structured, log-safe projection. Never includes the stack. */
  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      ...(this.providerId !== undefined ? { providerId: this.providerId } : {}),
      ...(this.capability !== undefined ? { capability: this.capability } : {}),
      ...(this.callId     !== undefined ? { callId: this.callId } : {}),
      ...(this.attempt    !== undefined ? { attempt: this.attempt } : {}),
      ...(Object.keys(this.details).length > 0 ? { details: this.details } : {}),
      ...(this.cause !== undefined ? { cause: describeCause(this.cause) } : {}),
    };
  }
}

/* ---------- Leaf classes ---------- */

export class ValidationError extends InterlayerError {
  readonly code = 'VALIDATION' as const;
  readonly issues: readonly Issue[];
  constructor(message: string, issues: readonly Issue[], ctx: ErrorContext = {}) {
    super(message, ctx, false);
    this.issues = issues;
  }
}

export class ConfigError extends InterlayerError {
  readonly code = 'CONFIG' as const;
  readonly issues: readonly Issue[];
  constructor(message: string, issues: readonly Issue[] = [], ctx: ErrorContext = {}) {
    super(message, ctx, false);
    this.issues = issues;
  }
}

export class UnsupportedCapabilityError extends InterlayerError {
  readonly code = 'UNSUPPORTED_CAPABILITY' as const;
  constructor(capability: string, ctx: ErrorContext = {}) {
    super(`No provider declares capability "${capability}"`, { ...ctx, capability }, false);
  }
}

export class NoProviderAvailableError extends InterlayerError {
  readonly code = 'NO_PROVIDER' as const;
  constructor(message: string, ctx: ErrorContext = {}) { super(message, ctx, false); }
}

export class TransportError extends InterlayerError {
  readonly code = 'TRANSPORT' as const;
  constructor(message: string, ctx: ErrorContext = {}) { super(message, ctx, true); }
}

export class TimeoutError extends InterlayerError {
  readonly code = 'TIMEOUT' as const;
  readonly timeoutMs: number;
  readonly scope: 'attempt' | 'call';
  constructor(timeoutMs: number, scope: 'attempt' | 'call', ctx: ErrorContext = {}) {
    super(`${scope} timed out after ${timeoutMs}ms`, ctx, scope === 'attempt');
    this.timeoutMs = timeoutMs;
    this.scope = scope;
  }
}

/** Caller-initiated abort. Never retryable — the caller asked us to stop. */
export class CancelledError extends InterlayerError {
  readonly code = 'CANCELLED' as const;
  constructor(message = 'Operation cancelled', ctx: ErrorContext = {}) { super(message, ctx, false); }
}

export class RateLimitedError extends InterlayerError {
  readonly code = 'RATE_LIMITED' as const;
  /** Server- or limiter-supplied hint, in ms. */
  readonly retryAfterMs: number | undefined;
  constructor(message: string, retryAfterMs?: number, ctx: ErrorContext = {}) {
    super(message, ctx, true);
    this.retryAfterMs = retryAfterMs;
  }
}

export class CircuitOpenError extends InterlayerError {
  readonly code = 'CIRCUIT_OPEN' as const;
  readonly openedAt: number;
  readonly halfOpenAt: number;
  constructor(key: string, openedAt: number, halfOpenAt: number, ctx: ErrorContext = {}) {
    super(`Circuit "${key}" is open`, { ...ctx, details: { ...ctx.details, key } }, true);
    this.openedAt = openedAt;
    this.halfOpenAt = halfOpenAt;
  }
}

export class BulkheadFullError extends InterlayerError {
  readonly code = 'BULKHEAD_FULL' as const;
  constructor(key: string, limit: number, ctx: ErrorContext = {}) {
    super(`Bulkhead "${key}" is full (limit ${limit})`, ctx, true);
  }
}

/** A provider returned a domain-level failure. `cause` holds the original throw. */
export class ProviderError extends InterlayerError {
  readonly code = 'PROVIDER' as const;
  readonly status: number | undefined;
  constructor(message: string, ctx: ErrorContext = {}, status?: number) {
    super(message, ctx, ctx.retryable ?? isRetryableStatus(status));
    this.status = status;
  }
}

/** Terminal error of a fallback chain. Aggregates one error per provider tried. */
export class AllProvidersFailedError extends InterlayerError {
  readonly code = 'ALL_FAILED' as const;
  readonly errors: readonly AnyInterlayerError[];
  readonly triedProviderIds: readonly string[];
  constructor(errors: readonly AnyInterlayerError[], ctx: ErrorContext = {}) {
    const ids = errors.map((e) => e.providerId ?? '<unknown>');
    super(
      `All ${errors.length} provider(s) failed for "${ctx.capability ?? '?'}": ` +
        errors.map((e, i) => `${ids[i]}=${e.code}`).join(', '),
      { ...ctx, cause: errors[errors.length - 1] },
      false,
    );
    this.errors = errors;
    this.triedProviderIds = ids;
  }
}

/* ---------- Union: enables exhaustive `switch (err.code)` ---------- */

export type AnyInterlayerError =
  | ValidationError | ConfigError | UnsupportedCapabilityError | NoProviderAvailableError
  | TransportError | TimeoutError | CancelledError | RateLimitedError
  | CircuitOpenError | BulkheadFullError | ProviderError | AllProvidersFailedError;

/* ---------- Guards & normalisation ---------- */

export function isInterlayerError(e: unknown): e is AnyInterlayerError {
  return typeof e === 'object' && e !== null && (e as Record<symbol, unknown>)[kInterlayer] === true;
}

export function hasCode<K extends ErrorCode>(
  e: unknown, code: K,
): e is Extract<AnyInterlayerError, { code: K }> {
  return isInterlayerError(e) && e.code === code;
}

/** Single funnel: every non-Interlayer throw becomes a typed error, cause preserved. */
export function toInterlayerError(e: unknown, ctx: ErrorContext = {}): AnyInterlayerError {
  if (isInterlayerError(e)) return e.withContext(ctx);
  if (isAbortLike(e)) return new CancelledError('Aborted', { ...ctx, cause: e });
  const message = e instanceof Error ? e.message : String(e);
  return new ProviderError(message, { ...ctx, cause: e }, extractStatus(e));
}

function isAbortLike(e: unknown): boolean {
  return (
    typeof e === 'object' && e !== null &&
    ((e as { name?: unknown }).name === 'AbortError' || (e as { name?: unknown }).name === 'TimeoutError')
  );
}

function extractStatus(e: unknown): number | undefined {
  if (typeof e !== 'object' || e === null) return undefined;
  const s = (e as { status?: unknown }).status ?? (e as { statusCode?: unknown }).statusCode;
  return typeof s === 'number' ? s : undefined;
}

function isRetryableStatus(status: number | undefined): boolean {
  if (status === undefined) return false;
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function describeCause(cause: unknown): unknown {
  if (isInterlayerError(cause)) return cause.toJSON();
  if (cause instanceof Error) return { name: cause.name, message: cause.message };
  return cause;
}

/** Flattens the `cause` chain for logs: "TimeoutError: … <- TransportError: …". */
export function formatErrorChain(e: unknown, max = 8): string {
  const parts: string[] = [];
  let cur: unknown = e;
  for (let i = 0; i < max && cur instanceof Error; i++) {
    parts.push(`${cur.name}: ${cur.message}`);
    cur = cur.cause;
  }
  return parts.join(' <- ');
}
```

**Cause-chain rules.** (a) Only `toInterlayerError` wraps foreign throws, and it always passes the
original as `cause`. (b) `AllProvidersFailedError.cause` is the *last* failure; the full set is
`errors[]`. (c) `withContext` clones rather than mutates, so an error's identity fields are stamped
exactly once, by whoever knows them. (d) `toJSON` recurses one level into `cause` for structured logs.

### 5.4 `src/events.ts` — typed emitter

```ts
import type { Emitter, EventMap, InterlayerEvents, Unsubscribe } from './types.js';

type AnyListener = (payload: never) => void;
type AnyWildcard = (event: string, payload: never) => void;

export function createEmitter<E extends EventMap>(): Emitter<E> {
  const listeners = new Map<string, Set<AnyListener>>();
  const wildcards = new Set<AnyWildcard>();

  function add(event: string, fn: AnyListener): Unsubscribe {
    let set = listeners.get(event);
    if (set === undefined) { set = new Set(); listeners.set(event, set); }
    set.add(fn);
    return () => { set.delete(fn); if (set.size === 0) listeners.delete(event); };
  }

  const emitter: Emitter<E> = {
    on(event, fn) { return add(event, fn as AnyListener); },
    once(event, fn) {
      const wrapped = ((p: never) => { off(); (fn as AnyListener)(p); }) as AnyListener;
      const off = add(event, wrapped);
      return off;
    },
    off(event, fn) {
      const set = listeners.get(event);
      if (set === undefined) return;
      set.delete(fn as AnyListener);
      if (set.size === 0) listeners.delete(event);
    },
    onAny(fn) {
      wildcards.add(fn as AnyWildcard);
      return () => { wildcards.delete(fn as AnyWildcard); };
    },
    emit(event, payload) {
      const set = listeners.get(event);
      if (set !== undefined) {
        // Snapshot: a listener may unsubscribe itself or others during dispatch.
        for (const fn of [...set]) {
          try { (fn as (p: unknown) => void)(payload); }
          catch (error) { reportListenerError(event, error); }
        }
      }
      if (wildcards.size > 0) {
        for (const fn of [...wildcards]) {
          try { (fn as (e: string, p: unknown) => void)(event, payload); }
          catch (error) { reportListenerError(event, error); }
        }
      }
    },
    listenerCount(event) { return listeners.get(event)?.size ?? 0; },
    removeAllListeners(event) {
      if (event === undefined) { listeners.clear(); wildcards.clear(); }
      else listeners.delete(event);
    },
  };

  function reportListenerError(event: string, error: unknown): void {
    const set = listeners.get('listener:error');
    if (set === undefined || set.size === 0 || event === 'listener:error') return;
    for (const fn of [...set]) {
      try { (fn as (p: unknown) => void)({ event, error, at: 0 }); } catch { /* give up */ }
    }
  }

  return emitter;
}

export type InterlayerEmitter = Emitter<InterlayerEvents>;
```

Two properties chosen against Node's `EventEmitter`: dispatch is **synchronous** (so event order is
deterministic and assertable against the injected clock), and a throwing listener **cannot** break
the call path — it is routed to `listener:error`. Node's emitter throws on an unhandled `'error'`
event, which would let an observer crash a request. `on` returns an unsubscribe closure, following
Cockatiel's disposable-returning `Event` convention.

### 5.5 `src/runtime.ts` — determinism seams

```ts
import type { DeadlineHandle, Runtime, TestRuntime } from './types.js';
import { CancelledError } from './errors.js';

export function createSystemRuntime(): Runtime {
  return {
    now: () => Date.now(),
    random: () => Math.random(),
    uuid: () => crypto.randomUUID(),

    sleep(ms, signal) {
      return new Promise<void>((resolve, reject) => {
        if (signal?.aborted === true) { reject(new CancelledError('Aborted before sleep')); return; }
        const handle = setTimeout(() => { cleanup(); resolve(); }, ms);
        const onAbort = () => { clearTimeout(handle); cleanup(); reject(new CancelledError('Aborted during sleep')); };
        const cleanup = () => { signal?.removeEventListener('abort', onAbort); };
        signal?.addEventListener('abort', onAbort, { once: true });   // no listener leak
      });
    },

    deadline(ms, parent): DeadlineHandle {
      const ctrl = new AbortController();
      const handle = setTimeout(() => ctrl.abort(new CancelledError(`Deadline of ${ms}ms elapsed`)), ms);
      handle.unref?.();                       // never hold the process open
      const onParent = () => ctrl.abort(parent?.reason);
      parent?.addEventListener('abort', onParent, { once: true });
      if (parent?.aborted === true) ctrl.abort(parent.reason);
      return {
        signal: ctrl.signal,
        dispose() { clearTimeout(handle); parent?.removeEventListener('abort', onParent); },
      };
    },
  };
}

export const systemRuntime: Runtime = createSystemRuntime();

/* ---------- Deterministic test runtime ---------- */

interface Timer { readonly dueAt: number; readonly seq: number; readonly fire: () => void; cancelled: boolean }

/** mulberry32 — 4 lines, uniform enough for jitter, fully reproducible. */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function createTestRuntime(opts: { startTime?: number; seed?: number } = {}): TestRuntime {
  let clock = opts.startTime ?? 0;
  let seq = 0;
  let ids = 0;
  const timers: Timer[] = [];
  const rand = mulberry32(opts.seed ?? 1);

  function schedule(delayMs: number, fire: () => void): Timer {
    const t: Timer = { dueAt: clock + Math.max(0, delayMs), seq: seq++, fire, cancelled: false };
    timers.push(t);
    return t;
  }

  async function drainUntil(target: number): Promise<void> {
    for (;;) {
      const due = timers
        .filter((t) => !t.cancelled && t.dueAt <= target)
        .sort((a, b) => a.dueAt - b.dueAt || a.seq - b.seq)[0];   // ties broken by insertion
      if (due === undefined) break;
      timers.splice(timers.indexOf(due), 1);
      clock = Math.max(clock, due.dueAt);
      due.fire();
      await Promise.resolve();   // let microtasks queued by the callback settle
    }
    clock = Math.max(clock, target);
  }

  return {
    now: () => clock,
    random: rand,
    uuid: () => `test-${(++ids).toString().padStart(8, '0')}`,

    sleep(ms, signal) {
      return new Promise<void>((resolve, reject) => {
        if (signal?.aborted === true) { reject(new CancelledError('Aborted before sleep')); return; }
        const t = schedule(ms, () => { cleanup(); resolve(); });
        const onAbort = () => { t.cancelled = true; cleanup(); reject(new CancelledError('Aborted during sleep')); };
        const cleanup = () => { signal?.removeEventListener('abort', onAbort); };
        signal?.addEventListener('abort', onAbort, { once: true });
      });
    },

    deadline(ms, parent): DeadlineHandle {
      const ctrl = new AbortController();
      const t = schedule(ms, () => ctrl.abort(new CancelledError(`Deadline of ${ms}ms elapsed`)));
      const onParent = () => ctrl.abort(parent?.reason);
      parent?.addEventListener('abort', onParent, { once: true });
      if (parent?.aborted === true) ctrl.abort(parent.reason);
      return { signal: ctrl.signal, dispose() { t.cancelled = true; parent?.removeEventListener('abort', onParent); } };
    },

    advance: (ms) => drainUntil(clock + ms),
    runAll: async () => { await drainUntil(timers.reduce((m, t) => Math.max(m, t.dueAt), clock)); },
    get pendingTimers() { return timers.filter((t) => !t.cancelled).length; },
    setTime(epochMs) { clock = epochMs; },
  };
}
```

**Where the seams are used — the complete list. No other file may call `Date.now`, `Math.random`,
`setTimeout`, or `crypto.randomUUID`. Enforce with an ESLint `no-restricted-globals`/`no-restricted-properties`
rule (R5's remit), scoped to allow `src/runtime.ts`.**

| Seam | Consumers |
|---|---|
| `now()` | every emitted event's `at`; breaker open/half-open timestamps; rate-limiter bucket refill; call duration; deadline arithmetic |
| `sleep()` | retry backoff delay; rate-limiter wait |
| `random()` | retry jitter; `weighted` selector |
| `uuid()` | `ctx.callId` |
| `deadline()` | `timeout` middleware; call-level deadline |

### 5.6 `src/validate.ts` — the reusable pattern (abridged; full file is 184 LOC)

```ts
import type { Infer, Issue, PathSegment, Validator, ValidationResult } from './types.js';
import { ConfigError, ValidationError } from './errors.js';
export type { Infer, Issue, Validator, ValidationResult } from './types.js';

const ok  = <T>(value: T): ValidationResult<T> => ({ ok: true, value });
const bad = (path: readonly PathSegment[], message: string, code = 'invalid_type'): ValidationResult<never> =>
  ({ ok: false, issues: [{ path, message, code }] });

function define<T>(
  typeName: string,
  fn: (v: unknown, p: readonly PathSegment[]) => ValidationResult<T>,
): Validator<T> {
  return { typeName, validate: (v, p = []) => fn(v, p) };
}

export const string = (): Validator<string> =>
  define('string', (v, p) => (typeof v === 'string' ? ok(v) : bad(p, `expected string, got ${typeOf(v)}`)));

export const integer = (): Validator<number> =>
  define('integer', (v, p) =>
    typeof v === 'number' && Number.isInteger(v) ? ok(v) : bad(p, `expected integer, got ${typeOf(v)}`));

export const enums = <const T extends readonly (string | number)[]>(values: T): Validator<T[number]> =>
  define(values.map(show).join(' | '), (v, p) =>
    (values as readonly unknown[]).includes(v)
      ? ok(v as T[number])
      : bad(p, `expected one of ${values.map(show).join(', ')}, got ${show(v)}`));

export function refine<T>(inner: Validator<T>, pred: (v: T) => boolean, message: string, code = 'refinement'): Validator<T> {
  return define(inner.typeName, (v, p) => {
    const r = inner.validate(v, p);
    if (!r.ok) return r;
    return pred(r.value) ? r : bad(p, message, code);
  });
}
export const min = (v: Validator<number>, n: number) => refine(v, (x) => x >= n, `must be >= ${n}`, 'too_small');
export const nonEmpty = (v: Validator<string>) => refine(v, (x) => x.length > 0, 'must not be empty', 'too_small');

export function optional<T>(inner: Validator<T>): Validator<T | undefined> {
  return define(`${inner.typeName}?`, (v, p) => (v === undefined ? ok(undefined) : inner.validate(v, p)));
}
export function withDefault<T>(inner: Validator<T>, fallback: () => T): Validator<T> {
  return define(inner.typeName, (v, p) => (v === undefined ? ok(fallback()) : inner.validate(v, p)));
}

export function array<T>(inner: Validator<T>): Validator<T[]> {
  return define(`${inner.typeName}[]`, (v, p) => {
    if (!Array.isArray(v)) return bad(p, `expected array, got ${typeOf(v)}`);
    const out: T[] = []; const issues: Issue[] = [];
    for (let i = 0; i < v.length; i++) {
      const r = inner.validate(v[i], [...p, i]);            // path accumulates
      if (r.ok) out.push(r.value); else issues.push(...r.issues);
    }
    return issues.length > 0 ? { ok: false, issues } : ok(out);   // ALL issues, not the first
  });
}

type Shape = Readonly<Record<string, Validator<unknown>>>;
type ShapeOutput<S extends Shape> = { [K in keyof S]: Infer<S[K]> };

export function object<S extends Shape>(shape: S, opts: { readonly strict?: boolean } = {}): Validator<ShapeOutput<S>> {
  const keys = Object.keys(shape);
  const typeName = `{ ${keys.map((k) => `${k}: ${shape[k]?.typeName ?? '?'}`).join('; ')} }`;
  return define(typeName, (v, p) => {
    if (!isPlainObject(v)) return bad(p, `expected object, got ${typeOf(v)}`);
    const out: Record<string, unknown> = {}; const issues: Issue[] = [];
    for (const k of keys) {
      const r = shape[k]!.validate(v[k], [...p, k]);
      if (r.ok) { if (r.value !== undefined || k in v) out[k] = r.value; }
      else issues.push(...r.issues);
    }
    if (opts.strict === true) {
      for (const k of Object.keys(v)) {
        if (!(k in shape)) {
          issues.push({ path: [...p, k], message: `unknown key "${k}" (allowed: ${keys.join(', ')})`, code: 'unrecognized_key' });
        }
      }
    }
    return issues.length > 0 ? { ok: false, issues } : ok(out as ShapeOutput<S>);
  });
}

/** Assertion function: narrows `value` in the CALLER's scope on success. */
export function assertValid<T>(value: unknown, schema: Validator<T>, label = 'value'): asserts value is T {
  const r = schema.validate(value, []);
  if (!r.ok) throw new ValidationError(`Invalid ${label}: ${formatIssues(r.issues)}`, r.issues);
}

export function parse<T>(value: unknown, schema: Validator<T>, label = 'value'): T {
  const r = schema.validate(value, []);
  if (!r.ok) throw new ValidationError(`Invalid ${label}: ${formatIssues(r.issues)}`, r.issues);
  return r.value;
}

/** Config-flavoured parse: throws ConfigError, which is never retried. */
export function parseConfig<T>(value: unknown, schema: Validator<T>, label = 'config'): T {
  const r = schema.validate(value, []);
  if (!r.ok) throw new ConfigError(`Invalid ${label}:\n${formatIssues(r.issues, '\n  ')}`, r.issues);
  return r.value;
}

export function formatIssues(issues: readonly Issue[], sep = '; '): string {
  return issues.map((i) => `${i.path.length > 0 ? pathString(i.path) : '<root>'}: ${i.message}`).join(sep);
}
export function pathString(path: readonly PathSegment[]): string {
  return path.reduce<string>((acc, seg) =>
    typeof seg === 'number' ? `${acc}[${seg}]` : acc === '' ? seg : `${acc}.${seg}`, '');
}
```

Usage — narrowing and messages together, verified:

```ts
const schema = v.object({
  id: v.nonEmpty(v.string()),
  retries: v.withDefault(v.min(v.integer(), 0), () => 3),
  endpoints: v.array(v.object({ url: v.string(), weight: v.optional(v.number()) })),
}, { strict: true });

type Config = v.Infer<typeof schema>;
// => { id: string; retries: number; endpoints: { url: string; weight: number | undefined }[] }
```

Actual output for `{ id: '', endpoints: [{url:1},{url:'ok',weight:'x'}], bogus: 1 }`:

```
Invalid config:
  id: must not be empty
  endpoints[0].url: expected string, got number
  endpoints[1].weight: expected finite number, got string
  bogus: unknown key "bogus" (allowed: id, retries, endpoints)
```

### 5.7 End-user call site — the acceptance criterion for the whole design

```ts
// No `extends Contract`. Interface OR type alias both work.
interface AiContract {
  'chat.complete': { input: ChatIn;  output: ChatOut };
  'embed.text':    { input: EmbedIn; output: EmbedOut };
}

const alpha: Provider<AiContract, 'chat.complete' | 'embed.text'> = {
  id: 'alpha',
  traits: { tags: ['fast', 'us'], costPerCall: 1 },
  capabilities: {
    'chat.complete': async (input, ctx) => ({ text: `alpha:${input.prompt}`, tokens: ctx.attempt }),
    'embed.text':    async (input)      => ({ vectors: input.texts.map(() => [0.1, 0.2]) }),
  },
};
const beta: Provider<AiContract, 'chat.complete'> = { /* subset — no error */ };

const il = createInterlayer<AiContract>({
  runtime: createTestRuntime({ seed: 42 }),
  attempt: [timeout(1_000), retry({ maxAttempts: 3, baseDelayMs: 100 })],
});
il.registry.register(alpha, { priority: 0 }).register(beta, { priority: 1 });

const out = await il.invoke('chat.complete', { prompt: 'hi' });   // out: ChatOut, inferred
await il.invoke('chat.complete', { texts: ['a'] });               // ✗ TS error — wrong input
await il.invoke('nope', {});                                      // ✗ TS error — unknown capability
(await il.invoke('chat.complete', { prompt: 'x' })).vectors;      // ✗ TS error — no such field

const r = await il.tryInvoke('chat.complete', { prompt: 'safe' });
if (!r.ok) {
  switch (r.error.code) {                       // exhaustive, narrows per-branch
    case 'TIMEOUT':      r.error.timeoutMs;     break;   // number
    case 'ALL_FAILED':   r.error.errors;        break;   // readonly AnyInterlayerError[]
    case 'RATE_LIMITED': r.error.retryAfterMs;  break;   // number | undefined
    case 'VALIDATION':   r.error.issues;        break;   // readonly Issue[]
  }
}

// Token form, for capabilities declared in a package that has no access to C:
export const ChatComplete = capability<ChatIn, ChatOut>()('chat.complete');
const out2 = await il.invoke(ChatComplete, { prompt: 'x' });   // out2: ChatOut
```

All three `✗` lines were asserted with `@ts-expect-error` and **all three directives were consumed**,
i.e. the errors genuinely fire.

### 5.8 Verified behaviours (smoke suite, all passing)

| # | Behaviour | Evidence |
|---|---|---|
| 1 | Onion order + context replacement | `['a>','b>1','t2','b<','a<']` |
| 2 | Re-entrant `next()` (3 iterations) succeeds; concurrent `next()` rejects | ✔ |
| 3 | `cause` preserved, `code` discriminates, brand guard works, `captureStackTrace` hides the ctor frame | ✔ |
| 4 | Fallback bad→good; `fallback:advance` emitted; 2× `provider:selected` | ✔ |
| 5 | Both fail ⇒ `ALL_FAILED`, `triedProviderIds=['a','b']`, `cause === errors[1]` | ✔ |
| 6 | Non-retryable (HTTP 400) ⇒ no fallback, error surfaces un-wrapped as `PROVIDER` | ✔ |
| 7 | Same seed ⇒ identical delays `26.05, 160.96, 216.35`; different seed diverges | ✔ |
| 8 | 1-hour `sleep` completes in <100 ms wall time; `now()` advances exactly | ✔ |
| 9 | `timeout(500)` fires on the virtual clock ⇒ `TIMEOUT` | ✔ |
| 10 | Validator emits all four issues with correct dotted/indexed paths | ✔ |
| 11 | A throwing listener does not break dispatch; later listeners still run | ✔ |

---

## 6. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **`types.ts` is a shared file — a C1 hazard.** Six agents all import it; if any edits it, they collide. | High | Orchestrator pre-seeds `src/types.ts` and `src/errors.ts` **verbatim from §5.1/§5.3 before dispatch**, and marks both read-only in the Wave 2 brief. They are declaration-only, so nothing forces an edit. Any needed change is an orchestrator decision, not an agent one. |
| R2 | Middleware sees `input`/result as `unknown`; there is exactly one `as unknown as Interlayer<C>` cast in the facade. | Medium | Confine it to `src/interlayer.ts`, comment it, and cover it with the §5.7 type tests so a regression shows up as a compile failure. |
| R3 | Re-entrant `next()` differs from koa; a contributor may assume koa semantics. | Medium | The `pending` guard rejects the actual dangerous case with an explicit message. Unit-test both branches (done). |
| R4 | Deriving contexts with `{...ctx}` silently drops mutations to primitive fields. | Medium | All shared mutable state lives behind object references (`stats`, `failures`, `state`). Documented in `CallStats`' doc comment. Add a test that a retry increments `stats.attempts` across derivations. |
| R5 | `AbortSignal` listener leaks on long-lived parent signals — a known Node footgun that core itself has hit. | Medium | Every `addEventListener` in §5.5 uses `{ once: true }` **and** a matching `dispose()`/`removeEventListener`. `deadline()` returns a handle whose `dispose` is called in a `finally`. Add a test asserting `pendingTimers === 0` after each call. |
| R6 | Lowest-common-denominator contract: the canonical shape can't express provider-specific features, pushing users off the layer. | Medium | `InvokeHints.providerOptions`, keyed by provider id — the AI SDK's escape hatch. Explicitly unvalidated and documented as advanced. |
| R7 | Unit 1 (kernel) imports factories from all five peers, so it cannot compile until they land. | Medium | Every factory's signature is fixed in §5 and mirrored in `types.ts`. Unit 1 codes against those signatures; typecheck runs at integration. Sequence Unit 1 last if agents finish at different times. |
| R8 | `exactOptionalPropertyTypes` makes `{ ...(x !== undefined ? { k: x } : {}) }` spreads necessary and easy to get wrong. | Low | Prefer `readonly k: T \| undefined` (required-but-undefined) on classes and records, as `errors.ts` and `CallStats` do. Only `InvokeHints`/`RegisterOptions` use true optionals. |
| R9 | `Symbol.for` brand is global-registry-wide; a future major version could clash. | Low | Marker is versioned: `'interlayer.error.v1'`. |
| R10 | Emitter dispatch is synchronous, so a slow listener adds latency to the call. | Low | Documented. Consumers who need async should queue. Keeps event ordering deterministic (C3), which is worth more here. |
| R11 | `retry` inside the breaker means one logical call can push the breaker over threshold on its own. | Low | Intended (Polly's default). Make it configurable via the breaker's `errorFilter`, and hand the decision to R4. |

---

## 7. Proposed `src/` file list and ownership

**Pre-seeded and FROZEN** (orchestrator writes verbatim from §5.1 and §5.3, then no agent edits):

| File | Responsibility |
|---|---|
| `src/types.ts` | Every cross-unit type and factory signature. Declarations only, zero runtime code. |
| `src/errors.ts` | Error hierarchy, `AnyInterlayerError` union, `isInterlayerError`/`hasCode`/`toInterlayerError`, `withContext`, `formatErrorChain`. |

**Six disjoint build units.** No file appears twice; no unit imports a peer's file except through
`types.ts`/`errors.ts` and the signatures fixed above.

| Unit | Owner | Files | Responsibility (one line each) |
|---|---|---|---|
| **1 — Kernel & facade** | A | `src/interlayer.ts` | `createInterlayer`: wires runtime/events/registry/router, builds both stacks, holds the single `as` cast. |
| | | `src/context.ts` | Builds `CallContext`/`AttemptContext`; deadline arithmetic; context derivation helpers. |
| | | `src/capability.ts` | `capability()` token factory and `CapabilityRef` helpers. |
| | | `src/index.ts` | Public barrel — the entire published surface, and nothing else. |
| **2 — Registry & providers** | B | `src/registry.ts` | `createRegistry`: id uniqueness, priority ordering, enable/disable, `candidatesFor`, disposal. |
| | | `src/provider.ts` | `defineProvider` helper, handler type-erasure (the one erasure site), capability introspection. |
| | | `src/health.ts` | Off-hot-path health probing and provider auto-disable policy. |
| **3 — Routing & fallback** | C | `src/router.ts` | `createRouter`: fallback chain, `shouldFallback`, single-failure unwrapping, aggregation. |
| | | `src/selectors.ts` | `inOrder`, `roundRobin`, `weighted`, `filterByTags`, `sticky` — all pure or `Runtime`-driven. |
| | | `src/result.ts` | `Result` helpers: `ok`, `err`, `unwrap`, `map`, `mapError`, `fromThrowable`. |
| **4 — Composition & core middleware** | D | `src/compose.ts` | `compose()` / `pipeline()` exactly as §5.2. |
| | | `src/pipeline.ts` | Builds the call and attempt stacks; canonical ordering; ordering validation. |
| | | `src/middleware/logging.ts` | Structured call/attempt logging middleware over the emitter. |
| | | `src/middleware/validate.ts` | Input/output validation middleware driven by `InterlayerOptions.validators`. |
| **5 — Resilience** *(consumes R4)* | E | `src/middleware/timeout.ts` | Per-attempt deadline via `runtime.deadline` + `next(ctx')`. |
| | | `src/middleware/retry.ts` | Re-entrant retry loop; delegates delay to `resilience/backoff.ts`. |
| | | `src/middleware/breaker.ts` | Wraps the pure state machine; emits `breaker:transition`; throws `CircuitOpenError`. |
| | | `src/middleware/ratelimit.ts` | Wraps the token bucket; emits `ratelimit:throttled`. |
| | | `src/middleware/bulkhead.ts` | Concurrency cap + queue; throws `BulkheadFullError`. |
| | | `src/resilience/backoff.ts` | Pure `(attempt, opts, random) => delayMs`; constant/exponential/decorrelated, none/full/equal jitter. |
| | | `src/resilience/breaker-state.ts` | Pure reducer `(state, event, now) => state`. No timers, no I/O — table-testable. |
| | | `src/resilience/token-bucket.ts` | Pure `(state, now, cost) => { allowed, waitMs, state }`. |
| **6 — Runtime, events, validation** | F | `src/runtime.ts` | `createSystemRuntime` / `systemRuntime` exactly as §5.5. |
| | | `src/events.ts` | `createEmitter` exactly as §5.4. |
| | | `src/validate.ts` | Combinator validators exactly as §5.6. |
| | | `src/config.ts` | `InterlayerOptions` schema built from `validate.ts`; `parseConfig` entry point. |
| | | `src/testing/test-runtime.ts` | `createTestRuntime`: virtual clock, seeded PRNG, counter uuid. |
| | | `src/testing/fake-provider.ts` | Scriptable provider (`succeed`, `failWith`, `delay`, call log) for every other unit's tests. |

24 files, 2 frozen + 22 owned, each by exactly one agent.

**Build order note.** Units 2–6 have no cross-dependencies and can start simultaneously. Unit 1
imports factories from all of them, but only through signatures already fixed in `types.ts`, so it
can be written in parallel and will typecheck at integration. `src/testing/fake-provider.ts`
(Unit 6) is on the critical path for other units' tests — have Unit 6 write it first.

---

## Sources

- [koajs/compose 4.1.0 — `index.js`](https://github.com/koajs/compose/blob/master/index.js) — the `dispatch`/`index` guard we deliberately relax.
- [Writing Middleware — koa](https://deepwiki.com/koajs/koa/3.1-writing-middleware) — onion semantics.
- [connor4312/cockatiel](https://github.com/connor4312/cockatiel) — `IPolicy`, `wrap`, backoff/jitter generators, disposable-returning events, `BrokenCircuitError`/`TaskCancelledError`/`BulkheadRejectedError`, `execute(fn, signal)`.
- [nodeshift/opossum](https://github.com/nodeshift/opossum) — `open`/`halfOpen`/`close` event vocabulary, `errorFilter` bookkeeping-vs-handling distinction.
- [Effect — Expected Errors / `Data.TaggedError`](https://effect.website/docs/error-management/expected-errors/) — `_tag` discriminant and typed error channel.
- [AI SDK — Provider & Model Management](https://vercel.com/templates/next.js/ai-sdk-provider-registry) and [`ProviderV2`](https://github.com/vercel/ai) — registry-by-string-id, `NoSuchModelError`, `providerOptions` escape hatch, `Symbol.for` error marker.
- [Standard Schema](https://standardschema.dev/) / [standard-schema/standard-schema](https://github.com/standard-schema/standard-schema) — the type-only `~standard` interface.
- [Hono — Factory Helper](https://hono.dev/docs/helpers/factory) — `MiddlewareHandler` typing and why generic middleware needs a factory.
- [Using AbortSignal in Node.js — Nearform](https://nearform.com/insights/using-abortsignal-in-node-js/) and [nodejs/node#46525](https://github.com/nodejs/node/issues/46525) — linked-signal listener leaks; `{ once: true }` + explicit removal.
- [Abstractions and the lowest common denominator](https://tante.cc/2009/02/15/abstractions-and-the-lowest-common-denominator/) / [Abstracting vendors in code — Test Double](https://testdouble.com/insights/abstracting-vendors-in-code) — the LCD failure mode motivating `providerOptions`.
