import { describe, expect, it } from 'vitest';
import { createFakeRuntime, type FakeRuntime, testContext } from '../../../test/support/index.ts';
import { hasCode, type RateLimitedError } from '../../core/errors.ts';
import {
  DEFAULTS,
  type Policy,
  type PolicyFactory,
  type RateLimitOptions,
} from '../../core/policy.ts';
import type { AttemptContext, Next } from '../../core/types.ts';
import { rateLimit } from './rate-limit.ts';

/* ------------------------------------------------------------------ *
 * Harness. Everything runs on the virtual clock: no test here sleeps.
 * ------------------------------------------------------------------ */

/**
 * R4 §4.2 / RL-6 — token counts drift by ~1.7e-13, so they are compared with a
 * tolerance and NEVER with `===`. `waitMs`/`retryAfterMs` are whole
 * milliseconds out of `Math.ceil`, so those ARE compared exactly.
 */
const TOLERANCE = 1e-9;

type LimiterPolicy = Policy<AttemptContext, unknown>;

/** Drains the microtask queue so `await`-chained admissions settle. */
async function flush(): Promise<void> {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

function introspect(policy: LimiterPolicy): Readonly<Record<string, unknown>> {
  const described = policy.describe?.();
  if (described === undefined) throw new Error('describe() is the RL-23 test hook; it must exist');
  return described;
}

/**
 * Reads one `describe()` field. A VARIABLE key, not a literal one: `describe()`
 * returns `Readonly<Record<string, unknown>>`, so `d.tokens` is a tsc error
 * (`noPropertyAccessFromIndexSignature`) while `d['tokens']` is a biome
 * `useLiteralKeys` hint. Going through a variable satisfies both.
 */
function field(record: Readonly<Record<string, unknown>>, key: string): unknown {
  return record[key];
}

function queueDepth(policy: LimiterPolicy): unknown {
  return field(introspect(policy), 'queueDepth');
}

function tokensOf(policy: LimiterPolicy): number {
  const tokens = field(introspect(policy), 'tokens');
  if (typeof tokens !== 'number') throw new Error('describe().tokens must be a number');
  return tokens;
}

function expectTokens(policy: LimiterPolicy, expected: number): void {
  expect(Math.abs(tokensOf(policy) - expected)).toBeLessThan(TOLERANCE);
}

/** A terminal that records `tag@<virtual time>` when the call is admitted. */
function tag(log: string[], rt: FakeRuntime, name: string): Next<AttemptContext, unknown> {
  return (): Promise<unknown> => {
    log.push(`${name}@${rt.now()}`);
    return Promise.resolve(name);
  };
}

function expectRateLimited(error: unknown): RateLimitedError {
  if (!hasCode(error, 'RATE_LIMITED')) {
    throw new Error(`expected a RATE_LIMITED error, got ${String(error)}`);
  }
  return error;
}

/** Settles a promise into its rejection reason, so the reason can be asserted on. */
function reasonOf(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    (value) => {
      throw new Error(`expected a rejection, resolved with ${String(value)}`);
    },
    (error: unknown) => error,
  );
}

/* ------------------------------------------------------------------ *
 * Policy shape
 * ------------------------------------------------------------------ */

describe('rate limit policy — contract', () => {
  it('is an attempt-scoped rate-limit policy and satisfies the core factory type', () => {
    const factory: PolicyFactory<RateLimitOptions> = rateLimit;
    const policy = factory({ key: 'openai' } as RateLimitOptions);
    expect(policy.kind).toBe('rate-limit');
    expect(policy.scope).toBe('attempt');
    expect(policy.name).toBe('rate-limit(openai)');
  });

  it('applies the R4 §0.1 defaults', () => {
    const described = introspect(rateLimit());
    expect(field(described, 'capacity')).toBe(DEFAULTS.rateLimit.capacity);
    expect(field(described, 'refillPerSec')).toBe(DEFAULTS.rateLimit.refillPerSec);
    expect(field(described, 'onExhaustion')).toBe(DEFAULTS.rateLimit.onExhaustion);
    expect(field(described, 'maxQueueDepth')).toBe(DEFAULTS.rateLimit.maxQueueDepth);
    expect(field(described, 'maxQueueWaitMs')).toBe(DEFAULTS.rateLimit.maxQueueWaitMs);
    expect(field(described, 'cost')).toBe(1);
    expect(field(described, 'key')).toBe('default');
    expect(field(described, 'tokens')).toBe(10); // starts FULL, before any clock exists
  });

  it('rejects an impossible configuration with RangeError, at construction', () => {
    expect(() => rateLimit({ capacity: -1 })).toThrow(RangeError);
    expect(() => rateLimit({ refillPerSec: Number.NaN })).toThrow(RangeError);
    expect(() => rateLimit({ maxQueueDepth: 1.5 })).toThrow(RangeError);
    expect(() => rateLimit({ maxQueueWaitMs: -1 })).toThrow(RangeError);
    expect(() => rateLimit({ cost: 0 })).toThrow(RangeError);
    expect(() => rateLimit({ cost: Number.POSITIVE_INFINITY })).toThrow(RangeError);
  });
});

/* ------------------------------------------------------------------ *
 * Burst, queueing, FIFO
 * ------------------------------------------------------------------ */

describe('rate limit policy — burst and queue-and-wait', () => {
  it('grants a full burst at t=0 and queues the next caller (RL-1, RL-2)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 3, refillPerSec: 10 });
    const h = testContext({ runtime: rt });
    const log: string[] = [];

    await policy.execute(h.attempt, tag(log, rt, 'a'));
    await policy.execute(h.attempt, tag(log, rt, 'b'));
    await policy.execute(h.attempt, tag(log, rt, 'c'));
    expect(log).toEqual(['a@0', 'b@0', 'c@0']);
    expectTokens(policy, 0);

    const queued = testContext({ runtime: rt });
    const pending = policy.execute(queued.attempt, tag(log, rt, 'd'));
    await flush();
    expect(log).toHaveLength(3);
    expect(queueDepth(policy)).toBe(1);

    await rt.advance(100);
    await flush();
    expect(await pending).toBe('d');
    expect(log).toEqual(['a@0', 'b@0', 'c@0', 'd@100']);
    expect(rt.pendingTimers).toBe(0);
  });

  it('releases a waiter at exactly msUntil — not earlier, not later (RL-15)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const first = testContext({ runtime: rt });
    const second = testContext({ runtime: rt });
    const log: string[] = [];

    await policy.execute(first.attempt, tag(log, rt, 'first'));
    const pending = policy.execute(second.attempt, tag(log, rt, 'second'));

    await rt.advance(99);
    await flush();
    expect(log).toEqual(['first@0']); // ceil: never woken a fraction early

    await rt.advance(1);
    await flush();
    expect(log).toEqual(['first@0', 'second@100']);
    await pending;
    expect(rt.pendingTimers).toBe(0);
  });

  it('releases waiters in strict FIFO order (RL-14)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const a = testContext({ runtime: rt });
    const b = testContext({ runtime: rt });
    const c = testContext({ runtime: rt });
    const pa = policy.execute(a.attempt, tag(log, rt, 'a'));
    const pb = policy.execute(b.attempt, tag(log, rt, 'b'));
    const pc = policy.execute(c.attempt, tag(log, rt, 'c'));
    expect(queueDepth(policy)).toBe(3);

    await rt.advance(100);
    await flush();
    expect(log).toEqual(['head@0', 'a@100']);
    await rt.advance(100);
    await flush();
    expect(log).toEqual(['head@0', 'a@100', 'b@200']);
    await rt.advance(100);
    await flush();
    expect(log).toEqual(['head@0', 'a@100', 'b@200', 'c@300']);

    await Promise.all([pa, pb, pc]);
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0);
  });

  it('does not let a fresh arrival overtake a queued waiter', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const early = testContext({ runtime: rt });
    const pEarly = policy.execute(early.attempt, tag(log, rt, 'early'));

    // A full token has accrued by now, but `early` is still in line for it.
    await rt.advance(100);
    const late = testContext({ runtime: rt });
    const pLate = policy.execute(late.attempt, tag(log, rt, 'late'));
    await flush();
    expect(log).toEqual(['head@0', 'early@100']);

    await rt.advance(100);
    await flush();
    expect(log).toEqual(['head@0', 'early@100', 'late@200']);
    await Promise.all([pEarly, pLate]);
    expect(rt.pendingTimers).toBe(0);
  });

  it('emits ratelimit:throttled with the key and the computed wait', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10, key: 'openai' });
    const first = testContext({ runtime: rt });
    const second = testContext({ runtime: rt });
    const log: string[] = [];

    await policy.execute(first.attempt, tag(log, rt, 'first'));
    expect(first.emitted('ratelimit:throttled')).toHaveLength(0);

    const pending = policy.execute(second.attempt, tag(log, rt, 'second'));
    expect(second.emitted('ratelimit:throttled')).toEqual([{ key: 'openai', waitMs: 100, at: 0 }]);

    await rt.advance(100);
    await flush();
    await pending;
    expect(rt.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Bounds
 * ------------------------------------------------------------------ */

describe('rate limit policy — bounded queue', () => {
  it('rejects immediately once maxQueueDepth is reached, with retryAfterMs (RL-16)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10, maxQueueDepth: 1 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const queued = testContext({ runtime: rt });
    const pending = policy.execute(queued.attempt, tag(log, rt, 'queued'));

    const overflow = testContext({ runtime: rt });
    const error = expectRateLimited(
      await reasonOf(policy.execute(overflow.attempt, tag(log, rt, 'overflow'))),
    );
    // Second in line behind one waiter: two tokens' worth of wait.
    expect(error.retryAfterMs).toBe(200);
    expect(field(error.details, 'key')).toBe('default');
    expect(queueDepth(policy)).toBe(1);

    await rt.advance(100);
    await flush();
    await pending;
    expect(log).toEqual(['head@0', 'queued@100']);
    expect(rt.pendingTimers).toBe(0);
  });

  it('rejects at ENQUEUE when the projected wait exceeds maxQueueWaitMs (RL-17)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10, maxQueueWaitMs: 50 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const late = testContext({ runtime: rt });
    const error = expectRateLimited(
      await reasonOf(policy.execute(late.attempt, tag(log, rt, 'late'))),
    );
    expect(error.retryAfterMs).toBe(100);
    expect(error.retryable).toBe(true);
    // Rejected up front: never queued, never scheduled.
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0);
    expect(log).toEqual(['head@0']);
  });

  it('rejects a cost above capacity with RangeError, without queueing it (RL-12)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 2, refillPerSec: 10, cost: 3 });
    const h = testContext({ runtime: rt });
    const log: string[] = [];
    await expect(policy.execute(h.attempt, tag(log, rt, 'never'))).rejects.toThrow(RangeError);
    expect(log).toEqual([]);
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Abort while queued
 * ------------------------------------------------------------------ */

describe('rate limit policy — abort while queued', () => {
  it('rejects with CancelledError, frees the slot, spends no token, clears the timer (RL-18, RL-20)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const waiter = testContext({ runtime: rt });
    const settled = reasonOf(policy.execute(waiter.attempt, tag(log, rt, 'waiter')));
    await flush();
    expect(rt.pendingTimers).toBe(1);

    waiter.abort(new Error('caller went away'));
    expect(hasCode(await settled, 'CANCELLED')).toBe(true);
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0); // the only waiter left no orphan timer
    expectTokens(policy, 0); // no token was consumed on the way out

    // The token that accrues is still there for the next caller.
    await rt.advance(100);
    const next = testContext({ runtime: rt });
    await policy.execute(next.attempt, tag(log, rt, 'next'));
    expect(log).toEqual(['head@0', 'next@100']);
    expect(rt.pendingTimers).toBe(0);
  });

  it('promotes the next waiter and re-arms the timer when the head aborts (RL-19)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const first = testContext({ runtime: rt });
    const second = testContext({ runtime: rt });
    const firstSettled = reasonOf(policy.execute(first.attempt, tag(log, rt, 'first')));
    const secondPending = policy.execute(second.attempt, tag(log, rt, 'second'));
    expect(queueDepth(policy)).toBe(2);

    first.abort();
    expect(hasCode(await firstSettled, 'CANCELLED')).toBe(true);
    expect(queueDepth(policy)).toBe(1);
    expect(rt.pendingTimers).toBe(1); // re-armed for the promoted waiter

    // `second` would have waited 200ms behind `first`; promotion gets it the
    // first token instead.
    await rt.advance(100);
    await flush();
    expect(log).toEqual(['head@0', 'second@100']);
    await secondPending;
    expect(rt.pendingTimers).toBe(0);
  });

  it('refuses a pre-aborted caller without spending a token', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 5, refillPerSec: 10 });
    const h = testContext({ runtime: rt });
    const log: string[] = [];
    h.abort();
    expect(hasCode(await reasonOf(policy.execute(h.attempt, tag(log, rt, 'x'))), 'CANCELLED')).toBe(
      true,
    );
    expect(log).toEqual([]);
    expectTokens(policy, 5);
    expect(rt.pendingTimers).toBe(0);
  });

  it('dispose() drains the queue and leaves no timers', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10 });
    const log: string[] = [];
    const head = testContext({ runtime: rt });
    await policy.execute(head.attempt, tag(log, rt, 'head'));

    const a = testContext({ runtime: rt });
    const b = testContext({ runtime: rt });
    const sa = reasonOf(policy.execute(a.attempt, tag(log, rt, 'a')));
    const sb = reasonOf(policy.execute(b.attempt, tag(log, rt, 'b')));
    await flush();

    await policy.dispose?.();
    expect(hasCode(await sa, 'CANCELLED')).toBe(true);
    expect(hasCode(await sb, 'CANCELLED')).toBe(true);
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0);
    expect(log).toEqual(['head@0']);
  });
});

/* ------------------------------------------------------------------ *
 * Modes and degenerate configuration
 * ------------------------------------------------------------------ */

describe('rate limit policy — exhaustion modes', () => {
  it("onExhaustion 'reject' fails immediately with retryAfterMs === msUntil (RL-21, RL-22)", async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10, onExhaustion: 'reject' });
    const log: string[] = [];
    const first = testContext({ runtime: rt });
    const second = testContext({ runtime: rt });
    await policy.execute(first.attempt, tag(log, rt, 'first'));

    const error = expectRateLimited(
      await reasonOf(policy.execute(second.attempt, tag(log, rt, 'second'))),
    );
    expect(error.retryAfterMs).toBe(100);
    expect(queueDepth(policy)).toBe(0);
    expect(rt.pendingTimers).toBe(0);
    expect(log).toEqual(['first@0']);

    // Still a limiter, not a wall: the next token grants normally.
    await rt.advance(100);
    const third = testContext({ runtime: rt });
    await policy.execute(third.attempt, tag(log, rt, 'third'));
    expect(log).toEqual(['first@0', 'third@100']);
  });

  it('capacity 0 refuses everything with RangeError, in wait mode (RL-8)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 0, refillPerSec: 10 });
    const h = testContext({ runtime: rt });
    const log: string[] = [];
    await expect(policy.execute(h.attempt, tag(log, rt, 'x'))).rejects.toThrow(RangeError);
    await rt.advance(10_000);
    await expect(policy.execute(h.attempt, tag(log, rt, 'y'))).rejects.toThrow(RangeError);
    expect(log).toEqual([]);
    expect(rt.pendingTimers).toBe(0);
  });

  it('refillPerSec 0 rejects rather than scheduling an infinite timer (RL-9)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 0 });
    const log: string[] = [];
    const first = testContext({ runtime: rt });
    const second = testContext({ runtime: rt });

    await policy.execute(first.attempt, tag(log, rt, 'first')); // the initial burst still works
    const error = expectRateLimited(
      await reasonOf(policy.execute(second.attempt, tag(log, rt, 'second'))),
    );
    // An `Infinity` hint would tell the caller to sleep forever, so there is none.
    expect(error.retryAfterMs).toBeUndefined();
    expect(rt.pendingTimers).toBe(0);
    expect(queueDepth(policy)).toBe(0);

    await rt.advance(1_000_000);
    const third = testContext({ runtime: rt });
    expectRateLimited(await reasonOf(policy.execute(third.attempt, tag(log, rt, 'third'))));
    expect(log).toEqual(['first@0']);
  });

  it('honours a fractional cost (RL-13)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 1, refillPerSec: 10, cost: 0.5 });
    const log: string[] = [];
    const a = testContext({ runtime: rt });
    await policy.execute(a.attempt, tag(log, rt, 'a'));
    await policy.execute(a.attempt, tag(log, rt, 'b'));
    expectTokens(policy, 0);

    const c = testContext({ runtime: rt });
    const pending = policy.execute(c.attempt, tag(log, rt, 'c'));
    await rt.advance(49);
    await flush();
    expect(log).toEqual(['a@0', 'b@0']);
    await rt.advance(1);
    await flush();
    expect(log).toEqual(['a@0', 'b@0', 'c@50']);
    await pending;
    expect(rt.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Accounting
 * ------------------------------------------------------------------ */

describe('rate limit policy — token accounting', () => {
  it('is exact across a mixed sequence of grants, refusals, aborts and refills (RL-23)', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 10, refillPerSec: 10 });
    const log: string[] = [];
    const h = testContext({ runtime: rt });

    for (const name of ['a', 'b', 'c', 'd']) {
      await policy.execute(h.attempt, tag(log, rt, name));
    }
    expectTokens(policy, 6);

    await rt.advance(300); // +3 tokens, credited on the next operation
    await policy.execute(h.attempt, tag(log, rt, 'e'));
    expectTokens(policy, 8);

    for (const name of ['f', 'g', 'h', 'i', 'j', 'k', 'l', 'm']) {
      await policy.execute(h.attempt, tag(log, rt, name));
    }
    expectTokens(policy, 0);

    const doomed = testContext({ runtime: rt });
    const settled = reasonOf(policy.execute(doomed.attempt, tag(log, rt, 'doomed')));
    doomed.abort();
    expect(hasCode(await settled, 'CANCELLED')).toBe(true);
    expectTokens(policy, 0); // the abort refunded nothing and spent nothing

    await rt.advance(100);
    await policy.execute(h.attempt, tag(log, rt, 'n'));
    expectTokens(policy, 0);
    expect(log).toHaveLength(14);
    expect(rt.pendingTimers).toBe(0);
  });

  it('spends one token per invocation — a retry loop above pays for every attempt', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 10, refillPerSec: 10 });
    const h = testContext({ runtime: rt });
    const log: string[] = [];
    // Retry sits OUTSIDE the limiter, so it re-enters `execute` per attempt.
    await policy.execute(h.attempt, tag(log, rt, '1'));
    await policy.execute(h.attempt, tag(log, rt, '2'));
    await policy.execute(h.attempt, tag(log, rt, '3'));
    expectTokens(policy, 7);
  });

  it('never refunds a token when the downstream call fails', async () => {
    const rt = createFakeRuntime();
    const policy = rateLimit({ capacity: 3, refillPerSec: 10 });
    const h = testContext({ runtime: rt });
    const boom = new Error('provider exploded');
    await expect(policy.execute(h.attempt, () => Promise.reject(boom))).rejects.toBe(boom);
    expectTokens(policy, 2);
    expect(rt.pendingTimers).toBe(0);
  });

  it('keeps two limiter instances completely independent (RL-24)', async () => {
    const rt = createFakeRuntime();
    const left = rateLimit({ capacity: 1, refillPerSec: 10, key: 'left' });
    const right = rateLimit({ capacity: 1, refillPerSec: 10, key: 'right' });
    const log: string[] = [];
    const h = testContext({ runtime: rt });

    await left.execute(h.attempt, tag(log, rt, 'left-1'));
    expectTokens(left, 0);
    expectTokens(right, 1);

    await right.execute(h.attempt, tag(log, rt, 'right-1'));
    expectTokens(right, 0);
    expect(log).toEqual(['left-1@0', 'right-1@0']);
    expect(rt.pendingTimers).toBe(0);
  });
});
