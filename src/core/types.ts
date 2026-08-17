/**
 * FROZEN CONTRACT SURFACE — declarations only, zero runtime code.
 *
 * Transcribed from R3 §5.1 with R6's public naming applied where the two
 * findings differ. The reconciliations, recorded once so nobody re-litigates:
 *
 *  | Concept              | R3                    | R6                  | Here                |
 *  |----------------------|-----------------------|---------------------|---------------------|
 *  | capability descriptor | `CapabilitySpec`      | `Capability<In,Out>`| **R6** (phantom)    |
 *  | contract constraint   | F-bounded `Contract<C>`| `ContractShape<C>` | **R3 form, R6 name**|
 *  | capability key union  | `CapabilityKey<C>`    | `CapabilityName<C>` | **R6**              |
 *  | provider identity     | `id`                  | `name`              | **R3 `id`** (†)     |
 *  | provider error code   | `'PROVIDER'`          | `'PROVIDER_ERROR'`  | **R6**              |
 *  | no-provider error     | `NoProviderAvailableError` | `NoProviderError` | **R6**         |
 *
 *  (†) `id` wins because `providerId` is already load-bearing across the error
 *  context and the whole event map, and because the U1 brief says "id
 *  uniqueness". One word, one meaning.
 *
 * R3's `CapabilityRef` branded token is DROPPED: R6's `capability()` returns a
 * capability *descriptor*, R3's returned a *token*, and one name cannot mean
 * both. Nothing in any unit brief needs the token form.
 */

import type { Clock, Random } from './clock.ts';

/* ---------- 1. Contract & capabilities ---------- */

declare const phantom: unique symbol;

/**
 * A single capability in a contract: a named (input, output) pair.
 *
 * `In`/`Out` are carried by a PHANTOM property that never exists at runtime.
 * `capability<In, Out>()` (owned by `src/registry/`) is the only place that
 * mints one, via the single cast in the design. Do not materialise
 * `[phantom]` — the whole inference technique depends on it staying type-only.
 */
export interface Capability<In = unknown, Out = unknown> {
  /** Type-only carrier. Never present at runtime. */
  readonly [phantom]?: { in: In; out: Out } | undefined;
  readonly idempotent?: boolean | undefined;
  readonly description?: string | undefined;
}

/**
 * F-BOUNDED constraint. Always write `C extends Contract<C>`, never
 * `C extends AnyContract`, and never `interface MyContract extends AnyContract`.
 *
 * Why: `Readonly<Record<string, Capability>>` carries a string index signature.
 * An interface that `extends` it inherits that signature, so `CapabilityName<C>`
 * widens from `'chat' | 'embed'` to `string` and every call site silently loses
 * type checking. Measured by BOTH R3 and R6 independently: with the naive
 * constraint a `@ts-expect-error` on a bogus capability name reported
 * `TS2578: Unused '@ts-expect-error' directive` — i.e. no error was raised.
 *
 * The F-bounded form imposes the same shape, preserves the literal key union,
 * and accepts `interface` AND `type` declarations alike (a bare `interface`
 * does not satisfy an index-signature constraint at all).
 */
export type Contract<C> = { readonly [K in keyof C]: Capability<unknown, unknown> };

/** Erased, index-signature form. Internal use only; never a public constraint. */
export type AnyContract = Readonly<Record<string, Capability<unknown, unknown>>>;

/** The literal union of capability names declared by a contract. */
export type CapabilityName<C> = Extract<keyof C, string>;

/** Indexed access into the phantom carrier — no conditional `infer` needed. */
export type InputOf<C extends Contract<C>, K extends keyof C> = NonNullable<
  C[K][typeof phantom]
>['in'];
export type OutputOf<C extends Contract<C>, K extends keyof C> = NonNullable<
  C[K][typeof phantom]
>['out'];

/* ---------- 2. Determinism seams ---------- */

/**
 * The single door to time, randomness and identity. Injected at construction
 * AND carried on every context, so middleware never reaches for a global.
 */
export interface Runtime extends Clock {
  /** Epoch milliseconds. The ONLY source of time in the library. */
  now(): number;
  /** Cancellable delay. Rejects with `CancelledError` if `signal` aborts. */
  sleep(ms: number, signal?: AbortSignal): Promise<void>;
  /** Uniform [0, 1). The ONLY source of randomness (jitter, weighted routing). */
  random: Random;
  /** Correlation id for a call. The ONLY source of identity. */
  uuid(): string;
  /** Signal that aborts after `ms`, linked to `parent`. `dispose` MUST be called. */
  deadline(ms: number, parent?: AbortSignal): DeadlineHandle;
}

export interface DeadlineHandle {
  readonly signal: AbortSignal;
  dispose(): void;
}

/** Virtual-clock runtime. Implemented by `test/support/fake-clock.ts`. */
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

/**
 * MUST be a `type`, not an `interface`: interfaces have no implicit index
 * signature and so do not satisfy the `EventMap` constraint.
 */
export type InterlayerEvents = {
  'call:start': { readonly callId: string; readonly capability: string; readonly at: number };
  'call:success': {
    readonly callId: string;
    readonly capability: string;
    readonly providerId: string;
    readonly attempts: number;
    readonly durationMs: number;
    readonly at: number;
  };
  'call:failure': {
    readonly callId: string;
    readonly capability: string;
    readonly error: AnyInterlayerError;
    readonly attempts: number;
    readonly durationMs: number;
    readonly at: number;
  };
  'provider:selected': {
    readonly callId: string;
    readonly capability: string;
    readonly providerId: string;
    readonly candidateIds: readonly string[];
    readonly index: number;
    readonly at: number;
  };
  'attempt:start': {
    readonly callId: string;
    readonly providerId: string;
    readonly attempt: number;
    readonly at: number;
  };
  'attempt:success': {
    readonly callId: string;
    readonly providerId: string;
    readonly attempt: number;
    readonly durationMs: number;
    readonly at: number;
  };
  'attempt:failure': {
    readonly callId: string;
    readonly providerId: string;
    readonly attempt: number;
    readonly durationMs: number;
    readonly error: AnyInterlayerError;
    readonly willRetry: boolean;
    readonly at: number;
  };
  'retry:scheduled': {
    readonly callId: string;
    readonly providerId: string;
    readonly attempt: number;
    readonly delayMs: number;
    readonly at: number;
  };
  'breaker:transition': {
    readonly key: string;
    readonly from: BreakerState;
    readonly to: BreakerState;
    readonly failures: number;
    readonly at: number;
  };
  'ratelimit:throttled': { readonly key: string; readonly waitMs: number; readonly at: number };
  'fallback:advance': {
    readonly callId: string;
    readonly fromProviderId: string;
    readonly toProviderId: string;
    readonly reason: ErrorCode;
    readonly at: number;
  };
  'listener:error': { readonly event: string; readonly error: unknown; readonly at: number };
};

/** Alias so error messages print a name, not a raw `keyof SomeGeneric<…>` (R6 §5.1). */
export type InterlayerEventName = Extract<keyof InterlayerEvents, string>;

/* ---------- 4. Error codes (classes live in errors.ts) ---------- */

export type ErrorCode =
  | 'VALIDATION'
  | 'CONFIG'
  | 'UNSUPPORTED_CAPABILITY'
  | 'NO_PROVIDER'
  | 'TRANSPORT'
  | 'TIMEOUT'
  | 'CANCELLED'
  | 'RATE_LIMITED'
  | 'CIRCUIT_OPEN'
  | 'BULKHEAD_FULL'
  | 'PROVIDER_ERROR'
  | 'ALL_FAILED';

export interface ErrorContext {
  readonly providerId?: string | undefined;
  readonly capability?: string | undefined;
  readonly callId?: string | undefined;
  readonly attempt?: number | undefined;
  readonly details?: Readonly<Record<string, unknown>> | undefined;
  readonly cause?: unknown;
  readonly retryable?: boolean | undefined;
}

/** Forward reference; `errors.ts` supplies the concrete union. Type-only, erased. */
export type AnyInterlayerError = import('./errors.ts').AnyInterlayerError;

/* ---------- 5. Result ---------- */

export type Result<T, E = AnyInterlayerError> =
  | { readonly ok: true; readonly value: T }
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
  readonly hints: CallOptions;
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

/**
 * Per-call overrides. Every optional is `?: T | undefined` so a consumer on
 * `exactOptionalPropertyTypes` can forward an optional straight through
 * (R1 §5.2 / R6 pitfall 13).
 */
export interface CallOptions {
  readonly timeoutMs?: number | undefined;
  readonly deadlineMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
  /** Restrict candidates to these provider ids, in this order. */
  readonly providers?: readonly string[] | undefined;
  /** Require providers carrying all of these tags. */
  readonly tags?: readonly string[] | undefined;
  /** Stable key for sticky/consistent selection. */
  readonly stickyKey?: string | undefined;
  readonly metadata?: Readonly<Record<string, unknown>> | undefined;
  /**
   * ESCAPE HATCH, keyed by provider id. Lets a caller reach a provider-specific
   * feature the canonical contract does not model, without widening the
   * contract to the lowest common denominator. Unvalidated by design.
   */
  readonly providerOptions?:
    | Readonly<Record<string, Readonly<Record<string, unknown>>>>
    | undefined;
}

/* ---------- 7. Providers & registry ---------- */

export type Handler<C extends Contract<C>, K extends keyof C> = (
  input: InputOf<C, K>,
  ctx: AttemptContext,
) => Promise<OutputOf<C, K>>;

export type Handlers<C extends Contract<C>> = { [K in keyof C]: Handler<C, K> };

export type ErasedHandler = (input: unknown, ctx: AttemptContext) => Promise<unknown>;

export interface ProviderTraits {
  readonly tags?: readonly string[] | undefined;
  /** Lower is preferred by cost-aware selectors. Arbitrary units. */
  readonly costPerCall?: number | undefined;
  /** Advisory p50 latency in ms, used by latency-aware selectors. */
  readonly latencyMs?: number | undefined;
  readonly region?: string | undefined;
}

export interface HealthStatus {
  readonly healthy: boolean;
  readonly detail?: string | undefined;
}

/**
 * A backend implementation of some subset of a contract.
 *
 * `Id` and `H` are inferred, never annotated. Annotating a provider array as
 * `Provider<C>[]` collapses `Id` to `string` AND over-reports the implemented
 * capability set — it fails OPEN (R6 R1). Use `satisfies`, and let U1's
 * runtime validation be the backstop.
 */
export interface Provider<
  C extends Contract<C>,
  Id extends string = string,
  H extends Partial<Handlers<C>> = Partial<Handlers<C>>,
> {
  readonly id: Id;
  readonly capabilities: H;
  readonly traits?: ProviderTraits | undefined;
  /** Optional liveness probe; never called on the hot path. */
  readonly health?:
    | ((ctx: { runtime: Runtime; signal: AbortSignal }) => Promise<HealthStatus>)
    | undefined;
  readonly dispose?: (() => void | Promise<void>) | undefined;
}

/** The literal id union of a provider (tuple element) type. Distributive. */
export type ProviderId<P> = P extends { id: infer I extends string } ? I : never;
/** The literal capability union a provider (tuple element) actually implements. */
export type ImplementedBy<P> = P extends { capabilities: infer H }
  ? Extract<keyof H, string>
  : never;

/** Runtime-side, type-erased provider entry held by the registry. */
export interface ProviderRecord {
  readonly id: string;
  readonly capabilities: ReadonlyMap<string, ErasedHandler>;
  readonly traits: ProviderTraits;
  readonly priority: number;
  enabled: boolean;
  readonly health?:
    | ((ctx: { runtime: Runtime; signal: AbortSignal }) => Promise<HealthStatus>)
    | undefined;
  readonly dispose?: (() => void | Promise<void>) | undefined;
}

export interface RegisterOptions {
  /** Lower runs first in the default `inOrder` selector. Default: registration index. */
  readonly priority?: number | undefined;
  readonly enabled?: boolean | undefined;
  readonly traits?: ProviderTraits | undefined;
}

export interface Registry<C extends Contract<C>> {
  register<Id extends string, H extends Partial<Handlers<C>>>(
    provider: Provider<C, Id, H>,
    opts?: RegisterOptions,
  ): this;
  unregister(providerId: string): boolean;
  get(providerId: string): ProviderRecord | undefined;
  /** Enabled providers declaring `capability`, sorted by priority then registration order. */
  candidatesFor(capability: string): readonly ProviderRecord[];
  list(): readonly ProviderRecord[];
  setEnabled(providerId: string, enabled: boolean): boolean;
  supports(capability: string): boolean;
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
  readonly selector?: Selector | undefined;
  /** Max providers tried per call. Default: Infinity. */
  readonly maxProviders?: number | undefined;
  /** Decides whether the next provider should be tried. Default: `err.retryable`. */
  readonly shouldFallback?: ((error: AnyInterlayerError, ctx: CallContext) => boolean) | undefined;
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
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly issues: readonly Issue[] };

export interface Validator<T> {
  /** Human-readable type name used in messages, e.g. `string`, `{ a: number }`. */
  readonly typeName: string;
  validate(value: unknown, path?: readonly PathSegment[]): ValidationResult<T>;
}

export type Infer<V> = V extends Validator<infer T> ? T : never;

/* ---------- 11. Public facade ---------- */

/** What `callWithMeta` returns: the value plus what actually happened. */
export interface CallResult<T> {
  readonly value: T;
  readonly providerId: string;
  readonly attempts: number;
  readonly durationMs: number;
  /** Every suppressed failure, in order. A fallback never swallows a cause. */
  readonly errors: readonly AnyInterlayerError[];
}

/**
 * The public facade. `src/layer.ts` (ORCH-POST) implements it; units code
 * against the pieces, never against this.
 */
export interface Layer<
  C extends Contract<C>,
  Ids extends string = string,
  Impl extends CapabilityName<C> = CapabilityName<C>,
> {
  readonly contract: C;
  readonly registry: Registry<C>;
  readonly events: Emitter<InterlayerEvents>;
  readonly runtime: Runtime;

  /** Throwing form. Rejects with an `InterlayerError` subclass, always. */
  call<K extends Impl>(
    capability: K,
    input: InputOf<C, K>,
    options?: CallOptions,
  ): Promise<OutputOf<C, K>>;

  /** Same call, with the metadata. Not an overload — a separate verb (R6). */
  callWithMeta<K extends Impl>(
    capability: K,
    input: InputOf<C, K>,
    options?: CallOptions,
  ): Promise<CallResult<OutputOf<C, K>>>;

  /** Non-throwing form. Never rejects for domain failures. */
  tryCall<K extends Impl>(
    capability: K,
    input: InputOf<C, K>,
    options?: CallOptions,
  ): Promise<Result<OutputOf<C, K>>>;

  /** Pin the chain to a subset of registered providers. */
  only(...ids: readonly Ids[]): Layer<C, Ids, Impl>;

  supports(capability: string): capability is Impl;
  on<K extends InterlayerEventName>(
    event: K,
    fn: (payload: InterlayerEvents[K]) => void,
  ): Unsubscribe;
  use(middleware: CallMiddleware): this;
  close(): Promise<void>;
}
