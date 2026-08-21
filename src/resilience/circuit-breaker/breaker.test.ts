/**
 * The `Policy` wrapper: admission, fail-fast, events, and the concurrency edge
 * cases R4 §3.7 enumerates.
 *
 * Every test runs on the injected fake clock. Nothing here sleeps, and the
 * breaker schedules no timers at all — the `open -> half-open` transition is
 * lazy — so `runtime.pendingTimers` must be 0 at the end of every test.
 */

import { describe, expect, it } from 'vitest';
import { type FakeRuntime, fakeProvider, testContext } from '../../../test/support/index.ts';
import { pipeline } from '../../core/compose.ts';
import {
  CancelledError,
  CircuitOpenError,
  ConfigError,
  hasCode,
  TimeoutError,
  TransportError,
} from '../../core/errors.ts';
import { type BreakerOptions, POLICY_SCOPE, type Policy } from '../../core/policy.ts';
import type { AttemptContext, BreakerState } from '../../core/types.ts';
import { circuitBreaker, DEFAULT_IS_FAILURE } from './breaker.ts';

/* ------------------------------------------------------------------ *
 * Harness
 * ------------------------------------------------------------------ */

interface KeyView {
  readonly state: BreakerState;
  readonly openedAt: number;
  readonly halfOpenAt: number;
  readonly halfOpenInFlight: number;
  readonly generation: number;
  readonly bucketCount: number;
}

interface Harness {
  readonly policy: Policy<AttemptContext, unknown>;
  readonly ctx: AttemptContext;
  readonly runtime: FakeRuntime;
  readonly emitted: () => readonly {
    key: string;
    from: BreakerState;
    to: BreakerState;
    failures: number;
    at: number;
  }[];
  /** How many times the operation was actually entered. */
  readonly invoked: () => number;
  succeed(value?: unknown): Promise<unknown>;
  fail(error?: unknown): Promise<unknown>;
  /** Starts a call and hands back the lever that settles it. */
  start(): { readonly promise: Promise<unknown>; readonly gate: PromiseWithResolvers<unknown> };
  view(key?: string): KeyView | undefined;
}

function harness(options?: BreakerOptions, providerId = 'p1'): Harness {
  const handle = testContext({ provider: fakeProvider(providerId) });
  const policy = circuitBreaker(options);
  let invoked = 0;

  const run = (op: () => Promise<unknown>): Promise<unknown> =>
    policy.execute(handle.attempt, () => {
      invoked++;
      return op();
    });

  return {
    policy,
    ctx: handle.attempt,
    runtime: handle.runtime,
    emitted: () => handle.emitted('breaker:transition') as never,
    invoked: () => invoked,
    succeed: (value: unknown = 'ok') => run(() => Promise.resolve(value)),
    fail: (error: unknown = new TransportError('boom')) => run(() => Promise.reject(error)),
    start() {
      const gate = Promise.withResolvers<unknown>();
      return { promise: run(() => gate.promise), gate };
    },
    view(key = providerId) {
      const described = policy.describe?.() ?? {};
      const keys = (described as { keys?: Record<string, KeyView> }).keys ?? {};
      return keys[key];
    },
  };
}

/** Feeds a script of outcomes: `S` = success, `F` = failure. Errors are absorbed. */
async function drive(h: Harness, script: string): Promise<void> {
  for (const ch of script) {
    try {
      await (ch === 'S' ? h.succeed() : h.fail());
    } catch {
      /* the operation's error (or CircuitOpenError) is the script's business */
    }
  }
}

const OPEN_AFTER_TWO: BreakerOptions = { minimumThroughput: 2, failureRatio: 0.5, resetMs: 10_000 };

/* ------------------------------------------------------------------ *
 * A caller's own cancellation is not evidence about the provider
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — cancellation is scored NEITHER way', () => {
  it('a CancelledError never trips the breaker, however many arrive', async () => {
    const h = harness({ minimumThroughput: 2, failureRatio: 0.5 });
    for (let i = 0; i < 10; i++) {
      await h.fail(new CancelledError('caller hung up')).catch(() => undefined);
    }
    // The default `isFailure` counts every error, so before the carve-out this
    // was ten failures and an open circuit: a burst of client cancellations
    // took a perfectly healthy provider out of service.
    expect(h.view()?.state ?? 'closed').toBe('closed');
  });

  it('and is not laundered into a SUCCESS either', async () => {
    // Scoring it as a success is the opposite error: a provider so slow that
    // every caller gives up would read as 100 % healthy. It is recorded as
    // nothing at all, so the window stays empty.
    const h = harness({ minimumThroughput: 2, failureRatio: 0.5 });
    await h.fail(new CancelledError('gave up')).catch(() => undefined);
    expect(h.view()?.bucketCount ?? 0).toBe(0);

    // One real failure plus one cancellation must not read as 50 % of two.
    await h.fail().catch(() => undefined);
    expect(h.view()?.state).toBe('closed');
  });

  it('rethrows the cancellation unchanged', async () => {
    const h = harness();
    const cancelled = new CancelledError('stop');
    await expect(h.fail(cancelled)).rejects.toBe(cancelled);
  });

  it('still releases the half-open trial slot it was holding', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    await h.runtime.advance(10_000);

    // The probe is admitted, then the caller cancels it. If the slot were not
    // released, half-open would be deadlocked at `halfOpenMaxConcurrent: 1` and
    // the breaker could never close again.
    await h.fail(new CancelledError('probe cancelled')).catch(() => undefined);
    expect(h.view()?.state).toBe('half-open');
    expect(h.view()?.halfOpenInFlight).toBe(0);

    await h.succeed();
    expect(h.view()?.state).toBe('closed');
  });

  it('a TimeoutError is still a failure — that IS evidence about the provider', async () => {
    const h = harness({ minimumThroughput: 2, failureRatio: 0.5 });
    await h.fail(new TimeoutError(10, 'attempt')).catch(() => undefined);
    await h.fail(new TimeoutError(10, 'attempt')).catch(() => undefined);
    expect(h.view()?.state).toBe('open');
  });
});

/* ------------------------------------------------------------------ *
 * Pass-through and fail-fast
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — closed', () => {
  it('passes the value through and records nothing visible to the caller', async () => {
    const h = harness();
    await expect(h.succeed('hello')).resolves.toBe('hello');
    expect(h.invoked()).toBe(1);
    expect(h.view()?.state).toBe('closed');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('rethrows the operation error unchanged', async () => {
    const h = harness();
    const boom = new TransportError('boom');
    await expect(h.fail(boom)).rejects.toBe(boom);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('CB-6: alternating outcomes at the threshold ratio trip ON THE SUCCESS', async () => {
    const h = harness({ minimumThroughput: 10, failureRatio: 0.5 });
    // F S F S F S F S F — nine outcomes, five failures, still under throughput.
    await drive(h, 'FSFSFSFSF');
    expect(h.view()?.state, 'total 9 < minimumThroughput').toBe('closed');
    // The tenth outcome is a SUCCESS and it is what takes total to 10 at a 50 %
    // failure rate. A breaker evaluating only its failure path never trips here.
    await h.succeed();
    expect(h.view()?.state).toBe('open');
    expect(h.emitted().at(-1)).toMatchObject({ from: 'closed', to: 'open', failures: 5 });
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('CB-2/CB-3: 9 failures stay closed, the 10th opens', async () => {
    const h = harness();
    await drive(h, 'FFFFFFFFF');
    expect(h.view()?.state).toBe('closed');
    await drive(h, 'F');
    expect(h.view()?.state).toBe('open');
    expect(h.invoked(), 'every one of the ten calls actually ran').toBe(10);
  });
});

describe('circuit breaker policy — open', () => {
  it('CB-7/CB-8: fails fast with CircuitOpenError without invoking the operation', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    expect(h.view()?.state).toBe('open');
    const before = h.invoked();

    const error = await h.succeed().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CircuitOpenError);
    expect(hasCode(error, 'CIRCUIT_OPEN')).toBe(true);
    const open = error as CircuitOpenError;
    expect(open.key).toBe('p1');
    expect(open.providerId).toBe('p1');
    expect(open.openedAt).toBe(0);
    // CB-8: R4 calls this `openUntil`; the core taxonomy calls it `halfOpenAt`.
    expect(open.halfOpenAt).toBe(open.openedAt + 10_000);
    expect(open.retryAfterMs).toBe(10_000);
    expect(open.retryable, 'a different provider may well work').toBe(true);
    expect(h.invoked(), 'the operation was never entered').toBe(before);
  });

  it('retryAfterMs counts down as the cooldown elapses', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    expect(h.view()?.openedAt).toBe(0);

    await h.runtime.advance(4_000);
    const midway = (await h.succeed().catch((e: unknown) => e)) as CircuitOpenError;
    // `halfOpenAt - now`, not `halfOpenAt - openedAt`. The old form always
    // answered the configured resetMs, whatever the clock said.
    expect(midway.retryAfterMs).toBe(6_000);
    expect(midway.halfOpenAt).toBe(10_000);

    await h.runtime.advance(5_999);
    const nearlyDone = (await h.succeed().catch((e: unknown) => e)) as CircuitOpenError;
    expect(nearlyDone.retryAfterMs).toBe(1);
  });

  it('CB-25: rejections are not recorded as outcomes', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    const openedAt = h.view()?.openedAt;
    for (let i = 0; i < 20; i++) {
      await expect(h.succeed()).rejects.toBeInstanceOf(CircuitOpenError);
    }
    expect(h.view()?.bucketCount, 'the window stayed empty').toBe(0);
    expect(h.view()?.openedAt, 'the cooldown was not extended').toBe(openedAt);
    expect(h.view()?.state).toBe('open');
  });

  it('CB-9/CB-10/CB-26: the transition is lazy and lands exactly at resetMs', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');

    await h.runtime.advance(9_999);
    expect(h.view()?.state, 'clock alone changes nothing').toBe('open');
    await expect(h.succeed()).rejects.toBeInstanceOf(CircuitOpenError);

    await h.runtime.advance(1);
    // Still 'open' as STORED state: no timer moved it. Only the next call does.
    expect(h.view()?.state).toBe('open');
    await expect(h.succeed()).resolves.toBe('ok');
    expect(h.view()?.state, 'the probe succeeded and closed it').toBe('closed');
    expect(h.runtime.pendingTimers, 'the breaker owns no timers').toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Half-open concurrency
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — half-open', () => {
  async function openThenCoolDown(h: Harness): Promise<void> {
    await drive(h, 'FF');
    expect(h.view()?.state).toBe('open');
    await h.runtime.advance(10_000);
  }

  it('CB-11: a second concurrent trial is rejected at halfOpenMaxConcurrent 1', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await openThenCoolDown(h);

    const first = h.start();
    const second = await h.succeed().catch((e: unknown) => e);
    expect(second, 'the probe slot was taken').toBeInstanceOf(CircuitOpenError);
    expect(h.invoked(), 'only the admitted probe ran').toBe(3);

    first.gate.resolve('probe');
    await expect(first.promise).resolves.toBe('probe');
    expect(h.view()?.state).toBe('closed');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('CB-12: halfOpenMaxConcurrent 2 admits two trials and rejects the third', async () => {
    const h = harness({ ...OPEN_AFTER_TWO, halfOpenMaxConcurrent: 2, halfOpenSuccessesToClose: 2 });
    await openThenCoolDown(h);

    const a = h.start();
    const b = h.start();
    const third = await h.succeed().catch((e: unknown) => e);
    expect(third).toBeInstanceOf(CircuitOpenError);
    expect(h.view()?.halfOpenInFlight).toBe(2);

    a.gate.resolve(1);
    b.gate.resolve(2);
    await expect(Promise.all([a.promise, b.promise])).resolves.toEqual([1, 2]);
    expect(h.view()?.state).toBe('closed');
  });

  it('CB-23: N calls racing the transition admit exactly halfOpenMaxConcurrent', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await openThenCoolDown(h);
    const entered = h.invoked();

    // Five calls dispatched with no `await` between them. The admission check
    // and the in-flight increment are synchronous, so exactly one wins.
    const started = [h.start(), h.start(), h.start(), h.start(), h.start()];
    expect(h.invoked() - entered, 'exactly one operation was entered').toBe(1);

    for (const s of started) s.gate.resolve('probe');
    const settled = await Promise.allSettled(started.map((s) => s.promise));
    expect(settled.filter((r) => r.status === 'fulfilled')).toHaveLength(1);
    const rejections = settled.filter((r) => r.status === 'rejected');
    expect(rejections).toHaveLength(4);
    for (const r of rejections) {
      expect(hasCode(r.reason, 'CIRCUIT_OPEN')).toBe(true);
    }
    expect(h.view()?.state).toBe('closed');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('CB-15: a failed trial re-opens and restarts the full cooldown (CB-16)', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await openThenCoolDown(h);
    await h.runtime.advance(5_000); // now = 15_000

    await expect(h.fail()).rejects.toBeInstanceOf(TransportError);
    expect(h.view()?.state).toBe('open');
    expect(h.view()?.openedAt, 'openedAt restarts, not resumes').toBe(15_000);
    expect(h.view()?.halfOpenAt).toBe(25_000);

    await h.runtime.advance(9_999);
    await expect(h.succeed()).rejects.toBeInstanceOf(CircuitOpenError);
    await h.runtime.advance(1);
    await expect(h.succeed()).resolves.toBe('ok');
  });

  it('CB-17: a freshly closed breaker does not re-trip on the next failure', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await openThenCoolDown(h);
    await expect(h.succeed()).resolves.toBe('ok');
    expect(h.view()?.state).toBe('closed');

    await expect(h.fail()).rejects.toBeInstanceOf(TransportError);
    expect(h.view()?.state, 'the window was cleared on close').toBe('closed');
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Generation counter — stale settlements
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — stale settlements', () => {
  it('CB-24: a call in flight when the breaker opens is not cancelled and is discarded', async () => {
    const h = harness(OPEN_AFTER_TWO);
    const inFlight = h.start();

    await drive(h, 'FF');
    expect(h.view()?.state).toBe('open');
    expect(h.ctx.signal.aborted, 'the breaker never cancels an in-flight call').toBe(false);

    inFlight.gate.resolve('late');
    await expect(inFlight.promise).resolves.toBe('late');
    expect(h.view()?.state, 'a superseded success cannot close the circuit').toBe('open');
    expect(h.view()?.bucketCount, 'nor land in the new window').toBe(0);
  });

  it('CB-22: a slow trial cannot close a circuit that has cycled back into half-open', async () => {
    // The race the generation counter exists for. `'open'` discards outcomes by
    // itself, so the dangerous window is the SECOND half-open period: a bare
    // success from the first probe would close a circuit that a different probe
    // is currently trialling.
    const h = harness({ ...OPEN_AFTER_TWO, halfOpenMaxConcurrent: 2 });
    await drive(h, 'FF');
    await h.runtime.advance(10_000);

    const slow = h.start(); // probe A — admitted, and never settles for a while
    const doomed = h.start(); // probe B — fails and re-opens the breaker
    doomed.gate.reject(new TransportError('still down'));
    await expect(doomed.promise).rejects.toBeInstanceOf(TransportError);
    expect(h.view()?.state).toBe('open');

    await h.runtime.advance(10_000);
    const fresh = h.start(); // probe C — a new half-open generation
    expect(h.view()?.state).toBe('half-open');

    slow.gate.resolve('very late');
    await expect(slow.promise).resolves.toBe('very late');
    expect(h.view()?.state, 'probe A is void; C is still the one on trial').toBe('half-open');
    expect(h.view()?.halfOpenInFlight, "and A's slot is not double-released").toBe(1);

    fresh.gate.resolve('ok');
    await expect(fresh.promise).resolves.toBe('ok');
    expect(h.view()?.state).toBe('closed');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('CB-22: half-open failure decides; the slower success from the old generation is void', async () => {
    const h = harness({ ...OPEN_AFTER_TWO, halfOpenMaxConcurrent: 2 });
    await drive(h, 'FF');
    await h.runtime.advance(10_000);

    const a = h.start();
    const b = h.start();
    const generation = h.view()?.generation;

    // B fails first: the breaker re-opens and the generation moves.
    b.gate.reject(new TransportError('still down'));
    await expect(b.promise).rejects.toBeInstanceOf(TransportError);
    expect(h.view()?.state).toBe('open');
    expect(h.view()?.generation).not.toBe(generation);

    // A now succeeds — from the superseded generation. It must NOT close it.
    a.gate.resolve('late');
    await expect(a.promise).resolves.toBe('late');
    expect(h.view()?.state).toBe('open');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('success and failure "simultaneously" in half-open: the first settlement decides', async () => {
    const h = harness({ ...OPEN_AFTER_TWO, halfOpenMaxConcurrent: 2 });
    await drive(h, 'FF');
    await h.runtime.advance(10_000);

    const a = h.start();
    const b = h.start();
    // Node serialises these on the microtask queue; A lands first.
    a.gate.resolve('first');
    b.gate.reject(new TransportError('second'));

    await expect(a.promise).resolves.toBe('first');
    expect(h.view()?.state, 'one success closed it').toBe('closed');
    await expect(b.promise).rejects.toBeInstanceOf(TransportError);
    expect(h.view()?.state, 'the loser is voided by the generation counter').toBe('closed');
    expect(h.view()?.bucketCount, 'and is not recorded in the new window').toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * isFailure
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — isFailure', () => {
  it('CB-20: an excluded error is recorded as a success and still rethrown', async () => {
    const notFound = new TransportError('404');
    const h = harness({
      minimumThroughput: 2,
      failureRatio: 0.5,
      isFailure: (error: unknown) => error !== notFound,
    });
    await expect(h.fail(notFound)).rejects.toBe(notFound);
    await expect(h.fail(notFound)).rejects.toBe(notFound);
    expect(h.view()?.state).toBe('closed');
    expect(h.view()?.bucketCount, 'it WAS recorded, as a success').toBe(1);
  });

  it('CB-21: 20 excluded errors in a row leave the breaker closed', async () => {
    const h = harness({ minimumThroughput: 2, isFailure: () => false });
    for (let i = 0; i < 20; i++) {
      await expect(h.fail()).rejects.toBeInstanceOf(TransportError);
    }
    expect(h.view()?.state).toBe('closed');
  });

  it('the default predicate counts every error as a failure (R4 §5.3)', () => {
    expect(DEFAULT_IS_FAILURE(new TransportError('x'))).toBe(true);
    expect(DEFAULT_IS_FAILURE('anything at all')).toBe(true);
  });
});

/* ------------------------------------------------------------------ *
 * Keying, events, metadata
 * ------------------------------------------------------------------ */

describe('circuit breaker policy — keying and metadata', () => {
  it('keys state per provider: a dead provider does not fail-fast a healthy one', async () => {
    const policy = circuitBreaker(OPEN_AFTER_TWO);
    const dead = testContext({ provider: fakeProvider('dead') });
    const healthy = testContext({ provider: fakeProvider('healthy'), runtime: dead.runtime });
    const boom = new TransportError('boom');

    for (let i = 0; i < 2; i++) {
      await expect(policy.execute(dead.attempt, () => Promise.reject(boom))).rejects.toBe(boom);
    }
    await expect(policy.execute(dead.attempt, () => Promise.resolve('x'))).rejects.toBeInstanceOf(
      CircuitOpenError,
    );
    await expect(policy.execute(healthy.attempt, () => Promise.resolve('x'))).resolves.toBe('x');
  });

  it('emits breaker:transition for every state change, with the causing failure count', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    await h.runtime.advance(10_000);
    await expect(h.succeed()).resolves.toBe('ok');

    expect(h.emitted()).toEqual([
      { key: 'p1', from: 'closed', to: 'open', failures: 2, at: 0 },
      { key: 'p1', from: 'open', to: 'half-open', failures: 0, at: 10_000 },
      { key: 'p1', from: 'half-open', to: 'closed', failures: 0, at: 10_000 },
    ]);
  });

  it('carries the metadata the composer orders on', () => {
    const policy = circuitBreaker();
    expect(policy.kind).toBe('circuit-breaker');
    expect(policy.scope).toBe(POLICY_SCOPE['circuit-breaker']);
    expect(policy.name).toBe('circuit-breaker');
    expect(typeof policy.execute).toBe('function');
  });

  it('describe() is side-effect free and dispose() is idempotent', async () => {
    const h = harness(OPEN_AFTER_TWO);
    await drive(h, 'FF');
    expect(h.policy.describe?.()).toEqual(h.policy.describe?.());
    expect(h.view()?.state).toBe('open');

    await h.policy.dispose?.();
    await h.policy.dispose?.();
    expect(h.view(), 'state was released').toBeUndefined();
    // A disposed breaker starts clean rather than throwing.
    await expect(h.succeed()).resolves.toBe('ok');
  });

  it('CB-27: an invalid option is rejected by the factory, not at call time', () => {
    expect(() => circuitBreaker({ minimumThroughput: 1 })).toThrow(ConfigError);
  });

  it('works as ordinary middleware through core compose()', async () => {
    const h = testContext({ provider: fakeProvider('p1') });
    const policy = circuitBreaker(OPEN_AFTER_TWO);
    const boom = new TransportError('boom');
    let calls = 0;
    const run = pipeline<AttemptContext, unknown>([policy.execute], () => {
      calls++;
      return Promise.reject(boom);
    });

    await expect(run(h.attempt)).rejects.toBe(boom);
    await expect(run(h.attempt)).rejects.toBe(boom);
    await expect(run(h.attempt)).rejects.toBeInstanceOf(CircuitOpenError);
    expect(calls, 'the third call never reached the terminal').toBe(2);
    expect(h.runtime.pendingTimers).toBe(0);
  });
});
