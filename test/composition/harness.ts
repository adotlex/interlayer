/**
 * Harness for the COMPOSITION suite (Wave 3 / T2). Owned by `test/composition/`.
 *
 * `test/support/**` is frozen and read-only, and it deliberately has no notion
 * of "the assembled stack" — it hands out a runtime, a context and a provider
 * double. What this suite needs on top of that is:
 *
 *  1. a clock driver that steps ONE due instant at a time (see {@link settle}),
 *  2. a hand-composed two-stack builder that assembles the REAL policy objects
 *     through the REAL `policyStack()` ({@link buildComposition}), so a test can
 *     read `describe()` off an individual policy — something `createLayer` does
 *     not expose,
 *  3. an `AbortSignal` whose listener bookkeeping is observable (ORD-12).
 *
 * Nothing here duplicates a policy's logic. Every ordering decision is made by
 * `policyStack()` from `src/core/policy.ts`; this file only supplies the array
 * (deliberately SHUFFLED, so a passing order test cannot be an artefact of the
 * declaration order) and the terminal.
 */

import { compose, pipeline } from '../../src/core/compose.ts';
import { isInterlayerError, TransportError } from '../../src/core/errors.ts';
import {
  type BreakerOptions,
  type CallPolicy,
  type Policy,
  type PolicyKind,
  policyStack,
  type TimeoutOptions,
} from '../../src/core/policy.ts';
import type {
  AnyInterlayerError,
  AttemptContext,
  CallContext,
  Capability,
  Composed,
  ErasedHandler,
  ProviderRecord,
} from '../../src/core/types.ts';
import { capability, defineContract } from '../../src/registry/index.ts';
import { circuitBreaker } from '../../src/resilience/circuit-breaker/index.ts';
import { type RateLimitPolicyOptions, rateLimit } from '../../src/resilience/rate-limit/index.ts';
import { type RetryPolicyOptions, retryPolicy } from '../../src/resilience/retry/index.ts';
import { attemptTimeout, totalTimeout } from '../../src/resilience/timeout/index.ts';
import type { FakeRuntime } from '../support/index.ts';

/* ------------------------------------------------------------------ *
 * 1. The clock driver
 * ------------------------------------------------------------------ */

/**
 * Empties the microtask queue completely.
 *
 * `await Promise.resolve()` advances it by exactly one tick and this pipeline is
 * five policies deep, so counting ticks is guesswork. A `setImmediate` turn is a
 * real macrotask: every microtask queued ahead of it has already run by the time
 * it fires. This is the only real timer in the suite and it is not under test.
 */
export function drainMicrotasks(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/**
 * Drives the virtual clock until `promise` settles, ONE DUE INSTANT AT A TIME.
 *
 * `runAll()` is unusable for ordering work: it computes a single target from the
 * timers present when it is called, so a 10 ms backoff and a 30 s total timeout
 * fire in the same drain and the timeout wins a race it should never have
 * entered. Stepping to `clock.nextTimerAt` fires timers in true due order, stops
 * the moment the promise settles, and leaves `runtime.now()` on an exact instant
 * so every assertion below can be an equality.
 */
export async function settle<T>(runtime: FakeRuntime, promise: Promise<T>): Promise<T> {
  let done = false;
  const tracked = promise.then(
    (value) => {
      done = true;
      return value;
    },
    (error: unknown) => {
      done = true;
      throw error;
    },
  );
  tracked.catch(() => undefined); // never an unhandled rejection while pumping

  for (let guard = 0; guard < 1_000 && !done; guard++) {
    await drainMicrotasks();
    if (done) break;
    const next = runtime.clock.nextTimerAt;
    if (next === undefined) break; // nothing left to move; it must be resolving
    await runtime.advance(Math.max(0, next - runtime.now()));
  }
  return tracked;
}

/** The rejection of a driven promise, as a typed error. Throws if it resolved. */
export async function rejectionOf(
  runtime: FakeRuntime,
  promise: Promise<unknown>,
): Promise<AnyInterlayerError> {
  try {
    await settle(runtime, promise);
  } catch (error) {
    if (isInterlayerError(error)) return error;
    throw error;
  }
  throw new Error('expected the call to reject, but it resolved');
}

/** Schedules `fn` on the VIRTUAL clock so `settle()` steps onto it. */
export function atVirtual(runtime: FakeRuntime, ms: number, fn: () => void): void {
  runtime.clock.setTimeout(fn, ms);
}

/* ------------------------------------------------------------------ *
 * 2. The contract and its scripted providers
 * ------------------------------------------------------------------ */

export interface Ping {
  readonly n: number;
}
export interface Pong {
  readonly n: number;
  readonly by: string;
}

/**
 * The explicit interface is required: `isolatedDeclarations` cannot emit the
 * inferred type of an exported `defineContract({...})`.
 */
export interface EchoContract {
  readonly echo: Capability<Ping, Pong>;
}

export const echoContract: EchoContract = defineContract<EchoContract>({
  echo: capability<Ping, Pong>({ idempotent: true }),
});

export interface ScriptedCall {
  /** `ctx.runtime.now()` on entry — the instant the PHYSICAL call was made. */
  readonly at: number;
  /** 1-based attempt number within this provider, as retry set it. */
  readonly attempt: number;
  readonly providerId: string;
}

export interface Scripted {
  readonly handler: (input: Ping, ctx: AttemptContext) => Promise<Pong>;
  readonly calls: readonly ScriptedCall[];
  readonly callCount: number;
  /** Instants of every physical call, for exact timeline assertions. */
  readonly at: readonly number[];
}

export interface ScriptOptions {
  readonly id: string;
  /** Leading invocations that throw. `Infinity` = never succeeds. Default 0. */
  readonly failures?: number | undefined;
  /** Virtual ms spent on `ctx.runtime.sleep` before settling. Default 0. */
  readonly delayMs?: number | undefined;
  /** Error thrown by a failing invocation. Default: a retryable `TransportError`. */
  readonly error?: (() => unknown) | undefined;
  /**
   * Never settles and never observes the signal — the pathological provider the
   * attempt timeout exists for. Overrides `delayMs`.
   */
  readonly hang?: boolean | undefined;
}

/** A provider handler with a scripted outcome sequence and a call log. */
export function scripted(options: ScriptOptions): Scripted {
  const calls: ScriptedCall[] = [];
  const failures = options.failures ?? 0;
  const delayMs = options.delayMs ?? 0;
  const makeError = options.error ?? ((): unknown => new TransportError(`${options.id} is down`));

  const handler = async (input: Ping, ctx: AttemptContext): Promise<Pong> => {
    calls.push({ at: ctx.runtime.now(), attempt: ctx.attempt, providerId: ctx.provider.id });
    if (options.hang === true) return await new Promise<Pong>(() => undefined);
    if (delayMs > 0) await ctx.runtime.sleep(delayMs, ctx.signal);
    if (calls.length <= failures) throw makeError();
    return { n: input.n, by: options.id };
  };

  return {
    handler,
    get calls(): readonly ScriptedCall[] {
      return calls;
    },
    get callCount(): number {
      return calls.length;
    },
    get at(): readonly number[] {
      return calls.map((c) => c.at);
    },
  };
}

/* ------------------------------------------------------------------ *
 * 3. An AbortSignal whose listener bookkeeping is observable (ORD-12)
 * ------------------------------------------------------------------ */

type AddArgs = Parameters<AbortSignal['addEventListener']>;
type RemoveArgs = Parameters<AbortSignal['removeEventListener']>;

export interface SignalProbe {
  readonly signal: AbortSignal;
  /** `addEventListener` calls minus `removeEventListener` calls. */
  readonly attached: number;
  readonly adds: number;
  abort(reason?: unknown): void;
}

/**
 * A caller signal that counts the listeners hung on it.
 *
 * MEANINGFUL ONLY WHILE THE SIGNAL HAS NOT ABORTED: every listener the library
 * attaches to a caller signal uses `{ once: true }`, and the platform detaches
 * those itself once they fire, without routing through `removeEventListener`.
 * That is also why the count is the right leak guard — the listener that leaks
 * is precisely the one that never fires and is never removed.
 */
export function probedSignal(): SignalProbe {
  const controller = new AbortController();
  const signal = controller.signal;
  const rawAdd = signal.addEventListener.bind(signal);
  const rawRemove = signal.removeEventListener.bind(signal);
  let adds = 0;
  let removes = 0;

  Object.defineProperty(signal, 'addEventListener', {
    configurable: true,
    value: (...args: AddArgs): void => {
      adds++;
      rawAdd(...args);
    },
  });
  Object.defineProperty(signal, 'removeEventListener', {
    configurable: true,
    value: (...args: RemoveArgs): void => {
      removes++;
      rawRemove(...args);
    },
  });

  return {
    signal,
    get attached(): number {
      return adds - removes;
    },
    get adds(): number {
      return adds;
    },
    abort(reason?: unknown): void {
      controller.abort(reason);
    },
  };
}

/* ------------------------------------------------------------------ *
 * 4. The hand-composed two-stack pipeline
 * ------------------------------------------------------------------ */

export interface CompositionOptions {
  readonly retry?: RetryPolicyOptions | false | undefined;
  readonly rateLimit?: RateLimitPolicyOptions | false | undefined;
  readonly breaker?: BreakerOptions | false | undefined;
  readonly timeout?: TimeoutOptions | false | undefined;
}

export interface Composition {
  /** `undefined` when that policy was switched off. */
  readonly retry: Policy<AttemptContext, unknown> | undefined;
  readonly limiter: Policy<AttemptContext, unknown> | undefined;
  readonly breaker: Policy<AttemptContext, unknown> | undefined;
  readonly attemptTimeout: Policy<AttemptContext, unknown> | undefined;
  readonly totalTimeout: Policy<CallContext, unknown> | undefined;
  /** The order the policies were DECLARED in — deliberately not canonical. */
  readonly declaredKinds: readonly PolicyKind[];
  /** The order `policyStack()` composed them in, per stack. */
  readonly callKinds: readonly PolicyKind[];
  readonly attemptKinds: readonly PolicyKind[];
  run(ctx: CallContext, provider: ProviderRecord, handler: ErasedHandler): Promise<unknown>;
  dispose(): Promise<void>;
}

/**
 * Assembles the real policies exactly the way `src/layer.ts` does — one call
 * stack, one attempt stack, both ordered by `policyStack()` — but keeps every
 * policy object reachable so a test can read its `describe()`.
 *
 * The declaration order below is SCRAMBLED on purpose. If `policyStack()` ever
 * stopped ordering, every behavioural test in this suite would fail rather than
 * silently pass on the array order it was handed.
 */
export function buildComposition(options: CompositionOptions = {}): Composition {
  const retry = options.retry === false ? undefined : retryPolicy(options.retry ?? {});
  const limiter = options.rateLimit === false ? undefined : rateLimit(options.rateLimit ?? {});
  const breaker = options.breaker === false ? undefined : circuitBreaker(options.breaker ?? {});
  const attempt = options.timeout === false ? undefined : attemptTimeout(options.timeout ?? {});
  const total = options.timeout === false ? undefined : totalTimeout(options.timeout ?? {});

  // Scrambled: innermost-first, with the call-scope policy in the middle.
  const declared: Policy<AttemptContext, unknown>[] = [];
  if (attempt !== undefined) declared.push(attempt);
  if (breaker !== undefined) declared.push(breaker);
  if (limiter !== undefined) declared.push(limiter);
  if (retry !== undefined) declared.push(retry);
  const callPolicies: CallPolicy[] = total === undefined ? [] : [total];

  const attemptStack: Composed<AttemptContext, unknown> = compose<AttemptContext, unknown>(
    policyStack(declared, 'attempt'),
  );

  return {
    retry,
    limiter,
    breaker,
    attemptTimeout: attempt,
    totalTimeout: total,
    declaredKinds: declared.map((p) => p.kind),
    callKinds: [...callPolicies].map((p) => p.kind),
    attemptKinds: orderedKinds(declared, 'attempt'),
    run(ctx: CallContext, provider: ProviderRecord, handler: ErasedHandler): Promise<unknown> {
      const runCall = pipeline<CallContext, unknown>(
        policyStack(callPolicies, 'call'),
        (callCtx: CallContext): Promise<unknown> =>
          // Exactly what `createRouter` builds per candidate: a fresh attempt
          // context with `attempt: 1`; retry bumps it downstream via `next(ctx')`.
          attemptStack({ ...callCtx, provider, attempt: 1 }, (final: AttemptContext) =>
            handler(final.input, final),
          ),
      );
      return runCall(ctx);
    },
    async dispose(): Promise<void> {
      for (const policy of [...declared, ...callPolicies]) {
        if (policy.dispose !== undefined) await policy.dispose();
      }
    },
  };
}

/** The kinds of one scope, in the order `policyStack()` would compose them. */
function orderedKinds(
  policies: readonly Policy<AttemptContext, unknown>[],
  scope: 'attempt',
): readonly PolicyKind[] {
  const executes = policyStack(policies, scope);
  return executes.map((execute) => {
    const owner = policies.find((p) => p.execute === execute);
    return owner === undefined ? 'custom' : owner.kind;
  });
}

/* ------------------------------------------------------------------ *
 * 5. `describe()` readers — narrowing, with a loud failure on drift
 * ------------------------------------------------------------------ */

function described(policy: Policy<AttemptContext, unknown>): Readonly<Record<string, unknown>> {
  if (policy.describe === undefined) throw new Error(`policy "${policy.name}" has no describe()`);
  return policy.describe();
}

function numberAt(record: Readonly<Record<string, unknown>>, key: string): number {
  const value = record[key];
  if (typeof value !== 'number') {
    throw new Error(`describe().${key} is ${typeof value}, expected number`);
  }
  return value;
}

/**
 * Reads one key of a `describe()` record.
 *
 * The key travels as a VARIABLE deliberately: `record['literal']` on an
 * index-signature type is what Biome's `useLiteralKeys` objects to, and
 * `record.literal` is what TypeScript's `noPropertyAccessFromIndexSignature`
 * objects to. A variable satisfies both.
 */
function valueAt(record: Readonly<Record<string, unknown>>, key: string): unknown {
  return record[key];
}

/** Tokens left in a limiter's bucket. Compare with a tolerance, never `===`. */
export function tokensOf(limiter: Policy<AttemptContext, unknown>): number {
  return numberAt(described(limiter), 'tokens');
}

/** How many callers are parked in a limiter's FIFO queue right now. */
export function queueDepthOf(limiter: Policy<AttemptContext, unknown>): number {
  return numberAt(described(limiter), 'queueDepth');
}

export interface BreakerView {
  readonly state: string;
  /** Buckets in the rolling window: one per distinct recorded-outcome instant. */
  readonly bucketCount: number;
  readonly consecutiveFailures: number;
  readonly openedAt: number;
}

/** The breaker's own view of one provider key, or `undefined` if it has none. */
export function breakerView(
  breaker: Policy<AttemptContext, unknown>,
  key: string,
): BreakerView | undefined {
  const keys = valueAt(described(breaker), 'keys');
  if (typeof keys !== 'object' || keys === null) return undefined;
  const entry = valueAt(keys as Record<string, unknown>, key);
  if (typeof entry !== 'object' || entry === null) return undefined;
  const record = entry as Record<string, unknown>;
  const state = valueAt(record, 'state');
  return {
    state: typeof state === 'string' ? state : '<unknown>',
    bucketCount: numberAt(record, 'bucketCount'),
    consecutiveFailures: numberAt(record, 'consecutiveFailures'),
    openedAt: numberAt(record, 'openedAt'),
  };
}
