/**
 * Deterministic time — READ-ONLY to Wave 2.
 *
 * A TEST THAT SLEEPS IS A BROKEN TEST. Everything here runs on a virtual
 * clock: a one-hour `sleep` settles in microseconds and `now()` advances by
 * exactly 3_600_000.
 *
 * Two views on the same clock, so both styles of unit are served:
 *
 *   createFakeClock()    -> Clock + Timers (+ advance/runAll/pendingTimers)
 *   createFakeRuntime()  -> the full `TestRuntime`: now/sleep/random/uuid/deadline
 *
 * `createFakeRuntime` builds ON `createFakeClock`, so a test may hold both and
 * they stay in lockstep.
 *
 * IMPORTANT: `advance()` and `runAll()` are ASYNC and await a microtask flush
 * between timer callbacks. That is what lets an `await`-chained retry backoff
 * make progress. The synchronous equivalents cannot work — R2 reproduced three
 * distinct hang modes caused by exactly that mistake.
 */

import type { Clock, Random, Timers } from '../../src/core/clock.ts';
import { CancelledError, isInterlayerError, TimeoutError } from '../../src/core/errors.ts';
import type { DeadlineHandle, TestRuntime } from '../../src/core/types.ts';
import { seededRandom } from './seeded-random.ts';

interface Timer {
  readonly id: number;
  readonly dueAt: number;
  readonly seq: number;
  readonly fire: () => void;
  cancelled: boolean;
}

export interface FakeClockOptions {
  /** Epoch ms the clock starts at. Default 0. */
  readonly startTime?: number | undefined;
}

export interface FakeClock extends Clock, Timers {
  /** Fires every timer due at or before now+ms, in due order, ties by insertion. */
  advance(ms: number): Promise<void>;
  /** Runs all pending timers regardless of due time. */
  runAll(): Promise<void>;
  /** Live count of scheduled, uncancelled timers. Assert `0` to prove no leak. */
  readonly pendingTimers: number;
  /**
   * Due time of the EARLIEST pending timer, or `undefined` when none is
   * scheduled.
   *
   * This is what lets a driver step to the next due instant and no further.
   * `runAll()` cannot do that job: it computes one target from the timers
   * present when it is called, so a 250 ms provider sleep and a 30 s total
   * timeout both fire in the same drain and the timeout wins a race it should
   * never have entered. Stepping timer by timer is the only way to run an
   * integration pipeline on virtual time and still land on exact instants.
   */
  readonly nextTimerAt: number | undefined;
  setTime(epochMs: number): void;
}

export function createFakeClock(opts: FakeClockOptions = {}): FakeClock {
  let clock = opts.startTime ?? 0;
  let seq = 0;
  let ids = 0;
  const timers: Timer[] = [];

  function schedule(delayMs: number, fire: () => void): Timer {
    const t: Timer = {
      id: ++ids,
      dueAt: clock + Math.max(0, delayMs),
      seq: seq++,
      fire,
      cancelled: false,
    };
    timers.push(t);
    return t;
  }

  async function drainUntil(target: number): Promise<void> {
    for (;;) {
      const due = timers
        .filter((t) => !t.cancelled && t.dueAt <= target)
        .sort((a, b) => a.dueAt - b.dueAt || a.seq - b.seq)[0]; // ties broken by insertion
      if (due === undefined) break;
      timers.splice(timers.indexOf(due), 1);
      clock = Math.max(clock, due.dueAt);
      due.fire();
      await Promise.resolve(); // let microtasks queued by the callback settle
    }
    clock = Math.max(clock, target);
  }

  function cancel(handle: unknown): void {
    const t = timers.find((x) => x.id === handle);
    if (t !== undefined) {
      t.cancelled = true;
      timers.splice(timers.indexOf(t), 1);
    }
  }

  return {
    now: (): number => clock,
    setTimeout: (fn: () => void, ms: number): unknown => schedule(ms, fn).id,
    clearTimeout: cancel,
    advance: (ms: number): Promise<void> => drainUntil(clock + ms),
    runAll: async (): Promise<void> => {
      await drainUntil(timers.reduce((m, t) => Math.max(m, t.dueAt), clock));
    },
    get pendingTimers(): number {
      return timers.filter((t) => !t.cancelled).length;
    },
    get nextTimerAt(): number | undefined {
      let earliest: number | undefined;
      for (const t of timers) {
        if (t.cancelled) continue;
        if (earliest === undefined || t.dueAt < earliest) earliest = t.dueAt;
      }
      return earliest;
    },
    setTime(epochMs: number): void {
      clock = epochMs;
    },
  };
}

export interface FakeRuntimeOptions extends FakeClockOptions {
  /** Seed for the built-in mulberry32. Ignored when `random` is supplied. */
  readonly seed?: number | undefined;
  /** Override the PRNG entirely — e.g. `scriptedRandom([0.5, 0.25])`. */
  readonly random?: Random | undefined;
  /** Share an existing fake clock instead of creating one. */
  readonly clock?: FakeClock | undefined;
}

export interface FakeRuntime extends TestRuntime {
  /** The underlying clock, for units that take `Clock`/`Timers` rather than `Runtime`. */
  readonly clock: FakeClock;
  /** Every `sleep()` duration requested, in order. Assert on it directly. */
  readonly slept: readonly number[];
}

/**
 * Mirrors `interruptionOf` in `src/core/runtime.ts` exactly: an Interlayer abort
 * reason (a deadline's `TimeoutError`) is surfaced as itself; anything else
 * becomes a `CancelledError` carrying it as `cause`.
 */
function interruptionOf(signal: AbortSignal | undefined, message: string): Error {
  const reason: unknown = signal?.reason;
  if (isInterlayerError(reason)) return reason;
  return new CancelledError(message, { cause: reason });
}

/** The full determinism seam: virtual clock, seeded PRNG, counter uuid. */
export function createFakeRuntime(opts: FakeRuntimeOptions = {}): FakeRuntime {
  const clock =
    opts.clock ??
    createFakeClock(opts.startTime === undefined ? {} : { startTime: opts.startTime });
  const rand = opts.random ?? seededRandom(opts.seed ?? 1);
  const slept: number[] = [];
  let ids = 0;

  return {
    clock,
    slept,
    now: (): number => clock.now(),
    random: rand,
    uuid: (): string => `test-${(++ids).toString().padStart(8, '0')}`,

    sleep(ms: number, signal?: AbortSignal): Promise<void> {
      slept.push(ms);
      return new Promise<void>((resolve, reject) => {
        if (signal?.aborted === true) {
          reject(interruptionOf(signal, 'Aborted before sleep'));
          return;
        }
        const cleanup = (): void => {
          signal?.removeEventListener('abort', onAbort);
        };
        const handle = clock.setTimeout(() => {
          cleanup();
          resolve();
        }, ms);
        function onAbort(): void {
          clock.clearTimeout(handle);
          cleanup();
          reject(interruptionOf(signal, 'Aborted during sleep'));
        }
        signal?.addEventListener('abort', onAbort, { once: true });
      });
    },

    /**
     * Mirrors `createSystemRuntime().deadline()` EXACTLY, including the abort
     * reason: a `TimeoutError` with `scope: 'call'`, never a `CancelledError`
     * (R4 §2.3). A fake whose abort reason differs from the real one is a fake
     * that certifies the wrong behaviour.
     */
    deadline(ms: number, parent?: AbortSignal): DeadlineHandle {
      const ctrl = new AbortController();
      const handle = clock.setTimeout(
        () => ctrl.abort(new TimeoutError(ms, 'call', { details: { source: 'runtime.deadline' } })),
        ms,
      );
      const onParent = (): void => ctrl.abort(parent?.reason);
      parent?.addEventListener('abort', onParent, { once: true });
      if (parent?.aborted === true) ctrl.abort(parent.reason);
      return {
        signal: ctrl.signal,
        dispose(): void {
          clock.clearTimeout(handle);
          parent?.removeEventListener('abort', onParent);
        },
      };
    },

    advance: (ms: number): Promise<void> => clock.advance(ms),
    runAll: (): Promise<void> => clock.runAll(),
    get pendingTimers(): number {
      return clock.pendingTimers;
    },
    setTime(epochMs: number): void {
      clock.setTime(epochMs);
    },
  };
}
