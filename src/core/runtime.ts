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
 */

import { CancelledError } from './errors.ts';
import type { DeadlineHandle, Runtime } from './types.ts';

export function createSystemRuntime(): Runtime {
  return {
    now: (): number => Date.now(),
    random: (): number => Math.random(),
    uuid: (): string => crypto.randomUUID(),

    sleep(ms: number, signal?: AbortSignal): Promise<void> {
      return new Promise<void>((resolve, reject) => {
        if (signal?.aborted === true) {
          reject(new CancelledError('Aborted before sleep'));
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
          reject(new CancelledError('Aborted during sleep'));
        }
        signal?.addEventListener('abort', onAbort, { once: true }); // no listener leak
      });
    },

    deadline(ms: number, parent?: AbortSignal): DeadlineHandle {
      const ctrl = new AbortController();
      const handle = setTimeout(
        () => ctrl.abort(new CancelledError(`Deadline of ${ms}ms elapsed`)),
        ms,
      );
      handle.unref?.(); // never hold the process open
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
