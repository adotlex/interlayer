/**
 * The system `Runtime` — R3 §5.5.
 *
 * THIS IS THE ONLY FILE IN `src/` ALLOWED TO TOUCH `Date.now()`,
 * `Math.random()`, `setTimeout`, `clearTimeout` or `crypto.randomUUID()`.
 * Everything else takes a `Runtime` (or a `Clock`/`Timers`/`Random`) as an
 * argument. Tests use `createFakeRuntime()` from `test/support/`.
 *
 * | Seam        | Consumers                                                           |
 * |-------------|---------------------------------------------------------------------|
 * | `now()`     | every event's `at`; breaker timestamps; bucket refill; durations     |
 * | `sleep()`   | retry backoff; rate-limiter wait                                     |
 * | `random()`  | retry jitter; `weighted` selector                                    |
 * | `uuid()`    | `ctx.callId`                                                         |
 * | `deadline()`| `timeout` middleware; call-level deadline                            |
 *
 * ── WHY THERE IS NO `setTimeout` / `clearTimeout` ON `Runtime` ────────────
 *
 * It was considered and declined. The two seams above already cover both timer
 * shapes a resilience library needs: `sleep(ms, signal)` for "wait, unless
 * cancelled" (retry backoff, the rate limiter's head-of-queue wake-up) and
 * `deadline(ms, parent)` for "abort at an instant" (both timeout policies, the
 * facade's call deadline). Neither is a workaround; they are the intended uses.
 *
 * A raw timer pair would add a third way to do the same thing, oblige every
 * `Runtime` implementation to grow two more methods, and — the actual reason —
 * hand out a timer with NO cancellation attached. Every timer defect R4
 * catalogued is an uncleared timer: the 300 ms one that kept a process alive
 * after a 1 ms success, and the unref'd one that let a process exit before it
 * fired. `sleep` and `deadline` both own their teardown; `setTimeout` would
 * make forgetting it the default.
 *
 * A unit that genuinely wants raw timers can take the `Timers` port from
 * `./clock.ts` directly, which is what `createFakeClock()` implements.
 */

import { CancelledError, isInterlayerError, TimeoutError } from './errors.ts';
import type { DeadlineHandle, Runtime } from './types.ts';

/**
 * Why an awaited wait ended early, as a typed error.
 *
 * A signal whose reason is already an Interlayer error — the call deadline, an
 * outer total timeout — is surfaced AS ITSELF; only an untyped abort (the bare
 * `DOMException` from `abort()` with no argument, or a caller's own value)
 * becomes a `CancelledError` carrying that value as `cause`. The alternative
 * relabels every deadline breach that lands mid-sleep as `CANCELLED`, which is
 * non-retryable, never falls back, and says the caller hung up when they did
 * not (R4 §2.3). Mirrors `abortErrorFor()` in `src/routing/fallback.ts`.
 */
function interruptionOf(signal: AbortSignal | undefined, message: string): Error {
  const reason: unknown = signal?.reason;
  if (isInterlayerError(reason)) return reason;
  return new CancelledError(message, { cause: reason });
}

export function createSystemRuntime(): Runtime {
  return {
    now: (): number => Date.now(),
    random: (): number => Math.random(),
    uuid: (): string => crypto.randomUUID(),

    sleep(ms: number, signal?: AbortSignal): Promise<void> {
      return new Promise<void>((resolve, reject) => {
        if (signal?.aborted === true) {
          reject(interruptionOf(signal, 'Aborted before sleep'));
          return;
        }
        const cleanup = (): void => {
          signal?.removeEventListener('abort', onAbort);
        };
        const handle = setTimeout(() => {
          cleanup();
          resolve();
        }, ms);
        function onAbort(): void {
          clearTimeout(handle);
          cleanup();
          // Same rule as `deadline()`: a typed reason IS the answer. A deadline
          // firing mid-backoff must reject with its own `TimeoutError`, not be
          // relabelled `CANCELLED` on the way past.
          reject(interruptionOf(signal, 'Aborted during sleep'));
        }
        signal?.addEventListener('abort', onAbort, { once: true }); // no listener leak
      });
    },

    /**
     * TWO DELIBERATE DECISIONS, both of which this function got wrong before.
     *
     * 1. THE ABORT REASON IS A `TimeoutError`, not a `CancelledError`. R4 §2.3
     *    requires caller-abort and deadline-abort to stay distinguishable; with
     *    a `CancelledError` here a breached deadline surfaced as `CANCELLED`,
     *    which is non-retryable, never falls back, and tells an operator the
     *    user hung up when in fact we ran out of time. `scope: 'call'` because a
     *    deadline bounds a whole logical operation.
     *
     * 2. THE TIMER IS REF'D — it holds the event loop open. It used to call
     *    `handle.unref()`, which meant a process whose only pending work was a
     *    deadline could EXIT BEFORE THE DEADLINE FIRED. The fake runtime's
     *    timer was never unref'd, so every test passed while production had a
     *    timeout that might simply not happen — the same hazard R4 measured in
     *    `AbortSignal.timeout()` and the reason this library does not use it.
     *    The two runtimes now agree. `dispose()` (mandatory, and called from a
     *    `finally` by every consumer) is what releases the loop.
     */
    deadline(ms: number, parent?: AbortSignal): DeadlineHandle {
      const ctrl = new AbortController();
      const handle = setTimeout(
        () => ctrl.abort(new TimeoutError(ms, 'call', { details: { source: 'runtime.deadline' } })),
        ms,
      );
      const onParent = (): void => ctrl.abort(parent?.reason);
      parent?.addEventListener('abort', onParent, { once: true });
      if (parent?.aborted === true) ctrl.abort(parent.reason);
      return {
        signal: ctrl.signal,
        dispose(): void {
          clearTimeout(handle);
          parent?.removeEventListener('abort', onParent);
        },
      };
    },
  };
}

export const systemRuntime: Runtime = createSystemRuntime();
