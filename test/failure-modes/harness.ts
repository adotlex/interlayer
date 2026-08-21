/**
 * Local harness for `test/failure-modes/`. NOT a test file — vitest collects
 * only `*.test.ts`.
 *
 * Everything shared by this subtree lives here rather than in `test/support/`,
 * which is frozen. Nothing in this file reads the wall clock, `Math.random` or
 * sleeps: the virtual clock from `createFakeRuntime()` is the only source of
 * time, and `settle()` is the crank that turns it.
 */

import { expect } from 'vitest';
import { type AnyInterlayerError, isInterlayerError } from '../../src/core/errors.ts';
import type { ResilienceOptions } from '../../src/core/policy.ts';
import type { Capability, ErasedHandler, ProviderRecord } from '../../src/core/types.ts';
import { capability, defineContract } from '../../src/registry/index.ts';
import type { FakeRuntime } from '../support/index.ts';

/* ------------------------------------------------------------------ *
 * 1. The contract every file in this subtree shares
 * ------------------------------------------------------------------ */

export interface Ask {
  readonly q: string;
}
export interface Say {
  readonly a: string;
  readonly by: string;
}

/**
 * Declared with an explicit interface rather than inferred from
 * `defineContract`, because `isolatedDeclarations` (TS9010) refuses to emit a
 * declaration for an exported `const` whose type comes from a call. The
 * F-bounded constraint still holds, so `CapabilityName` stays `'run' | 'spare'`
 * and never widens to `string`.
 */
export interface Probe {
  readonly run: Capability<Ask, Say>;
  /** Declared by the contract, deliberately implemented by nobody by default. */
  readonly spare: Capability<Ask, Say>;
}

export const probe: Probe = defineContract<Probe>({
  run: capability<Ask, Say>({ idempotent: true }),
  spare: capability<Ask, Say>({ idempotent: true }),
});

/**
 * Every policy switched off.
 *
 * `false` is the only off switch (`undefined` means "use the default"), and a
 * bare layer is what makes an error-taxonomy assertion unambiguous: with the
 * four policies on, a single provider throw can surface as four different codes
 * depending on which wrapper reached it first.
 */
export const NO_POLICIES: ResilienceOptions = {
  retry: false,
  breaker: false,
  rateLimit: false,
  timeout: false,
};

/* ------------------------------------------------------------------ *
 * 2. Driving the virtual clock
 * ------------------------------------------------------------------ */

/**
 * Empties the microtask queue completely.
 *
 * A `setImmediate` turn is a real macrotask, so every microtask queued ahead of
 * it has already run by the time it fires. This is the one real timer in the
 * subtree and it is not the clock under test.
 */
export function drainMicrotasks(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/**
 * Drives the virtual clock until `promise` settles, ONE DUE INSTANT AT A TIME.
 *
 * Transcribed in spirit from `settle()` in `src/layer.test.ts`. `runAll()` is
 * the wrong crank: it collapses a 250 ms provider sleep and a 30 s total
 * timeout into a single drain, so the timeout wins a race it should never have
 * entered. Stepping to `clock.nextTimerAt` fires timers in real due order and
 * leaves `runtime.now()` on an exact instant.
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

  for (let guard = 0; guard < 500 && !done; guard++) {
    await drainMicrotasks();
    if (done) break;
    const next = runtime.clock.nextTimerAt;
    if (next === undefined) break; // nothing left to move; it must be resolving
    await runtime.advance(Math.max(0, next - runtime.now()));
  }
  return tracked;
}

/** The rejection of a call, as a typed error. Fails loudly if it resolved. */
export async function rejectionOf(
  runtime: FakeRuntime,
  promise: Promise<unknown>,
): Promise<AnyInterlayerError> {
  try {
    const value = await settle(runtime, promise);
    expect.unreachable(`expected a rejection, got ${JSON.stringify(value)}`);
  } catch (error) {
    if (!isInterlayerError(error)) {
      throw new Error(
        `expected an InterlayerError, got ${error instanceof Error ? error.stack : String(error)}`,
      );
    }
    return error;
  }
}

/**
 * Aborts `controller` at an exact VIRTUAL instant.
 *
 * The abort rides the same fake clock as everything else, so "abort 50 ms into
 * a 200 ms provider call" is an exact statement rather than a race.
 */
export function abortAt(
  runtime: FakeRuntime,
  delayMs: number,
  controller: AbortController,
  reason?: unknown,
): void {
  runtime.clock.setTimeout(() => {
    if (reason === undefined) controller.abort();
    else controller.abort(reason);
  }, delayMs);
}

/**
 * Asserts nothing is still holding the event loop open.
 *
 * Every timer in the library is created through `runtime.sleep` or
 * `runtime.deadline`, both of which the fake clock counts. A non-zero count
 * after a settled call is a leak, and a leak is what `dispose()`/`finally`
 * exist to prevent.
 */
export async function expectNoLeakedTimers(runtime: FakeRuntime): Promise<void> {
  await drainMicrotasks();
  expect(runtime.pendingTimers).toBe(0);
}

/* ------------------------------------------------------------------ *
 * 3. Reading `cause` chains
 * ------------------------------------------------------------------ */

interface ChainLink {
  readonly name: string;
  /** The Interlayer `code`, or `undefined` for a foreign error. */
  readonly code: string | undefined;
  readonly message: string;
}

/** Every link of the `cause` chain, outermost first. */
function causeChain(error: unknown, max = 12): readonly ChainLink[] {
  const out: ChainLink[] = [];
  let cur: unknown = error;
  for (let i = 0; i < max && cur instanceof Error; i++) {
    out.push({
      name: cur.name,
      code: isInterlayerError(cur) ? cur.code : undefined,
      message: cur.message,
    });
    cur = cur.cause;
  }
  return out;
}

/** The RAW `cause` values, for identity (`toBe`) assertions. Excludes `error`. */
export function causes(error: unknown, max = 12): readonly unknown[] {
  const out: unknown[] = [];
  let cur: unknown = error;
  while (out.length < max && cur instanceof Error && cur.cause !== undefined) {
    cur = cur.cause;
    out.push(cur);
  }
  return out;
}

/** `code` of every link, foreign errors reported as `name`. */
export function codeChain(error: unknown): readonly string[] {
  return causeChain(error).map((link) => link.code ?? link.name);
}

/* ------------------------------------------------------------------ *
 * 4. Hand-built doubles
 * ------------------------------------------------------------------ */

/** A handler that never settles. The only way out is a timeout or an abort. */
export function neverSettles(): () => Promise<Say> {
  return () => new Promise<Say>(() => undefined);
}

/**
 * A handler that throws SYNCHRONOUSLY rather than returning a rejected promise.
 *
 * Deliberately not `async`: an `async` function cannot throw synchronously, and
 * the whole point is to prove the two paths are indistinguishable downstream.
 */
export function throwsSynchronously(thrown: unknown): () => Promise<Say> {
  return (): Promise<Say> => {
    throw thrown;
  };
}

/** A handler that returns a rejected promise without ever throwing. */
export function rejects(thrown: unknown): () => Promise<Say> {
  return (): Promise<Say> => Promise.reject(thrown);
}

/**
 * Removes a capability from a LIVE registry record, simulating a provider
 * unregistered mid-flight.
 *
 * `ProviderRecord.capabilities` is a `ReadonlyMap` in the type system and a
 * real `Map` at runtime; the cast is the whole point of the test and is
 * confined to this one function.
 */
export function stripCapability(record: ProviderRecord, name: string): void {
  (record.capabilities as Map<string, ErasedHandler>).delete(name);
}
