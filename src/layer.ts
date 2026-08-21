/**
 * `createLayer` — THE INTEGRATION POINT.
 *
 * Six units were built in parallel against `src/core/**` and never against each
 * other (setup guide §2.1). This is the file where they finally meet: the
 * registry supplies candidates, routing walks them, and the four resilience
 * policies wrap each attempt. Nothing below this line knows that any of the
 * others exist.
 *
 * ── THE TWO STACKS (R3 §2.1) ──────────────────────────────────────────────
 *
 * ```
 *   layer.call(cap, input, opts)
 *   └── CALL STACK        compose<CallContext>      once per call
 *       ├── TotalTimeout                            bounds the whole operation
 *       ├── [user middleware from .use()]           kind 'custom', sorts inside
 *       └── TERMINAL: router.routeWith(ctx, candidates, handler)
 *           └── for each candidate, in selector order:
 *               ATTEMPT STACK  compose<AttemptContext>   once per provider
 *               ├── Retry                           re-entrant: calls next() N times
 *               ├── RateLimit                       one token per PHYSICAL call
 *               ├── CircuitBreaker                  sees every physical call
 *               ├── AttemptTimeout                  bounds ONE provider call
 *               └── TERMINAL: provider.capabilities.get(cap)(input, ctx)
 * ```
 *
 * The order is NOT hand-written here. `POLICY_ORDER` / `POLICY_SCOPE` /
 * `policyStack()` in `src/core/policy.ts` own it (R4 §5.6, settled), so a
 * policy added later lands in the right place without this file changing.
 *
 * ── PER-PROVIDER STATE ────────────────────────────────────────────────────
 *
 * A dead provider must never fail-fast a healthy sibling in the same fallback
 * chain. The breaker keys its own state on `ctx.provider.id` internally, so one
 * instance serves every provider. The rate limiter does NOT — it owns a single
 * token bucket — so this file gives each provider its own limiter through
 * {@link perProviderPolicy}.
 *
 * ── TYPE ERASURE ──────────────────────────────────────────────────────────
 *
 * Above the facade every value is precisely typed; below it the middleware
 * onion moves `unknown`. Handler erasure happens once, in `src/registry/`.
 * This file adds exactly THREE casts — two re-attaching the output type that
 * the onion erased, one widening a literal-keyed config record to a runtime
 * string key — each commented at its call site with why it is unavoidable.
 * There are no `any`s and no non-null assertions anywhere.
 */

import { pipeline } from './core/compose.ts';
import {
  CancelledError,
  isInterlayerError,
  TimeoutError,
  toInterlayerError,
  UnsupportedCapabilityError,
} from './core/errors.ts';
import { createEmitter } from './core/events.ts';
import {
  type CallPolicy,
  definePolicy,
  disposePolicies,
  type Policy,
  policyStack,
  type ResilienceOptions,
  type RetryOptions,
} from './core/policy.ts';
import { systemRuntime } from './core/runtime.ts';
import type {
  AnyInterlayerError,
  AttemptContext,
  CallContext,
  CallMiddleware,
  CallOptions,
  CallResult,
  CallStats,
  CapabilityName,
  Contract,
  Emitter,
  Handlers,
  ImplementedBy,
  InputOf,
  InterlayerEventName,
  InterlayerEvents,
  Layer,
  OutputOf,
  Provider,
  ProviderId,
  ProviderRecord,
  Registry,
  Result,
  Runtime,
  Selector,
  Unsubscribe,
} from './core/types.ts';
import { createRegistry } from './registry/index.ts';
import { circuitBreaker } from './resilience/circuit-breaker/index.ts';
import { rateLimit } from './resilience/rate-limit/index.ts';
import { isTransient, resolveRetryOptions, retryPolicy } from './resilience/retry/index.ts';
import { attemptTimeout, totalTimeout } from './resilience/timeout/index.ts';
import { createRouter, type PolicyRouter } from './routing/index.ts';

/* ------------------------------------------------------------------ *
 * 1. Configuration
 * ------------------------------------------------------------------ */

/** Per-capability resilience overrides, keyed by capability name. */
export type PerCapabilityResilience<C extends Contract<C>> = Partial<
  Record<CapabilityName<C>, ResilienceOptions>
>;

/**
 * What `createLayer` takes.
 *
 * `providers` is inferred as a TUPLE (`const P`), which is what preserves each
 * provider's literal id and its exact implemented capability set. Do NOT
 * annotate the array as `Provider<C>[]` at the call site: that collapses the
 * ids to `string` AND fills the handler map with the whole contract, so the
 * layer claims capabilities nobody implements — it fails OPEN (R6 R1). The
 * registry re-derives everything from the runtime value as the backstop.
 */
export interface LayerConfig<
  C extends Contract<C>,
  P extends readonly Provider<C, string, Partial<Handlers<C>>>[],
> {
  readonly contract: C;
  /** Array order is the default fallback order. */
  readonly providers: P;
  /**
   * Defaults for every capability. A policy set to `false` is switched off.
   *
   * ALL FOUR POLICIES ARE ON BY DEFAULT, with `DEFAULTS` from `core/policy.ts`
   * (R4 §0.1). `false` is the only way to remove one — that is what the
   * `T | false | undefined` shape of every field means, and it is why
   * `retry: { maxAttempts: 0 }` is a `RangeError` rather than an off switch.
   *
   * The one worth knowing about: `rateLimit` defaults to a token bucket of
   * 10 tokens refilling at 10/s PER PROVIDER, in `'wait'` mode. Below 10 calls
   * a second per provider it is invisible; above that it PACES rather than
   * fails. Unlike the other three it encodes an external fact — the provider's
   * quota — that this library cannot know, so set it to your real quota or pass
   * `rateLimit: false`.
   */
  readonly resilience?: ResilienceOptions | undefined;
  /**
   * Overrides for named capabilities. An entry REPLACES the default for that
   * capability rather than merging into it, so `{ retry: false }` really means
   * "no retries here" and not "the default retry, minus nothing".
   *
   * A capability with an override gets its OWN breaker and limiter instances:
   * asking for different limits is asking for different accounting.
   */
  readonly perCapability?: PerCapabilityResilience<C> | undefined;
  /** Candidate ordering. Default `inOrder` — registration order. */
  readonly selector?: Selector | undefined;
  /** Max providers tried per call. Default: unlimited. */
  readonly maxProviders?: number | undefined;
  /** Overrides the default `error.retryable` fallback rule. */
  readonly shouldFallback?: ((error: AnyInterlayerError, ctx: CallContext) => boolean) | undefined;
  /**
   * The determinism seam (C3). Defaults to {@link systemRuntime}; tests pass
   * `createFakeRuntime()` and a one-hour timeout settles in microseconds.
   */
  readonly runtime?: Runtime | undefined;
  /** Share an emitter across layers, e.g. to fan every event into one sink. */
  readonly events?: Emitter<InterlayerEvents> | undefined;
}

/** The capability names at least one of `P` actually implements. */
export type ImplementedCapabilities<
  C extends Contract<C>,
  P extends readonly Provider<C, string, Partial<Handlers<C>>>[],
> = Extract<ImplementedBy<P[number]>, CapabilityName<C>>;

/** The literal id union of `P`. */
export type ProviderIds<
  C extends Contract<C>,
  P extends readonly Provider<C, string, Partial<Handlers<C>>>[],
> = ProviderId<P[number]>;

/* ------------------------------------------------------------------ *
 * 2. Policy assembly
 * ------------------------------------------------------------------ */

/**
 * Gives every provider its own instance of an attempt policy.
 *
 * The circuit breaker does its own per-key bookkeeping, but the rate limiter
 * owns exactly one token bucket, so a single shared limiter would meter the
 * SUM of every backend's traffic against one quota — and a fallback chain would
 * spend openai's tokens discovering that anthropic is busy. One limiter per
 * provider is the only correct arrangement.
 *
 * Instances are created lazily on first use and disposed together.
 */
function perProviderPolicy(
  name: string,
  make: (providerId: string) => Policy<AttemptContext, unknown>,
): Policy<AttemptContext, unknown> {
  const instances = new Map<string, Policy<AttemptContext, unknown>>();
  const forProvider = (providerId: string): Policy<AttemptContext, unknown> => {
    const existing = instances.get(providerId);
    if (existing !== undefined) return existing;
    const created = make(providerId);
    instances.set(providerId, created);
    return created;
  };

  // `kind` and `scope` are read off one throwaway instance, so this wrapper
  // never has to be told where it sorts in `POLICY_ORDER`.
  const probe = make('<probe>');
  return definePolicy<AttemptContext, unknown>({
    kind: probe.kind,
    name,
    scope: probe.scope,
    execute: (ctx, next) => forProvider(ctx.provider.id).execute(ctx, next),
    describe: (): Readonly<Record<string, unknown>> =>
      Object.fromEntries(
        [...instances].map(([id, policy]) => [id, policy.describe?.() ?? {}] as const),
      ),
    dispose: async (): Promise<void> => {
      await disposePolicies([...instances.values()]);
      instances.clear();
    },
  });
}

/** What the terminal needs in order to predict `willRetry` on the event. */
interface RetryAdvice {
  readonly maxAttempts: number;
  readonly isRetryable: (error: unknown, attempt: number) => boolean;
}

/** One capability's fully wired pipeline. */
interface Stack {
  readonly callPolicies: readonly CallPolicy[];
  readonly router: PolicyRouter;
  /** `undefined` when retry is switched off for this capability. */
  readonly retry: RetryAdvice | undefined;
  dispose(): Promise<void>;
}

interface StackInput {
  readonly resilience: ResilienceOptions;
  readonly selector: Selector | undefined;
  readonly maxProviders: number | undefined;
  readonly shouldFallback: ((error: AnyInterlayerError, ctx: CallContext) => boolean) | undefined;
}

/**
 * Builds one capability's two stacks from its resilience options.
 *
 * Every policy is created unconditionally UNLESS its option is `false` — R6's
 * rule that `false` disables and `undefined` means "use the default", so
 * `retry: { maxAttempts: 0 }` is never how you turn something off (it is a
 * `RangeError`).
 *
 * Nothing here decides the nesting order; `createRouter` runs the attempt
 * policies through `policyStack()` and the call stack does the same below.
 */
function buildStack(input: StackInput): Stack {
  const { resilience } = input;
  const attemptPolicies: Policy<AttemptContext, unknown>[] = [];
  const callPolicies: CallPolicy[] = [];

  let retry: RetryAdvice | undefined;
  if (resilience.retry !== false) {
    const options: RetryOptions = resilience.retry ?? {};
    attemptPolicies.push(retryPolicy(options));
    const resolved = resolveRetryOptions(options);
    retry = { maxAttempts: resolved.maxAttempts, isRetryable: options.isRetryable ?? isTransient };
  }
  if (resilience.rateLimit !== false) {
    const options = resilience.rateLimit ?? {};
    attemptPolicies.push(
      perProviderPolicy('rate-limit(per-provider)', (providerId) =>
        rateLimit({ ...options, key: providerId }),
      ),
    );
  }
  if (resilience.breaker !== false) {
    // One instance for every provider: the breaker keys on `ctx.provider.id`.
    attemptPolicies.push(circuitBreaker(resilience.breaker ?? {}));
  }
  if (resilience.timeout !== false) {
    const options = resilience.timeout ?? {};
    attemptPolicies.push(attemptTimeout(options));
    callPolicies.push(totalTimeout(options));
  }

  const router = createRouter({
    policies: attemptPolicies,
    ...(input.selector !== undefined ? { selector: input.selector } : {}),
    ...(input.maxProviders !== undefined ? { maxProviders: input.maxProviders } : {}),
    ...(input.shouldFallback !== undefined ? { shouldFallback: input.shouldFallback } : {}),
  });

  return {
    callPolicies,
    router,
    retry,
    async dispose(): Promise<void> {
      await router.dispose();
      await disposePolicies(callPolicies);
    },
  };
}

/* ------------------------------------------------------------------ *
 * 3. The facade
 * ------------------------------------------------------------------ */

/** Turns user middleware into a call-scope policy so `policyStack()` places it. */
function asCallPolicy(middleware: CallMiddleware, index: number): CallPolicy {
  return definePolicy<CallContext, unknown>({
    kind: 'custom', // sorts after every built-in, in declaration order
    name: `middleware[${String(index)}]`,
    scope: 'call',
    execute: middleware,
  });
}

/** A signal that never aborts — cheaper and clearer than threading `undefined`. */
const NEVER_ABORTS: AbortSignal = new AbortController().signal;

/** Why a signal aborted, as a typed error. A typed reason IS the answer. */
function abortErrorOf(signal: AbortSignal): AnyInterlayerError {
  const reason: unknown = signal.reason;
  return isInterlayerError(reason)
    ? reason
    : new CancelledError('Call was cancelled', { cause: reason });
}

/**
 * Rejects as soon as `signal` aborts, even if `work` never settles.
 *
 * Handing the deadline to the context is not enough on its own: a provider that
 * ignores `ctx.signal` — and plenty do — keeps the call hanging forever, and
 * with `timeout: false` there is no policy wrapping it to notice. Something has
 * to be watching the signal, and with the policies switched off the facade is
 * the only thing left. The orphaned work is then the operation's problem
 * (R4 §2.2); the caller's deadline is not.
 *
 * The rejection handler is attached UNCONDITIONALLY, so a late failure arriving
 * after the deadline fired is absorbed rather than becoming an unhandled
 * rejection, and the listener is removed on every path.
 */
function raceSignal<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = (): void => {
      reject(abortErrorOf(signal));
    };
    signal.addEventListener('abort', onAbort, { once: true });
    void work.then(
      (value) => {
        signal.removeEventListener('abort', onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      },
    );
  });
}

/**
 * Wires a registry, a router and the resilience policies into a {@link Layer}.
 *
 * ```ts
 * const layer = createLayer({
 *   contract: ai,
 *   providers: [openai, anthropic],
 *   resilience: { retry: { maxAttempts: 3 }, timeout: { attemptTimeoutMs: 2_000 } },
 * });
 *
 * const reply = await layer.call('chat', { messages });   // typed both ways
 * await layer.close();
 * ```
 */
export function createLayer<
  C extends Contract<C>,
  const P extends readonly Provider<C, string, Partial<Handlers<C>>>[],
>(config: LayerConfig<C, P>): Layer<C, ProviderIds<C, P>, ImplementedCapabilities<C, P>> {
  type Impl = ImplementedCapabilities<C, P>;
  type Ids = ProviderIds<C, P>;

  const runtime: Runtime = config.runtime ?? systemRuntime;
  const events: Emitter<InterlayerEvents> = config.events ?? createEmitter<InterlayerEvents>();
  const registry: Registry<C> = createRegistry<C>({ contract: config.contract });
  for (const provider of config.providers) registry.register(provider);

  const middleware: CallMiddleware[] = [];
  const stackInput = {
    selector: config.selector,
    maxProviders: config.maxProviders,
    shouldFallback: config.shouldFallback,
  };
  const defaultStack: Stack = buildStack({ resilience: config.resilience ?? {}, ...stackInput });
  /** Only capabilities with a `perCapability` override get their own stack. */
  const overrides = new Map<string, Stack>();
  // CAST 3 of 3, and the only one that is not on the output edge.
  // `PerCapabilityResilience<C>` is `Partial<Record<CapabilityName<C>, …>>`: a
  // record keyed by a LITERAL UNION, with no index signature, so it cannot be
  // read with the plain `string` the call path carries. `Object.entries` does
  // not rescue it either — over a generic mapped type it degrades to
  // `[string, {} | null][]`, which is worse than the cast. Widening the key is
  // sound because the VALUE type is untouched and a miss simply falls through
  // to the default stack.
  const perCapability = config.perCapability as
    | Readonly<Record<string, ResilienceOptions | undefined>>
    | undefined;
  let closed = false;

  function stackFor(capability: string): Stack {
    const configured = perCapability?.[capability];
    if (configured === undefined) return defaultStack;
    const existing = overrides.get(capability);
    if (existing !== undefined) return existing;
    const built = buildStack({ resilience: configured, ...stackInput });
    overrides.set(capability, built);
    return built;
  }

  /**
   * Runs one physical provider call and owns the `attempt:*` events.
   *
   * This is the only place in the library that sees the handler boundary, so it
   * is the only place those three events can honestly be emitted: the router
   * deliberately emits only `provider:selected` / `fallback:advance`, and retry
   * only `retry:scheduled`.
   */
  function runHandler(ctx: AttemptContext, advice: RetryAdvice | undefined): Promise<unknown> {
    const handler = ctx.provider.capabilities.get(ctx.capability);
    if (handler === undefined) {
      // Unreachable through `candidatesFor`, which indexes by capability; kept
      // because a caller may unregister a provider mid-flight.
      return Promise.reject(
        new UnsupportedCapabilityError(ctx.capability, {
          providerId: ctx.provider.id,
          callId: ctx.callId,
        }),
      );
    }
    const startedAt = ctx.runtime.now();
    events.emit('attempt:start', {
      callId: ctx.callId,
      providerId: ctx.provider.id,
      attempt: ctx.attempt,
      at: startedAt,
    });
    return handler(ctx.input, ctx).then(
      (value) => {
        events.emit('attempt:success', {
          callId: ctx.callId,
          providerId: ctx.provider.id,
          attempt: ctx.attempt,
          durationMs: ctx.runtime.now() - startedAt,
          at: ctx.runtime.now(),
        });
        return value;
      },
      (raw: unknown) => {
        const error = toInterlayerError(raw, {
          providerId: ctx.provider.id,
          capability: ctx.capability,
          callId: ctx.callId,
          attempt: ctx.attempt,
        });
        events.emit('attempt:failure', {
          callId: ctx.callId,
          providerId: ctx.provider.id,
          attempt: ctx.attempt,
          durationMs: ctx.runtime.now() - startedAt,
          error,
          // ADVISORY. Computed with the very predicate the retry policy will
          // use, so it agrees with it on every input — but retry may still stop
          // short on its wall-clock budget, which is not knowable from here.
          willRetry:
            advice !== undefined &&
            ctx.attempt < advice.maxAttempts &&
            advice.isRetryable(error, ctx.attempt),
          at: ctx.runtime.now(),
        });
        throw raw;
      },
    );
  }

  /** The single call path. `call`, `callWithMeta` and `tryCall` all land here. */
  async function invoke(
    capability: string,
    input: unknown,
    options: CallOptions,
    pinned: readonly string[] | undefined,
  ): Promise<CallResult<unknown>> {
    // Nothing declares it at all -> UNSUPPORTED_CAPABILITY. Declared but none
    // available right now -> NO_PROVIDER, raised by the router. Two facts, two
    // errors; folding them together loses the distinction.
    if (!registry.supports(capability)) throw new UnsupportedCapabilityError(capability);

    const stack = stackFor(capability);
    const callId = runtime.uuid();
    const startedAt = runtime.now();
    const hints: CallOptions = {
      ...options,
      ...(pinned === undefined ? {} : { providers: intersectPins(pinned, options.providers) }),
    };

    // `deadlineAt` is an ABSOLUTE instant (core `CallOptions`). Arming it here
    // rather than leaving it to the total-timeout policy is what makes it hold
    // even with every resilience policy switched off. It aborts with a
    // `TimeoutError`, so a breach surfaces as TIMEOUT and never as CANCELLED.
    const deadlineAt = options.deadlineAt;
    const callerSignal = options.signal ?? NEVER_ABORTS;
    if (deadlineAt !== undefined && deadlineAt <= startedAt) {
      // Already spent. Fail before entering a provider rather than arming a
      // 0 ms timer, which would not fire until a later macrotask anyway.
      throw new TimeoutError(0, 'call', {
        capability,
        callId,
        details: { deadlineAt, now: startedAt },
      });
    }
    const deadline =
      deadlineAt === undefined ? undefined : runtime.deadline(deadlineAt - startedAt, callerSignal);

    const stats: CallStats = { attempts: 0, providersTried: 0, winnerId: undefined };
    const failures: AnyInterlayerError[] = [];
    const ctx: CallContext = {
      callId,
      capability,
      input,
      startedAt,
      deadlineAt,
      signal: deadline?.signal ?? callerSignal,
      runtime,
      events,
      state: new Map<string, unknown>(),
      hints,
      failures,
      stats,
    };

    events.emit('call:start', { callId, capability, at: startedAt });

    // TotalTimeout, then user middleware, then the router. The array order is
    // `policyStack()`'s doing, not this file's — see POLICY_ORDER.
    const run = pipeline<CallContext, unknown>(
      policyStack([...stack.callPolicies, ...middleware.map(asCallPolicy)], 'call'),
      (callCtx: CallContext): Promise<unknown> =>
        stack.router.routeWith(
          callCtx,
          registry.candidatesFor(callCtx.capability),
          (_provider, attemptCtx) => runHandler(attemptCtx, stack.retry),
        ),
    );

    try {
      // With a deadline armed the facade watches the signal itself; without one
      // there is nothing extra to race and the pipeline is awaited directly.
      const value =
        deadline === undefined ? await run(ctx) : await raceSignal(run(ctx), deadline.signal);
      const durationMs = runtime.now() - startedAt;
      const providerId = stats.winnerId ?? '<unknown>';
      events.emit('call:success', {
        callId,
        capability,
        providerId,
        attempts: stats.attempts,
        durationMs,
        at: runtime.now(),
      });
      return {
        value,
        providerId,
        provider: providerId,
        attempts: stats.attempts,
        durationMs,
        errors: [...failures],
      };
    } catch (raw) {
      const error = toInterlayerError(raw, { capability, callId });
      events.emit('call:failure', {
        callId,
        capability,
        error,
        attempts: stats.attempts,
        durationMs: runtime.now() - startedAt,
        at: runtime.now(),
      });
      throw error;
    } finally {
      deadline?.dispose(); // no timer left holding the event loop open
    }
  }

  /** Builds one facade object; `only()` produces another over the same state. */
  function view(pinned: readonly string[] | undefined): Layer<C, Ids, Impl> {
    const layer: Layer<C, Ids, Impl> = {
      contract: config.contract,
      registry,
      events,
      runtime,

      get providers(): readonly ProviderRecord[] {
        const all = registry.list();
        if (pinned === undefined) return all;
        // Pinned ORDER is meaningful — it is the fallback order the caller asked
        // for — so walk the pins, not the registry.
        return pinned.flatMap((id) => {
          const record = registry.get(id);
          return record === undefined ? [] : [record];
        });
      },

      async call<K extends Impl>(
        capability: K,
        input: InputOf<C, K>,
        options: CallOptions = {},
      ): Promise<OutputOf<C, K>> {
        const result = await invoke(capability, input, options, pinned);
        // CAST 1 of 2. `CallContext.input` and the composed pipeline are
        // `unknown` by construction — the onion moves values it cannot name —
        // and the handler that produced this one was type-erased in
        // `src/registry/provider.ts`. The capability key `K` is what re-attaches
        // the type; the registry's runtime validation is what makes that safe.
        return result.value as OutputOf<C, K>;
      },

      async callWithMeta<K extends Impl>(
        capability: K,
        input: InputOf<C, K>,
        options: CallOptions = {},
      ): Promise<CallResult<OutputOf<C, K>>> {
        const result = await invoke(capability, input, options, pinned);
        // CAST 2 of 2. Identical justification to cast 1, on the same value —
        // `CallResult<unknown>` narrowed to `CallResult<OutputOf<C, K>>`.
        return result as CallResult<OutputOf<C, K>>;
      },

      async tryCall<K extends Impl>(
        capability: K,
        input: InputOf<C, K>,
        options: CallOptions = {},
      ): Promise<Result<OutputOf<C, K>>> {
        try {
          return { ok: true, value: await layer.call(capability, input, options) };
        } catch (error) {
          // `invoke` funnels every throw through `toInterlayerError`, so this is
          // already typed; the guard is belt-and-braces for a middleware that
          // rethrows something exotic.
          return { ok: false, error: toError(error) };
        }
      },

      only(...ids: readonly Ids[]): Layer<C, Ids, Impl> {
        return view(intersectPins(ids, pinned));
      },

      supports(capability: string): capability is Impl {
        return registry.supports(capability);
      },

      on<K extends InterlayerEventName>(
        event: K,
        fn: (payload: InterlayerEvents[K]) => void,
      ): Unsubscribe {
        // The emitter hands back its own unsubscribe closure, which is what
        // kills the "you must keep the same function reference for off()" bug.
        return events.on(event, fn);
      },

      use(fn: CallMiddleware): typeof layer {
        middleware.push(fn);
        return layer;
      },

      async close(): Promise<void> {
        if (closed) return;
        closed = true;
        await defaultStack.dispose();
        for (const stack of overrides.values()) await stack.dispose();
        overrides.clear();
        await registry.dispose();
        events.removeAllListeners();
      },

      [Symbol.asyncDispose](): Promise<void> {
        return layer.close();
      },
    };
    return layer;
  }

  return view(undefined);
}

/* ------------------------------------------------------------------ *
 * 4. Small helpers
 * ------------------------------------------------------------------ */

/**
 * Narrows one pin list by another, keeping the FIRST list's order.
 *
 * `layer.only('a', 'b').only('b')` must mean `['b']`, and a `providers` hint on
 * the call must not widen a pin the layer already made — a restriction that can
 * be undone is not a restriction.
 */
function intersectPins(
  pins: readonly string[],
  existing: readonly string[] | undefined,
): readonly string[] {
  if (existing === undefined) return [...pins];
  return pins.filter((id) => existing.includes(id));
}

/** Last-resort normalisation for `tryCall`. */
function toError(error: unknown): AnyInterlayerError {
  return isInterlayerError(error) ? error : toInterlayerError(error);
}
