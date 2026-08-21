/**
 * The concurrency driver — owned by `test/concurrency/`, used by nothing else.
 *
 * `test/support/**` is frozen and its `settle()`-style drivers all assume ONE
 * in-flight promise. Every test in this directory has N in flight at once, so
 * the crank has to be turned differently:
 *
 *  - {@link flush} empties the microtask queue and advances the virtual clock by
 *    NOTHING. That is the primary tool here. N calls parked on a gate all reach
 *    their gate through microtasks alone, so `flush()` is what establishes the
 *    "all N are in flight, none has settled" state a race test needs.
 *  - {@link step} advances the clock by an exact amount, flushing either side.
 *  - {@link drive} steps to `clock.nextTimerAt` one due instant at a time until a
 *    predicate holds. `runtime.runAll()` is UNUSABLE for these tests: it computes
 *    a single target from every timer present and drains a 1 s rate-limit wake-up
 *    and a 30 s total timeout in the same pass, collapsing the exact interleaving
 *    under test.
 *
 * Determinism note: `setImmediate` is a macrotask, not a sleep. Nothing here has
 * a duration, nothing polls, and no test in this directory reads `Date.now()` or
 * `Math.random()`. Every instant asserted below is a virtual instant.
 */

import { CancelledError } from '../../src/core/errors.ts';
import type { FakeRuntime } from '../support/index.ts';

/* ------------------------------------------------------------------ *
 * 1. Turning the crank
 * ------------------------------------------------------------------ */

/**
 * Empties the microtask queue completely, without moving the clock.
 *
 * A single `await Promise.resolve()` advances the queue by one tick, and the
 * pipeline here is deep — compose, four policies, the router, the emitter — so
 * counting ticks is guesswork. A `setImmediate` turn is a real macrotask, so
 * every microtask queued ahead of it has already run when it fires.
 */
export function flush(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/** Advances the virtual clock by exactly `ms`, draining microtasks either side. */
export async function step(runtime: FakeRuntime, ms: number): Promise<void> {
  await flush();
  await runtime.advance(ms);
  await flush();
}

/**
 * Steps to the next due timer, repeatedly, until `done()` holds or no timer is
 * left. Never advances past an instant nothing is scheduled at, so every
 * assertion downstream can be an equality rather than a range.
 */
export async function drive(
  runtime: FakeRuntime,
  done: () => boolean,
  maxSteps = 400,
): Promise<void> {
  for (let i = 0; i < maxSteps; i++) {
    await flush();
    if (done()) return;
    const next = runtime.clock.nextTimerAt;
    if (next === undefined) return;
    await runtime.advance(Math.max(0, next - runtime.now()));
  }
  await flush();
}

/* ------------------------------------------------------------------ *
 * 2. Recording what settled, when, and in what order
 * ------------------------------------------------------------------ */

/** One promise's settlement, stamped with the virtual instant it happened at. */
export interface Settlement {
  readonly id: string;
  readonly ok: boolean;
  /** `runtime.now()` at settlement. Exact, because time is virtual. */
  readonly at: number;
  readonly value: unknown;
  readonly error: unknown;
}

/**
 * Settlement order for N concurrent promises.
 *
 * `watch()` returns a promise that NEVER rejects, so a rejected call under test
 * can never become an unhandled rejection while the clock is being pumped.
 */
export interface Ledger {
  readonly settlements: readonly Settlement[];
  /** Ids in settlement order — the FIFO assertion for the rate limiter. */
  readonly order: readonly string[];
  /** Ids that resolved, in settlement order. */
  readonly resolved: readonly string[];
  /** Ids that rejected, in settlement order. */
  readonly rejected: readonly string[];
  readonly size: number;
  watch(id: string, promise: Promise<unknown>): Promise<void>;
  get(id: string): Settlement | undefined;
  has(id: string): boolean;
  /** Virtual instant `id` settled at, or `undefined` if it has not. */
  at(id: string): number | undefined;
}

export function createLedger(runtime: FakeRuntime): Ledger {
  const settlements: Settlement[] = [];
  const record = (id: string, ok: boolean, value: unknown, error: unknown): void => {
    settlements.push({ id, ok, at: runtime.now(), value, error });
  };
  const ledger: Ledger = {
    settlements,
    get order(): readonly string[] {
      return settlements.map((s) => s.id);
    },
    get resolved(): readonly string[] {
      return settlements.filter((s) => s.ok).map((s) => s.id);
    },
    get rejected(): readonly string[] {
      return settlements.filter((s) => !s.ok).map((s) => s.id);
    },
    get size(): number {
      return settlements.length;
    },
    watch(id: string, promise: Promise<unknown>): Promise<void> {
      return promise.then(
        (value: unknown) => {
          record(id, true, value, undefined);
        },
        (error: unknown) => {
          record(id, false, undefined, error);
        },
      );
    },
    get(id: string): Settlement | undefined {
      return settlements.find((s) => s.id === id);
    },
    has(id: string): boolean {
      return settlements.some((s) => s.id === id);
    },
    at(id: string): number | undefined {
      return settlements.find((s) => s.id === id)?.at;
    },
  };
  return ledger;
}

/* ------------------------------------------------------------------ *
 * 3. The gate — N calls held at the handler boundary at once
 * ------------------------------------------------------------------ */

/**
 * A handler-side barrier. Every parked call is identified, so a test can settle
 * them in ANY order — which is the whole point: "concurrent" here means
 * "several calls are simultaneously past admission and before settlement", and
 * the order they leave in is the variable under test.
 */
export interface Gate {
  /**
   * Park the calling handler. Records the arrival at the current instant.
   *
   * Pass `signal` to make the park abort-aware: an abort rejects it with a
   * `CancelledError`, which is what a well-behaved provider handler does and
   * what the breaker's `isUnscored` path needs to see.
   */
  park(id: string, signal?: AbortSignal): Promise<unknown>;
  /** Ids in the order they entered the handler. */
  readonly arrivals: readonly string[];
  /** Virtual instants of each arrival, aligned with {@link Gate.arrivals}. */
  readonly arrivalTimes: readonly number[];
  /** Ids currently parked and not yet released. */
  readonly parked: readonly string[];
  release(id: string, value?: unknown): void;
  fail(id: string, error: unknown): void;
  /** Virtual instant `id` entered the handler, or `undefined`. */
  arrivedAt(id: string): number | undefined;
}

export function createGate(runtime: FakeRuntime): Gate {
  const arrivals: string[] = [];
  const arrivalTimes: number[] = [];
  const waiting = new Map<
    string,
    { readonly resolve: (value: unknown) => void; readonly reject: (error: unknown) => void }
  >();

  return {
    arrivals,
    arrivalTimes,
    get parked(): readonly string[] {
      return [...waiting.keys()];
    },
    park(id: string, signal?: AbortSignal): Promise<unknown> {
      arrivals.push(id);
      arrivalTimes.push(runtime.now());
      return new Promise<unknown>((resolve, reject) => {
        if (signal === undefined) {
          waiting.set(id, { resolve, reject });
          return;
        }
        const onAbort = (): void => {
          waiting.delete(id);
          reject(new CancelledError('gate: aborted while parked', { cause: signal.reason }));
        };
        if (signal.aborted) {
          onAbort();
          return;
        }
        signal.addEventListener('abort', onAbort, { once: true });
        waiting.set(id, {
          resolve: (value: unknown): void => {
            signal.removeEventListener('abort', onAbort);
            resolve(value);
          },
          reject: (error: unknown): void => {
            signal.removeEventListener('abort', onAbort);
            reject(error);
          },
        });
      });
    },
    release(id: string, value: unknown = undefined): void {
      const w = waiting.get(id);
      if (w === undefined) throw new Error(`gate: nothing parked under "${id}"`);
      waiting.delete(id);
      w.resolve(value);
    },
    fail(id: string, error: unknown): void {
      const w = waiting.get(id);
      if (w === undefined) throw new Error(`gate: nothing parked under "${id}"`);
      waiting.delete(id);
      w.reject(error);
    },
    arrivedAt(id: string): number | undefined {
      const at = arrivals.indexOf(id);
      return at === -1 ? undefined : arrivalTimes[at];
    },
  };
}

/* ------------------------------------------------------------------ *
 * 4. Reading policy internals without `any`
 * ------------------------------------------------------------------ */

function asRecord(value: unknown): Readonly<Record<string, unknown>> | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  return value as Readonly<Record<string, unknown>>;
}

/**
 * Field readers taking the key as a VARIABLE.
 *
 * `describe()` is typed `Readonly<Record<string, unknown>>`, so its fields can
 * only be reached through an index — `noPropertyAccessFromIndexSignature` bans
 * the dot form. Reading through a parameter rather than a string literal keeps
 * that legal without tripping `useLiteralKeys` at every call site.
 */
function pick(record: Readonly<Record<string, unknown>> | undefined, key: string): unknown {
  return record?.[key];
}

function num(record: Readonly<Record<string, unknown>> | undefined, key: string): number {
  const value = pick(record, key);
  return typeof value === 'number' ? value : Number.NaN;
}

function str(record: Readonly<Record<string, unknown>> | undefined, key: string): string {
  const value = pick(record, key);
  return typeof value === 'string' ? value : '<missing>';
}

function bool(record: Readonly<Record<string, unknown>> | undefined, key: string): boolean {
  return pick(record, key) === true;
}

/** One key's slice of `circuitBreaker().describe()`. */
export interface BreakerView {
  readonly state: string;
  readonly openedAt: number;
  readonly halfOpenAt: number;
  readonly halfOpenInFlight: number;
  readonly halfOpenSuccesses: number;
  readonly consecutiveFailures: number;
  readonly generation: number;
}

/**
 * Projects `describe()` for one provider key.
 *
 * `describe()` is declared `Readonly<Record<string, unknown>>`, so every read is
 * a narrowing — no `any`, no non-null assertion. Returns `undefined` for a key
 * the breaker has never seen, which is itself an assertable fact.
 */
export function breakerView(
  describe: (() => Readonly<Record<string, unknown>>) | undefined,
  key: string,
): BreakerView | undefined {
  if (describe === undefined) return undefined;
  const keys = asRecord(pick(describe(), 'keys'));
  const entry = keys === undefined ? undefined : asRecord(keys[key]);
  if (entry === undefined) return undefined;
  return {
    state: str(entry, 'state'),
    openedAt: num(entry, 'openedAt'),
    halfOpenAt: num(entry, 'halfOpenAt'),
    halfOpenInFlight: num(entry, 'halfOpenInFlight'),
    halfOpenSuccesses: num(entry, 'halfOpenSuccesses'),
    consecutiveFailures: num(entry, 'consecutiveFailures'),
    generation: num(entry, 'generation'),
  };
}

/** The assertable half of `rateLimit().describe()`. */
export interface LimiterView {
  readonly tokens: number;
  readonly queueDepth: number;
  readonly timerArmed: boolean;
}

export function limiterView(
  describe: (() => Readonly<Record<string, unknown>>) | undefined,
): LimiterView {
  const d = describe === undefined ? undefined : describe();
  return {
    tokens: num(d, 'tokens'),
    queueDepth: num(d, 'queueDepth'),
    timerArmed: bool(d, 'timerArmed'),
  };
}
