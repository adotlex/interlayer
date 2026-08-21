/**
 * Regression tests for the two defects the system `Runtime` shipped with.
 *
 * Both were invisible to the rest of the suite because every other test runs on
 * the FAKE runtime, and the fake disagreed with the real one on exactly these
 * two points. That is the shape of bug this file exists to catch: a seam whose
 * double is more correct than the thing it doubles.
 */

import { describe, expect, it } from 'vitest';
import { createFakeRuntime } from '../../test/support/fake-clock.ts';
import { hasCode, type TimeoutError } from './errors.ts';
import { createSystemRuntime, systemRuntime } from './runtime.ts';
import type { Runtime } from './types.ts';

/** Timers currently keeping the event loop alive. Unref'd ones are excluded. */
function refdTimers(): number {
  return process.getActiveResourcesInfo().filter((r) => r === 'Timeout').length;
}

describe('systemRuntime.deadline — the timer must hold the event loop open', () => {
  it('is REF’d, so the process cannot exit before the deadline fires', () => {
    const before = refdTimers();
    const d = systemRuntime.deadline(60_000);
    try {
      // The bug: `handle.unref()` here meant a process whose only pending work
      // was a deadline exited BEFORE it fired — the same hazard R4 measured in
      // `AbortSignal.timeout()`, and the reason this library refuses to use it.
      // `getActiveResourcesInfo()` lists ref'd timers only, so this assertion
      // fails the moment anyone restores the `unref`.
      expect(refdTimers()).toBe(before + 1);
    } finally {
      d.dispose();
    }
  });

  it('dispose() releases it again, so a completed call holds nothing', () => {
    const before = refdTimers();
    systemRuntime.deadline(60_000).dispose();
    expect(refdTimers()).toBe(before);
  });

  it('actually fires, on the real clock', async () => {
    const d = systemRuntime.deadline(1);
    try {
      await new Promise<void>((resolve) => {
        d.signal.addEventListener('abort', () => {
          resolve();
        });
      });
      expect(d.signal.aborted).toBe(true);
    } finally {
      d.dispose();
    }
  });
});

describe('deadline abort reason — identity, not just a stop', () => {
  /** Both runtimes implement the same port; both must answer identically. */
  const runtimes: readonly (readonly [string, Runtime])[] = [
    ['system', createSystemRuntime()],
    ['fake', createFakeRuntime()],
  ];

  for (const [name, runtime] of runtimes) {
    it(`${name}: aborts with a call-scoped TimeoutError, never a CancelledError`, async () => {
      const d = runtime.deadline(1);
      await new Promise<void>((resolve) => {
        d.signal.addEventListener('abort', () => {
          resolve();
        });
        // The fake's timers only move when told to; the system's move on their own.
        if ('advance' in runtime && typeof runtime.advance === 'function') {
          void runtime.advance(1);
        }
      });
      d.dispose();

      // R4 §2.3: a deadline breach and a caller abort must stay
      // distinguishable. With `CancelledError` here a blown deadline reported
      // `CANCELLED` — non-retryable, never falls back, and tells an operator the
      // user hung up when in fact we ran out of time.
      expect(hasCode(d.signal.reason, 'TIMEOUT'), `${name} reason`).toBe(true);
      expect(hasCode(d.signal.reason, 'CANCELLED')).toBe(false);
      const reason = d.signal.reason as TimeoutError;
      expect(reason.scope).toBe('call');
      expect(reason.timeoutMs).toBe(1);
      expect(reason.retryable, 'a blown overall deadline is not worth retrying').toBe(false);
    });
  }

  it('a parent abort propagates the PARENT’s reason by identity', () => {
    const parent = new AbortController();
    const sentinel = { why: 'caller said stop' };
    const d = systemRuntime.deadline(60_000, parent.signal);
    parent.abort(sentinel);
    d.dispose();

    // Not relabelled: whatever the caller aborted with is what downstream sees.
    expect(d.signal.reason).toBe(sentinel);
  });

  it('an already-aborted parent aborts the derived signal synchronously', () => {
    const parent = AbortSignal.abort('gone');
    const d = systemRuntime.deadline(60_000, parent);
    d.dispose();
    expect(d.signal.aborted).toBe(true);
    expect(d.signal.reason).toBe('gone');
  });
});

describe('systemRuntime.sleep', () => {
  it('resolves, and rejects with CANCELLED when the signal aborts', async () => {
    await systemRuntime.sleep(1);

    const ac = new AbortController();
    const pending = systemRuntime.sleep(60_000, ac.signal);
    ac.abort();
    await expect(pending).rejects.toSatisfy((e: unknown) => hasCode(e, 'CANCELLED'));
  });

  it('rejects immediately when the signal is already aborted', async () => {
    await expect(systemRuntime.sleep(1, AbortSignal.abort())).rejects.toSatisfy((e: unknown) =>
      hasCode(e, 'CANCELLED'),
    );
  });
});
