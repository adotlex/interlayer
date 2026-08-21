/**
 * THE RATE-LIMIT WAIT QUEUE UNDER LOAD — FIFO, or it is not a queue.
 *
 * A bounded FIFO queue only has interesting behaviour when several callers are
 * in it at once. Sequentially every acquire either takes a token or waits alone,
 * and the three properties that actually matter never get exercised:
 *
 *   1. strict FIFO — position is decided at ARRIVAL, never at wake-up;
 *   2. no overtaking — a fresh arrival that finds accrued tokens must not spend
 *      them ahead of a waiter whose wake-up timer has not run yet;
 *   3. clean removal — an aborted or refused waiter must neither strand the
 *      queue behind it nor reorder what is left.
 *
 * Property 2 is the sharp one. `acquire()` drains the queue before it looks at
 * the bucket precisely so that the window between "tokens exist" and "the head's
 * timer fires" cannot be stolen. That window is reproduced here with
 * `runtime.setTime()`, which moves the clock WITHOUT firing timers — the only
 * deterministic way to stand inside it.
 *
 * Every caller is a real overlapping promise: N `execute()` calls are started
 * with no await between them, and admission is recorded inside `next()`, i.e. at
 * the exact instant the token was spent.
 */

import { describe, expect, it } from 'vitest';
import type { Policy } from '../../src/core/policy.ts';
import type { AttemptContext, InterlayerEvents } from '../../src/core/types.ts';
import { type RateLimitPolicyOptions, rateLimit } from '../../src/resilience/rate-limit/index.ts';
import {
  createFakeRuntime,
  type FakeRuntime,
  fakeProvider,
  type TestContextHandle,
  testContext,
} from '../support/index.ts';
import { createLedger, drive, flush, type Ledger, limiterView } from './harness.ts';

/** One admission: which caller spent a token, and at which virtual instant. */
interface Admission {
  readonly id: string;
  readonly at: number;
}

interface Fixture {
  readonly runtime: FakeRuntime;
  readonly h: TestContextHandle;
  readonly policy: Policy<AttemptContext, unknown>;
  readonly ledger: Ledger;
  /** Admissions in the order tokens were actually spent. */
  readonly admissions: readonly Admission[];
  readonly admittedIds: readonly string[];
  /** Starts one acquisition. NEVER awaited — overlap is the subject. */
  acquire(id: string, signal?: AbortSignal): void;
  readonly queueDepth: number;
  readonly throttled: readonly InterlayerEvents['ratelimit:throttled'][];
}

function fixture(options: RateLimitPolicyOptions): Fixture {
  const runtime = createFakeRuntime();
  const h = testContext({ runtime, provider: fakeProvider('p').alwaysSucceed('unused') });
  const policy = rateLimit({ key: 'p', ...options });
  const ledger = createLedger(runtime);
  const admissions: Admission[] = [];
  const throttled: InterlayerEvents['ratelimit:throttled'][] = [];
  h.events.on('ratelimit:throttled', (e) => throttled.push(e));

  return {
    runtime,
    h,
    policy,
    ledger,
    admissions,
    throttled,
    get admittedIds(): readonly string[] {
      return admissions.map((a) => a.id);
    },
    get queueDepth(): number {
      return limiterView(policy.describe?.bind(policy)).queueDepth;
    },
    acquire(id: string, signal?: AbortSignal): void {
      const ctx = signal === undefined ? h.attempt : h.withAttempt({ signal });
      void ledger.watch(
        id,
        policy.execute(ctx, () => {
          admissions.push({ id, at: runtime.now() });
          return Promise.resolve(id);
        }),
      );
    },
  };
}

function codeOf(ledger: Ledger, id: string): string {
  const error: unknown = ledger.get(id)?.error;
  if (typeof error === 'object' && error !== null && 'code' in error) {
    return String((error as { readonly code: unknown }).code);
  }
  return `<${id} did not reject>`;
}

function retryAfterOf(ledger: Ledger, id: string): unknown {
  const error: unknown = ledger.get(id)?.error;
  if (typeof error === 'object' && error !== null && 'retryAfterMs' in error) {
    return (error as { readonly retryAfterMs: unknown }).retryAfterMs;
  }
  return undefined;
}

/* ------------------------------------------------------------------ *
 * 1. FIFO
 * ------------------------------------------------------------------ */

describe('rate-limit queue — FIFO under a simultaneous burst', () => {
  it('admits six concurrent callers in arrival order, one per refill period', async () => {
    const f = fixture({ capacity: 2, refillPerSec: 1 });

    for (const id of ['c1', 'c2', 'c3', 'c4', 'c5', 'c6']) f.acquire(id);
    await flush();

    // The bucket starts FULL, so the first two are free; the rest queue.
    expect(f.admittedIds).toEqual(['c1', 'c2']);
    expect(f.queueDepth).toBe(4);

    await drive(f.runtime, () => f.ledger.size === 6);

    expect(f.admittedIds, 'admission order is arrival order').toEqual([
      'c1',
      'c2',
      'c3',
      'c4',
      'c5',
      'c6',
    ]);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 0, 1_000, 2_000, 3_000, 4_000]);
    expect(f.queueDepth).toBe(0);
    expect(f.runtime.pendingTimers, 'the wake-up timer is disarmed once the queue drains').toBe(0);
  });

  it('each queued caller is throttled with its own position, not the head position', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });

    for (const id of ['c1', 'c2', 'c3', 'c4']) f.acquire(id);
    await flush();

    // c1 took the only token and was never throttled. The rest are priced by
    // CUMULATIVE demand — everything already in line has to be paid for first.
    expect(f.throttled.map((e) => e.waitMs)).toEqual([1_000, 2_000, 3_000]);
    expect(f.throttled.every((e) => e.key === 'p')).toBe(true);
    expect(f.throttled.map((e) => e.at)).toEqual([0, 0, 0]);

    await drive(f.runtime, () => f.ledger.size === 4);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000, 3_000]);
  });
});

/* ------------------------------------------------------------------ *
 * 2. No overtaking
 * ------------------------------------------------------------------ */

describe('rate-limit queue — a fresh arrival cannot overtake a waiter', () => {
  it('does not let a late arrival spend tokens that accrued for the queued head', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });

    f.acquire('early'); // takes the only token at t=0
    f.acquire('queued'); // parks, wake-up armed for t=1000
    await flush();
    expect(f.admittedIds).toEqual(['early']);
    expect(f.queueDepth).toBe(1);

    // THE RACE. Stand exactly inside the window the source claims to close:
    // the clock reaches the instant a token exists, but the head's wake-up
    // timer has not run yet. `setTime` moves time WITHOUT firing timers, which
    // is the only way to be in that window deterministically.
    f.runtime.setTime(1_000);
    f.acquire('latecomer');
    await flush();

    expect(f.admittedIds, 'the queued caller is served first').toEqual(['early', 'queued']);
    expect(f.queueDepth).toBe(1);

    await drive(f.runtime, () => f.ledger.size === 3);
    expect(f.admittedIds).toEqual(['early', 'queued', 'latecomer']);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000]);
  });

  it('a caller arriving mid-wait joins the back of the line, not the front', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });

    f.acquire('a');
    f.acquire('b');
    f.acquire('c');
    await flush();
    expect(f.queueDepth).toBe(2);

    // Half a token has accrued when `d` shows up. It still queues behind `c`.
    await f.runtime.advance(500);
    f.acquire('d');
    await flush();
    expect(f.admittedIds).toEqual(['a']);
    expect(f.queueDepth).toBe(3);

    await drive(f.runtime, () => f.ledger.size === 4);
    expect(f.admittedIds).toEqual(['a', 'b', 'c', 'd']);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000, 3_000]);
  });
});

/* ------------------------------------------------------------------ *
 * 3. Removal — abort, refusal, disposal
 * ------------------------------------------------------------------ */

describe('rate-limit queue — removing one waiter never strands or reorders the rest', () => {
  it('aborting a middle waiter promotes the queue and releases its share of the wait', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });
    const gone = new AbortController();

    f.acquire('a');
    f.acquire('b');
    f.acquire('c', gone.signal);
    f.acquire('d');
    await flush();
    expect(f.queueDepth).toBe(3);

    await f.runtime.advance(500);
    gone.abort();
    await flush();

    expect(codeOf(f.ledger, 'c')).toBe('CANCELLED');
    expect(f.queueDepth, 'the aborted waiter is spliced out, not tombstoned').toBe(2);

    await drive(f.runtime, () => f.ledger.size === 4);

    expect(f.admittedIds).toEqual(['a', 'b', 'd']);
    // `d` moves up to `c`'s slot: 2_000, not the 3_000 it was originally priced
    // at. No token was spent by the abort, so the released demand is real.
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000]);
    expect(f.runtime.pendingTimers).toBe(0);
  });

  it('aborting the head promotes the next waiter and re-arms for it', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });
    const gone = new AbortController();

    f.acquire('a');
    f.acquire('head', gone.signal);
    f.acquire('behind');
    await flush();
    expect(f.queueDepth).toBe(2);

    gone.abort();
    await flush();
    expect(codeOf(f.ledger, 'head')).toBe('CANCELLED');
    expect(f.queueDepth).toBe(1);

    await drive(f.runtime, () => f.ledger.size === 3);
    expect(f.admittedIds).toEqual(['a', 'behind']);
    expect(
      f.admissions.map((a) => a.at),
      'the promoted waiter inherits the head wait',
    ).toEqual([0, 1_000]);
  });

  it('a burst past maxQueueDepth refuses the overflow without disturbing the line', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1, maxQueueDepth: 2 });

    for (const id of ['a', 'b', 'c', 'overflow']) f.acquire(id);
    await flush();

    expect(codeOf(f.ledger, 'overflow')).toBe('RATE_LIMITED');
    expect(retryAfterOf(f.ledger, 'overflow')).toBe(3_000);
    expect(f.queueDepth, 'the refused caller never entered the queue').toBe(2);

    await drive(f.runtime, () => f.ledger.size === 4);
    expect(f.admittedIds).toEqual(['a', 'b', 'c']);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000]);
  });

  it('a burst past maxQueueWaitMs refuses only the callers that would wait too long', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1, maxQueueWaitMs: 2_500 });

    for (const id of ['a', 'b', 'c', 'too-late']) f.acquire(id);
    await flush();

    // `b` would wait 1_000 and `c` 2_000; `too-late` would wait 3_000, which is
    // refused AT ENQUEUE rather than admitted and disappointed later.
    expect(codeOf(f.ledger, 'too-late')).toBe('RATE_LIMITED');
    expect(retryAfterOf(f.ledger, 'too-late')).toBe(3_000);
    expect(f.queueDepth).toBe(2);

    await drive(f.runtime, () => f.ledger.size === 4);
    expect(f.admittedIds).toEqual(['a', 'b', 'c']);
    expect(f.admissions.map((a) => a.at)).toEqual([0, 1_000, 2_000]);
  });

  it('disposing the limiter rejects every queued caller and leaves no timer behind', async () => {
    const f = fixture({ capacity: 1, refillPerSec: 1 });

    for (const id of ['a', 'b', 'c', 'd']) f.acquire(id);
    await flush();
    expect(f.queueDepth).toBe(3);
    expect(f.runtime.pendingTimers).toBe(1);

    await f.policy.dispose?.();
    await flush();

    expect(f.ledger.rejected, 'nobody is stranded in the queue').toEqual(['b', 'c', 'd']);
    for (const id of ['b', 'c', 'd']) expect(codeOf(f.ledger, id)).toBe('CANCELLED');
    expect(f.queueDepth).toBe(0);
    expect(f.runtime.pendingTimers, 'the wake-up timer is cancelled, not orphaned').toBe(0);
  });
});
