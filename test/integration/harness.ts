/**
 * Drivers for the integration suite — T1's own, inside T1's own subtree.
 *
 * `test/support/**` is frozen and `src/layer.test.ts` belongs to another wave,
 * so the fake-clock driver those files use is reproduced here rather than
 * imported across an ownership boundary. It is the same algorithm, and the
 * reason it exists is worth restating because it is the single most important
 * fact about testing this library on virtual time:
 *
 * > `runtime.runAll()` drains every timer that exists WHEN IT IS CALLED. A
 * > provider sleeping 250 ms and the 30 s total timeout sitting behind it fire
 * > in the same drain, so the call "times out" after having already succeeded.
 * > Stepping to `clock.nextTimerAt` fires them in real due order and stops the
 * > moment the promise settles, which is also what leaves `runtime.now()` on an
 * > exact instant so every timing assertion can be an equality.
 *
 * Nothing here reads the wall clock, `Date.now()` or `Math.random()`. The one
 * real timer in this file is the `setImmediate` used to drain microtasks, and
 * it is not the clock under test.
 */

import type {
  AnyInterlayerError,
  InterlayerEmitter,
  InterlayerEventName,
} from '../../src/index.ts';
import { isInterlayerError } from '../../src/index.ts';
import type { FakeRuntime } from '../support/index.ts';

/**
 * Empties the microtask queue completely.
 *
 * `await Promise.resolve()` advances it by exactly one tick, and the pipeline
 * here — compose, four policies, the router, the emitter — is far too deep to
 * count ticks. A `setImmediate` turn is a real macrotask, so everything queued
 * ahead of it has already run by the time it fires.
 */
export function drainMicrotasks(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/** Drives the virtual clock until `promise` settles, ONE DUE INSTANT AT A TIME. */
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

/**
 * The rejection of a call, as a typed error.
 *
 * A call that RESOLVES throws a plain `Error`, which is not an
 * `InterlayerError`, so the `catch` below rethrows it and the test fails loudly
 * on the right line instead of quietly asserting against `undefined`.
 */
export async function rejectionOf(
  runtime: FakeRuntime,
  promise: Promise<unknown>,
): Promise<AnyInterlayerError> {
  try {
    const value = await settle(runtime, promise);
    throw new Error(`expected a rejection, but the call resolved with ${JSON.stringify(value)}`);
  } catch (error) {
    if (!isInterlayerError(error)) throw error;
    return error;
  }
}

/**
 * Records every event name the layer emits, in emission order.
 *
 * Dispatch is synchronous (`src/core/events.ts`), so the returned array is a
 * faithful transcript and not a sampling. It keeps growing as calls are made.
 */
export function recordEventNames(events: InterlayerEmitter): InterlayerEventName[] {
  const names: InterlayerEventName[] = [];
  events.onAny((name) => {
    names.push(name);
  });
  return names;
}

/** A handler that never settles. The only way to test a timeout honestly. */
export function neverSettles<T>(): Promise<T> {
  return new Promise<T>(() => undefined);
}

/** A counter a handler can bump, readable from the test. */
export interface Counter {
  readonly count: number;
  bump(): number;
}

export function counter(): Counter {
  let n = 0;
  return {
    get count(): number {
      return n;
    },
    bump(): number {
      n++;
      return n;
    },
  };
}
