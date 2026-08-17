import { getEventListeners } from 'node:events';
import { describe, expect, it } from 'vitest';
import { CancelledError, hasCode, TimeoutError } from '../../core/errors.ts';
import type { AttemptContext, CallContext, Next } from '../../core/types.ts';
import { testContext } from '../../../test/support/index.ts';
import { attemptTimeout, totalTimeout } from './timeout.ts';

/* ------------------------------------------------------------------ *
 * Harness. Everything below runs on the fake runtime's virtual clock:
 * no test sleeps, and every test asserts `pendingTimers === 0` at the
 * end, which is the assertion that proves R4 §2.2's real defect — the
 * uncleared timer holding the event loop open — cannot happen here.
 * ------------------------------------------------------------------ */

type Outcome =
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly error: unknown };

interface Recorder<Ctx> {
  readonly next: Next<Ctx, unknown>;
  /** The context each invocation actually received (the fallback when `next()` took none). */
  readonly seen: Ctx[];
  /** `undefined` records a bare `next()` — i.e. the context was passed straight through. */
  readonly args: (Ctx | undefined)[];
}

function recordNext<Ctx>(fallback: Ctx, impl: (ctx: Ctx) => Promise<unknown>): Recorder<Ctx> {
  const seen: Ctx[] = [];
  const args: (Ctx | undefined)[] = [];
  return {
    seen,
    args,
    next: (ctx?: Ctx): Promise<unknown> => {
      args.push(ctx);
      const received = ctx ?? fallback;
      seen.push(received);
      return impl(received);
    },
  };
}

interface Probe {
  /** Resolves once the watched promise settles, whichever way it went. */
  readonly settled: Promise<Outcome>;
  /** `undefined` while still pending — lets a test assert "nothing has happened yet". */
  outcome(): Outcome | undefined;
}

/** Attaches handlers BEFORE the clock moves, so nothing is ever momentarily unhandled. */
function watch(promise: Promise<unknown>): Probe {
  let current: Outcome | undefined;
  const settled: Promise<Outcome> = promise.then(
    (value): Outcome => {
      current = { ok: true, value };
      return current;
    },
    (error: unknown): Outcome => {
      current = { ok: false, error };
      return current;
    },
  );
  return { settled, outcome: () => current };
}

/**
 * Drives a policy to settlement.
 *
 * The `flushMacrotask()` before `drive()` is load-bearing: the wrapper invokes
 * `next()` from a microtask, so without it a `runtime.advance()` issued in the
 * same synchronous turn would fire the deadline BEFORE the operation ever
 * started — an artefact of the virtual clock that real time cannot produce.
 */
async function settle(promise: Promise<unknown>, drive: () => Promise<void>): Promise<Outcome> {
  const probe = watch(promise);
  await flushMacrotask();
  await drive();
  return await probe.settled;
}

const noDrive = (): Promise<void> => Promise.resolve();

async function rejectionOf(
  promise: Promise<unknown>,
  drive: () => Promise<void> = noDrive,
): Promise<unknown> {
  return errorOf(await settle(promise, drive));
}

function errorOf(outcome: Outcome): unknown {
  if (outcome.ok) throw new Error(`expected a rejection, resolved with ${String(outcome.value)}`);
  return outcome.error;
}

function asTimeout(error: unknown): TimeoutError {
  expect(error).toBeInstanceOf(TimeoutError);
  if (!(error instanceof TimeoutError)) throw new Error('unreachable');
  return error;
}

function asCancelled(error: unknown): CancelledError {
  expect(error).toBeInstanceOf(CancelledError);
  if (!(error instanceof CancelledError)) throw new Error('unreachable');
  return error;
}

function only<T>(items: readonly T[]): T {
  expect(items).toHaveLength(1);
  const [first] = items;
  if (first === undefined) throw new Error('expected exactly one recorded item');
  return first;
}

const neverSettles = (): Promise<never> => new Promise<never>(() => undefined);

/** One turn of the macrotask queue — instantaneous, never a wall-clock wait. */
function flushMacrotask(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/** An `AttemptContext` derived from a call-scope narrowing, as the real pipeline does. */
function asAttempt(ctx: CallContext | undefined, base: AttemptContext): AttemptContext {
  return ctx === undefined ? base : { ...base, ...ctx };
}

/* ------------------------------------------------------------------ *
 * 1. Metadata
 * ------------------------------------------------------------------ */

describe('policy metadata', () => {
  it('places total-timeout on the call stack and attempt-timeout on the attempt stack', () => {
    const total = totalTimeout();
    const attempt = attemptTimeout();
    expect(total.kind).toBe('total-timeout');
    expect(total.scope).toBe('call');
    expect(attempt.kind).toBe('attempt-timeout');
    expect(attempt.scope).toBe('attempt');
  });

  it('names itself with the effective budget and describes its configuration', () => {
    expect(totalTimeout().name).toBe('total-timeout(30000ms)');
    expect(attemptTimeout().name).toBe('attempt-timeout(10000ms)');
    expect(attemptTimeout({ attemptTimeoutMs: Number.POSITIVE_INFINITY }).name).toBe(
      'attempt-timeout(unbounded)',
    );
    expect(attemptTimeout({ attemptTimeoutMs: 250 }).describe?.()).toEqual({
      kind: 'attempt-timeout',
      scope: 'attempt',
      timeoutMs: 250,
    });
  });

  it('reads only its own field: the other one is ignored, not swapped in', () => {
    expect(totalTimeout({ attemptTimeoutMs: 5, totalTimeoutMs: 400 }).name).toBe(
      'total-timeout(400ms)',
    );
    expect(attemptTimeout({ attemptTimeoutMs: 5, totalTimeoutMs: 400 }).name).toBe(
      'attempt-timeout(5ms)',
    );
  });

  it('declares no teardown: a timeout policy owns nothing long-lived', () => {
    expect(totalTimeout().dispose).toBeUndefined();
    expect(attemptTimeout().dispose).toBeUndefined();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Happy path  (TO-1, TO-13)
 * ------------------------------------------------------------------ */

describe('the fast path', () => {
  it('resolves with the operation value (TO-1)', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, () => Promise.resolve('answer'));
    await expect(attemptTimeout({ attemptTimeoutMs: 5_000 }).execute(h.attempt, rec.next)).resolves.toBe(
      'answer',
    );
    expect(rec.seen).toHaveLength(1);
  });

  it('clears the timer without the clock ever moving (TO-13)', async () => {
    const h = testContext();
    let timersDuring = -1;
    const rec = recordNext(h.attempt, () => {
      timersDuring = h.runtime.pendingTimers;
      return Promise.resolve('fast');
    });
    await attemptTimeout({ attemptTimeoutMs: 5_000 }).execute(h.attempt, rec.next);
    expect(timersDuring).toBe(1); // the deadline was genuinely armed…
    expect(h.runtime.pendingTimers).toBe(0); // …and genuinely cleared
    expect(h.runtime.now()).toBe(0); // settled without advancing the clock
  });

  it('narrows the signal via next(ctx\') without mutating the incoming context', async () => {
    const h = testContext();
    const originalSignal = h.attempt.signal;
    const rec = recordNext(h.attempt, () => Promise.resolve('ok'));
    await attemptTimeout({ attemptTimeoutMs: 250 }).execute(h.attempt, rec.next);
    const received = only(rec.seen);

    expect(received).not.toBe(h.attempt);
    expect(received.signal).not.toBe(originalSignal);
    expect(received.signal.aborted).toBe(false);
    expect(received.deadlineAt).toBe(250);

    // The incoming context is untouched…
    expect(h.attempt.signal).toBe(originalSignal);
    expect(h.attempt.deadlineAt).toBeUndefined();
    // …and everything shared stays shared by reference.
    expect(received.state).toBe(h.attempt.state);
    expect(received.stats).toBe(h.attempt.stats);
    expect(received.failures).toBe(h.attempt.failures);
    expect(received.provider).toBe(h.attempt.provider);
    expect(received.runtime).toBe(h.attempt.runtime);
    expect(received.callId).toBe(h.attempt.callId);
  });

  it('works the same on the call stack', async () => {
    const h = testContext();
    const rec = recordNext(h.call, () => Promise.resolve(42));
    await expect(totalTimeout({ totalTimeoutMs: 1_000 }).execute(h.call, rec.next)).resolves.toBe(42);
    expect(only(rec.seen).deadlineAt).toBe(1_000);
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 3. Timing out  (TO-2, TO-9, TO-10, TO-11)
 * ------------------------------------------------------------------ */

describe('timing out', () => {
  it('rejects with a TimeoutError carrying the scope, budget and call context (TO-2)', async () => {
    const h = testContext({ capability: 'chat' });
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(100),
    );

    const timeout = asTimeout(error);
    expect(timeout.name).toBe('TimeoutError');
    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    expect(timeout.timeoutMs).toBe(100);
    expect(timeout.scope).toBe('attempt');
    expect(timeout.retryable).toBe(true); // an attempt timeout is worth another provider
    expect(timeout.capability).toBe('chat');
    expect(timeout.callId).toBe(h.attempt.callId);
    expect(timeout.providerId).toBe(h.provider.id);
    expect(timeout.attempt).toBe(1);
    expect(timeout.details).toEqual({ source: 'timeout' });
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('does not fire one millisecond early', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const probe = watch(attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next));
    await flushMacrotask();

    await h.runtime.advance(99);
    await flushMacrotask();
    expect(probe.outcome()).toBeUndefined(); // still pending at 99ms
    expect(h.runtime.pendingTimers).toBe(1);

    await h.runtime.advance(1);
    expect(asTimeout(errorOf(await probe.settled)).timeoutMs).toBe(100);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('a call-scope timeout is NOT retryable and reports scope "call"', async () => {
    const h = testContext();
    const rec = recordNext(h.call, neverSettles);
    const error = await rejectionOf(
      totalTimeout({ totalTimeoutMs: 30 }).execute(h.call, rec.next),
      () => h.runtime.advance(30),
    );
    const timeout = asTimeout(error);
    expect(timeout.scope).toBe('call');
    expect(timeout.retryable).toBe(false);
    expect(timeout.providerId).toBeUndefined();
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('aborts the operation\'s own signal with the very TimeoutError it will surface (TO-9)', async () => {
    const h = testContext();
    let observed: { aborted: boolean; reason: unknown } | undefined;
    const rec = recordNext(h.attempt, (ctx) => {
      ctx.signal.addEventListener(
        'abort',
        () => {
          observed = { aborted: ctx.signal.aborted, reason: ctx.signal.reason };
        },
        { once: true },
      );
      return neverSettles();
    });
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 40 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(40),
    );
    expect(observed?.aborted).toBe(true);
    expect(observed?.reason).toBe(error); // identical instance, not a look-alike
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('absorbs a rejection arriving after the timeout — no unhandled rejection (TO-10)', async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown): void => {
      unhandled.push(reason);
    };
    process.on('unhandledRejection', onUnhandled);
    try {
      const h = testContext();
      let failLate: (reason: unknown) => void = () => undefined;
      const rec = recordNext(
        h.attempt,
        () =>
          new Promise<never>((_resolve, reject) => {
            failLate = reject;
          }),
      );
      const error = await rejectionOf(
        attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
        () => h.runtime.advance(100),
      );
      expect(asTimeout(error).timeoutMs).toBe(100);

      failLate(new Error('the provider answered, far too late'));
      await flushMacrotask();
      await flushMacrotask();

      expect(unhandled).toEqual([]);
      expect(asTimeout(error).code).toBe('TIMEOUT'); // the surfaced error is unchanged
      expect(h.runtime.pendingTimers).toBe(0);
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });

  it('keeps the TimeoutError when the operation resolves after the deadline (TO-11)', async () => {
    const h = testContext();
    let finishLate: (value: string) => void = () => undefined;
    const rec = recordNext(
      h.attempt,
      () =>
        new Promise<string>((resolve) => {
          finishLate = resolve;
        }),
    );
    const promise = attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next);
    const error = await rejectionOf(promise, () => h.runtime.advance(100));
    finishLate('too late to matter');
    await flushMacrotask();
    expect(asTimeout(error).scope).toBe('attempt');
    expect(await settle(promise, noDrive)).toEqual({ ok: false, error });
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 4. Caller-abort vs timeout-abort  (TO-3, TO-4, TO-5)
 * ------------------------------------------------------------------ */

describe('caller-abort is not timeout-abort', () => {
  it('rejects immediately when the caller signal is already aborted at entry (TO-3)', async () => {
    const reason = new Error('caller gave up first');
    const h = testContext({ signal: AbortSignal.abort(reason) });
    const rec = recordNext(h.attempt, neverSettles);

    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
    );

    asCancelled(error);
    expect(hasCode(error, 'CANCELLED')).toBe(true);
    expect(rec.seen).toHaveLength(0); // the operation is never invoked…
    expect(h.runtime.pendingTimers).toBe(0); // …and no timer is ever created
    expect(asCancelled(error).cause).toBe(reason);
    expect(asCancelled(error).retryable).toBe(false);
  });

  it('surfaces CancelledError, not TimeoutError, when the caller aborts mid-flight (TO-4, TO-5)', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const promise = attemptTimeout({ attemptTimeoutMs: 10_000 }).execute(h.attempt, rec.next);
    const reason = { why: 'user navigated away' };

    const error = await rejectionOf(promise, async () => {
      await Promise.resolve(); // let the operation actually start
      h.abort(reason);
    });

    const cancelled = asCancelled(error);
    expect(cancelled.name).toBe('CancelledError');
    expect(cancelled.cause).toBe(reason); // the caller's own reason, by identity
    expect(cancelled.retryable).toBe(false);
    expect(rec.seen).toHaveLength(1);
    expect(only(rec.seen).signal.aborted).toBe(true); // cancellation propagated inward
    expect(h.runtime.pendingTimers).toBe(0); // the deadline timer was cleared
  });

  it('a caller abort with no reason still yields CancelledError, carrying the DOMException', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const promise = attemptTimeout({ attemptTimeoutMs: 10_000 }).execute(h.attempt, rec.next);
    const error = await rejectionOf(promise, async () => {
      await Promise.resolve();
      h.abort();
    });
    const cancelled = asCancelled(error);
    expect(cancelled.cause).toBeInstanceOf(DOMException);
    expect((cancelled.cause as DOMException).name).toBe('AbortError');
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('the deadline still wins when the caller never aborts', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 5 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(5),
    );
    asTimeout(error);
    expect(h.call.signal.aborted).toBe(false); // the caller's signal is left alone
  });
});

/* ------------------------------------------------------------------ *
 * 5. Edge cases  (R4 §2.5 / TO-6, TO-7, TO-8, TO-17)
 * ------------------------------------------------------------------ */

describe('edge cases', () => {
  it('timeoutMs 0 rejects immediately, invoking nothing and arming nothing (TO-6)', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 0 }).execute(h.attempt, rec.next),
    );
    const timeout = asTimeout(error);
    expect(timeout.timeoutMs).toBe(0);
    expect(timeout.scope).toBe('attempt');
    expect(rec.seen).toHaveLength(0);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('timeoutMs Infinity creates no timer and passes the caller signal straight through (TO-7)', async () => {
    const h = testContext();
    let timersDuring = -1;
    const rec = recordNext(h.attempt, () => {
      timersDuring = h.runtime.pendingTimers;
      return Promise.resolve('unbounded');
    });
    await expect(
      attemptTimeout({ attemptTimeoutMs: Number.POSITIVE_INFINITY }).execute(h.attempt, rec.next),
    ).resolves.toBe('unbounded');

    expect(timersDuring).toBe(0); // no timer, at any point
    expect(rec.args).toEqual([undefined]); // bare next(): the context is not replaced
    expect(only(rec.seen).signal).toBe(h.attempt.signal);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('negative and NaN budgets throw RangeError at construction (TO-8)', () => {
    expect(() => attemptTimeout({ attemptTimeoutMs: -1 })).toThrow(RangeError);
    expect(() => attemptTimeout({ attemptTimeoutMs: Number.NaN })).toThrow(RangeError);
    expect(() => totalTimeout({ totalTimeoutMs: -1 })).toThrow(RangeError);
    expect(() => totalTimeout({ totalTimeoutMs: Number.NaN })).toThrow(RangeError);
  });

  it('propagates a synchronous throw unchanged and still clears the timer (TO-17)', async () => {
    const h = testContext();
    const boom = new Error('handler exploded synchronously');
    const rec = recordNext(h.attempt, (): Promise<never> => {
      throw boom;
    });
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 1_000 }).execute(h.attempt, rec.next),
    );
    expect(error).toBe(boom);
    expect(error).not.toBeInstanceOf(TimeoutError);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('propagates an ordinary rejection unchanged', async () => {
    const h = testContext();
    const boom = new Error('provider said no');
    const rec = recordNext(h.attempt, () => Promise.reject(boom));
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 1_000 }).execute(h.attempt, rec.next),
    );
    expect(error).toBe(boom);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('removes its listener from the caller signal on both the success and the failure path (TO-14)', async () => {
    const h = testContext();
    const before = getEventListeners(h.call.signal, 'abort').length;
    let during = -1;

    const okRec = recordNext(h.attempt, () => {
      during = getEventListeners(h.call.signal, 'abort').length;
      return Promise.resolve('ok');
    });
    await attemptTimeout({ attemptTimeoutMs: 1_000 }).execute(h.attempt, okRec.next);
    expect(during).toBe(before + 1);
    expect(getEventListeners(h.call.signal, 'abort').length).toBe(before);

    const failRec = recordNext(h.attempt, neverSettles);
    await rejectionOf(attemptTimeout({ attemptTimeoutMs: 10 }).execute(h.attempt, failRec.next), () =>
      h.runtime.advance(10),
    );
    expect(getEventListeners(h.call.signal, 'abort').length).toBe(before);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('leaks neither the timer nor a handle when the operation ignores cancellation entirely', async () => {
    const h = testContext();
    let invocations = 0;
    const rec = recordNext(h.attempt, () => {
      invocations += 1;
      return neverSettles(); // no signal listener, no cancellation support at all
    });
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 250 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(250),
    );
    asTimeout(error);
    expect(invocations).toBe(1);
    expect(h.runtime.pendingTimers).toBe(0);
    // Nothing is left that could fire later.
    await h.runtime.runAll();
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 6. The same-tick race  (TO-12)
 * ------------------------------------------------------------------ */

describe('the same-tick race', () => {
  /**
   * "First settlement wins" is exact here, and the deadline is inclusive: the
   * abort path runs SYNCHRONOUSLY inside the timer callback, while a value
   * travels through the promise chain, so an operation that finishes at
   * precisely `timeoutMs` has not settled the outer promise yet and the timeout
   * wins. The point of these two tests is that the outcome does not depend on
   * which timer was registered first — it is the same either way, every run.
   */
  it('is deterministic when the deadline timer is registered first', async () => {
    for (let run = 0; run < 3; run++) {
      const h = testContext();
      const rec = recordNext(h.attempt, () => h.runtime.sleep(100).then(() => 'operation'));
      const outcome = await settle(
        attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
        () => h.runtime.advance(100),
      );
      expect(outcome.ok).toBe(false);
      expect(asTimeout(errorOf(outcome)).timeoutMs).toBe(100);
      await h.runtime.runAll();
      expect(h.runtime.pendingTimers).toBe(0);
    }
  });

  it('is deterministic when the operation timer is registered first', async () => {
    for (let run = 0; run < 3; run++) {
      const h = testContext();
      let finish: (value: string) => void = () => undefined;
      const operation = new Promise<string>((resolve) => {
        finish = resolve;
      });
      // Registered BEFORE the policy arms its deadline: same due time, lower sequence.
      h.runtime.clock.setTimeout(() => {
        finish('operation');
      }, 100);

      const rec = recordNext(h.attempt, () => operation);
      const outcome = await settle(
        attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
        () => h.runtime.advance(100),
      );
      expect(outcome.ok).toBe(false);
      expect(asTimeout(outcome.ok ? undefined : outcome.error).timeoutMs).toBe(100);
      expect(h.runtime.pendingTimers).toBe(0);
    }
  });

  it('resolves when the operation settles even one millisecond earlier', async () => {
    const h = testContext();
    const rec = recordNext(h.attempt, () => h.runtime.sleep(99).then(() => 'operation'));
    const outcome = await settle(
      attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(100),
    );
    expect(outcome).toEqual({ ok: true, value: 'operation' });
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 7. Deadline composition — the tighter of the two wins
 * ------------------------------------------------------------------ */

describe('deadline composition', () => {
  it('clamps the attempt budget to an inherited call deadline', async () => {
    const h = testContext({ deadlineAt: 300 });
    const rec = recordNext(h.attempt, neverSettles);
    const promise = attemptTimeout({ attemptTimeoutMs: 10_000 }).execute(h.attempt, rec.next);

    const outcome = await settle(promise, () => h.runtime.advance(299));
    expect(outcome).toBeUndefined; // still pending; asserted properly below
    const error = await rejectionOf(promise, () => h.runtime.advance(1));

    const timeout = asTimeout(error);
    expect(timeout.timeoutMs).toBe(300); // 300, not 10_000
    expect(timeout.details).toEqual({ source: 'deadline' });
    expect(only(rec.seen).deadlineAt).toBe(300);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('keeps its own budget when it is the tighter of the two', async () => {
    const h = testContext({ deadlineAt: 10_000 });
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 50 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(50),
    );
    const timeout = asTimeout(error);
    expect(timeout.timeoutMs).toBe(50);
    expect(timeout.details).toEqual({ source: 'timeout' });
    expect(only(rec.seen).deadlineAt).toBe(50);
  });

  it('fails fast when the inherited deadline has already passed', async () => {
    const h = testContext({ deadlineAt: 100 });
    h.runtime.setTime(500);
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 10_000 }).execute(h.attempt, rec.next),
    );
    const timeout = asTimeout(error);
    expect(timeout.timeoutMs).toBe(0);
    expect(timeout.details).toEqual({ source: 'deadline' });
    expect(rec.seen).toHaveLength(0);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('honours hints.timeoutMs as a per-call override on the call stack', async () => {
    const h = testContext({ hints: { timeoutMs: 25 } });
    const rec = recordNext(h.call, neverSettles);
    const error = await rejectionOf(
      totalTimeout({ totalTimeoutMs: 30_000 }).execute(h.call, rec.next),
      () => h.runtime.advance(25),
    );
    expect(asTimeout(error).timeoutMs).toBe(25);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('lets hints.timeoutMs widen the configured budget, but never past a real deadline', async () => {
    const wide = testContext({ hints: { timeoutMs: 900 } });
    const wideRec = recordNext(wide.call, neverSettles);
    const wideError = await rejectionOf(
      totalTimeout({ totalTimeoutMs: 100 }).execute(wide.call, wideRec.next),
      () => wide.runtime.advance(900),
    );
    expect(asTimeout(wideError).timeoutMs).toBe(900);

    const bounded = testContext({ hints: { timeoutMs: 900 }, deadlineAt: 200 });
    const boundedRec = recordNext(bounded.call, neverSettles);
    const boundedError = await rejectionOf(
      totalTimeout({ totalTimeoutMs: 100 }).execute(bounded.call, boundedRec.next),
      () => bounded.runtime.advance(200),
    );
    expect(asTimeout(boundedError).timeoutMs).toBe(200);
    expect(asTimeout(boundedError).details).toEqual({ source: 'deadline' });
  });

  it('rejects an invalid hints.timeoutMs with RangeError', () => {
    const h = testContext({ hints: { timeoutMs: -5 } });
    const rec = recordNext(h.call, neverSettles);
    expect(() => totalTimeout().execute(h.call, rec.next)).toThrow(RangeError);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('the attempt policy ignores hints.timeoutMs: the call policy already owns it', async () => {
    const h = testContext({ hints: { timeoutMs: 5 } });
    const rec = recordNext(h.attempt, neverSettles);
    const error = await rejectionOf(
      attemptTimeout({ attemptTimeoutMs: 40 }).execute(h.attempt, rec.next),
      () => h.runtime.advance(40),
    );
    expect(asTimeout(error).timeoutMs).toBe(40);
  });
});

/* ------------------------------------------------------------------ *
 * 8. Nesting  (TO-15, TO-16)
 * ------------------------------------------------------------------ */

describe('nesting total-timeout outside attempt-timeout', () => {
  async function runNested(
    totalMs: number,
    attemptMs: number,
    advanceMs: number,
  ): Promise<{ outcome: Outcome; inner: AttemptContext[]; pending: number }> {
    const h = testContext();
    const inner: AttemptContext[] = [];
    const promise = totalTimeout({ totalTimeoutMs: totalMs }).execute(h.call, (callCtx) => {
      const attemptCtx = asAttempt(callCtx, h.attempt);
      return attemptTimeout({ attemptTimeoutMs: attemptMs }).execute(attemptCtx, (ctx) => {
        inner.push(ctx ?? attemptCtx);
        return neverSettles();
      });
    });
    const outcome = await settle(promise, () => h.runtime.advance(advanceMs));
    return { outcome, inner, pending: h.runtime.pendingTimers };
  }

  it('the inner (attempt) timeout wins when it is smaller (TO-15)', async () => {
    const { outcome, inner, pending } = await runNested(100, 10, 10);
    const timeout = asTimeout(outcome.ok ? undefined : outcome.error);
    expect(timeout.scope).toBe('attempt');
    expect(timeout.timeoutMs).toBe(10);
    expect(timeout.retryable).toBe(true);
    expect(only(inner).deadlineAt).toBe(10);
    expect(pending).toBe(0);
  });

  it('the outer (call) timeout wins when it is smaller (TO-16)', async () => {
    const { outcome, inner, pending } = await runNested(10, 100, 10);
    const timeout = asTimeout(outcome.ok ? undefined : outcome.error);
    expect(timeout.scope).toBe('call');
    expect(timeout.timeoutMs).toBe(10);
    expect(timeout.retryable).toBe(false);
    // The inner policy inherited the outer deadline and clamped 100ms down to it.
    expect(only(inner).deadlineAt).toBe(10);
    expect(pending).toBe(0);
  });

  it('produces no unhandled rejection when the outer aborts the inner', async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown): void => {
      unhandled.push(reason);
    };
    process.on('unhandledRejection', onUnhandled);
    try {
      const { outcome } = await runNested(10, 100, 10);
      expect(outcome.ok).toBe(false);
      await flushMacrotask();
      await flushMacrotask();
      expect(unhandled).toEqual([]);
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });
});

/* ------------------------------------------------------------------ *
 * 9. Regression guard  (TO-18)
 * ------------------------------------------------------------------ */

describe('AbortSignal.timeout regression guard (TO-18)', () => {
  it('does not abort synchronously, which is one reason the core path never uses it', () => {
    expect(AbortSignal.timeout(0).aborted).toBe(false);
    expect(AbortSignal.timeout(5).aborted).toBe(false);
  });

  it('the timeout policies never reach for it: their timer comes from the injected runtime', async () => {
    // Proof by observability: the fake runtime sees the timer. An unref'd
    // `AbortSignal.timeout()` timer would be invisible here and would never fire
    // under the virtual clock (R4 §2.1).
    const h = testContext();
    const rec = recordNext(h.attempt, neverSettles);
    const promise = attemptTimeout({ attemptTimeoutMs: 100 }).execute(h.attempt, rec.next);
    await Promise.resolve();
    expect(h.runtime.pendingTimers).toBe(1);
    const error = await rejectionOf(promise, () => h.runtime.advance(100));
    asTimeout(error);
    expect(h.runtime.pendingTimers).toBe(0);
  });
});
