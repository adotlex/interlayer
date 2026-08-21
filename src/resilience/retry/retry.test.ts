import { describe, expect, it } from 'vitest';
import { type TestContextHandle, testContext } from '../../../test/support/context.ts';
import type { FakeRuntime } from '../../../test/support/fake-clock.ts';
import { constantRandom, scriptedRandom } from '../../../test/support/seeded-random.ts';
import {
  CancelledError,
  CircuitOpenError,
  hasCode,
  ProviderError,
  RateLimitedError,
  type RetryExhaustedError,
  TimeoutError,
  TransportError,
  ValidationError,
} from '../../core/errors.ts';
import type { Policy, PolicyFactory, RetryOptions } from '../../core/policy.ts';
import type { AttemptContext, Next } from '../../core/types.ts';
import { isTransient, retryFailures, retryPolicy } from './retry.ts';

/* ------------------------------------------------------------------ *
 * Harness. Everything runs on the virtual clock: no test sleeps.
 * ------------------------------------------------------------------ */

type Outcome =
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly error: unknown };

type Step =
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly error: unknown };

const ok = (value: unknown = 'result'): Step => ({ ok: true, value });
const err = (error: unknown): Step => ({ ok: false, error });

interface Script {
  /** The `next` the policy drives. */
  readonly next: Next<AttemptContext, unknown>;
  /** `ctx.attempt` seen on each invocation, in order. Length == invocation count. */
  readonly seen: number[];
}

/**
 * A scripted downstream. `steps` are consumed in order; `tail` repeats forever.
 * `next` mirrors `compose()`: the replacement context wins when supplied.
 */
function script(h: TestContextHandle, steps: readonly Step[], tail: Step): Script {
  const seen: number[] = [];
  const next: Next<AttemptContext, unknown> = (replacement?: AttemptContext) => {
    const step = steps[seen.length] ?? tail;
    seen.push((replacement ?? h.attempt).attempt);
    return step.ok ? Promise.resolve(step.value) : Promise.reject(step.error);
  };
  return { next, seen };
}

/** Drives the virtual clock until the policy settles. Never waits on real time. */
async function settle(promise: Promise<unknown>, rt: FakeRuntime, rounds = 200): Promise<Outcome> {
  let done = false;
  const captured = promise.then(
    (value): Outcome => {
      done = true;
      return { ok: true, value };
    },
    (error: unknown): Outcome => {
      done = true;
      return { ok: false, error };
    },
  );
  for (let i = 0; i < rounds && !done; i++) await rt.runAll();
  return captured;
}

function capture(promise: Promise<unknown>): Promise<Outcome> {
  return promise.then(
    (value): Outcome => ({ ok: true, value }),
    (error: unknown): Outcome => ({ ok: false, error }),
  );
}

/** Yields the microtask queue without touching the clock. */
async function flush(rounds = 25): Promise<void> {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

function run(
  policy: Policy<AttemptContext, unknown>,
  h: TestContextHandle,
  s: Script,
): Promise<Outcome> {
  return settle(policy.execute(h.attempt, s.next), h.runtime);
}

function syscallError(code: string): Error {
  const e = new Error(`socket failure: ${code}`);
  Object.defineProperty(e, 'code', { value: code, enumerable: true });
  return e;
}

function httpError(status: number): Error {
  const e = new Error(`HTTP ${status}`);
  Object.defineProperty(e, 'status', { value: status, enumerable: true });
  return e;
}

/* ------------------------------------------------------------------ *
 * Policy shape & construction
 * ------------------------------------------------------------------ */

describe('retryPolicy — shape', () => {
  it('is an attempt-scoped policy of kind "retry"', () => {
    const p = retryPolicy();
    expect(p.kind).toBe('retry');
    expect(p.scope).toBe('attempt');
    expect(p.name).toBe('retry(full, 3)');
  });

  it('describe() reports the resolved defaults from core DEFAULTS.retry', () => {
    expect(retryPolicy().describe?.()).toEqual({
      maxAttempts: 3,
      strategy: 'full',
      baseDelayMs: 100,
      factor: 2,
      maxDelayMs: 30_000,
      budgetMs: undefined,
      retryNonIdempotentOnConnectFailure: true,
      idempotent: true,
    });
  });

  it('satisfies the core PolicyFactory<RetryOptions> contract', () => {
    // Compile-time proof: the extra `idempotent` option does not break the
    // contract the orchestrator wires against.
    const asCoreFactory: PolicyFactory<RetryOptions> = retryPolicy;
    expect(asCoreFactory({ maxAttempts: 2 }).kind).toBe('retry');
  });

  it('RT-5/RT-6: rejects an invalid maxAttempts with RangeError AT CONSTRUCTION', () => {
    for (const maxAttempts of [0, -1, 1.5, Number.NaN]) {
      expect(() => retryPolicy({ maxAttempts })).toThrow(RangeError);
    }
  });

  it('validates the remaining numeric options at construction too', () => {
    expect(() => retryPolicy({ baseDelayMs: -1 })).toThrow(RangeError);
    expect(() => retryPolicy({ factor: 0 })).toThrow(RangeError);
    expect(() => retryPolicy({ maxDelayMs: Number.NaN })).toThrow(RangeError);
    expect(() => retryPolicy({ budgetMs: -5 })).toThrow(RangeError);
  });
});

/* ------------------------------------------------------------------ *
 * `maxAttempts` — the settled semantics (R4 §1.2)
 * ------------------------------------------------------------------ */

describe('maxAttempts is TOTAL attempts including the first (R4 §1.2)', () => {
  it('THE PIN — maxAttempts: 3 makes 3 invocations, 2 retries, 2 sleeps', async () => {
    const h = testContext({ random: constantRandom(0.5) });
    const s = script(h, [], err(new TransportError('down')));
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, s);

    // Not 4 (Polly's "retries in addition to"), not 2. THREE.
    expect(s.seen.length).toBe(3);
    expect(h.runtime.slept.length).toBe(2);
    expect(h.emitted('retry:scheduled')).toHaveLength(2);
    expect(outcome.ok).toBe(false);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('maxAttempts: 5 makes 5 invocations and 4 sleeps', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 5 }), h, s);
    expect(s.seen.length).toBe(5);
    expect(h.runtime.slept.length).toBe(4);
  });

  it('RT-3/RT-4: maxAttempts: 1 invokes once, sleeps zero times, throws the ORIGINAL error', async () => {
    const h = testContext();
    const boom = new TransportError('down');
    const s = script(h, [], err(boom));
    const outcome = await run(retryPolicy({ maxAttempts: 1 }), h, s);

    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
    expect(outcome).toEqual({ ok: false, error: boom });
    expect(hasCode(outcome.ok ? undefined : outcome.error, 'TRANSPORT')).toBe(true);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('RT-2: success on the FINAL attempt resolves normally with no error', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [err(new TransportError('a')), err(new TransportError('b'))], ok('third'));
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, s);

    expect(outcome).toEqual({ ok: true, value: 'third' });
    expect(s.seen.length).toBe(3);
    expect(h.runtime.slept.length).toBe(2);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('RT-7: success on the first attempt invokes once, sleeps zero times, creates no timer', async () => {
    const h = testContext();
    const s = script(h, [], ok('immediate'));
    const outcome = await run(retryPolicy(), h, s);

    expect(outcome).toEqual({ ok: true, value: 'immediate' });
    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Attempt numbering & context derivation
 * ------------------------------------------------------------------ */

describe('attempt context', () => {
  it('advances ctx.attempt via next(ctx′) without mutating the original context', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 4 }), h, s);

    expect(s.seen).toEqual([1, 2, 3, 4]);
    expect(h.attempt.attempt).toBe(1); // untouched
  });

  it('respects a non-1 incoming attempt number', async () => {
    const h = testContext({ attempt: 7, random: constantRandom(0) });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 3 }), h, s);
    expect(s.seen).toEqual([7, 8, 9]);
  });
});

/* ------------------------------------------------------------------ *
 * Delays — exact values, via the seeded PRNG
 * ------------------------------------------------------------------ */

describe('backoff wiring — exact delays, never ranges', () => {
  it('exponential without jitter sleeps exactly [100, 200, 400]', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 4, strategy: 'exponential' }), h, s);
    expect(h.runtime.slept).toEqual([100, 200, 400]);
  });

  it('DEFAULT full jitter is a pure function of (n, rand()) — scripted PRNG pins it', async () => {
    // exp = [100, 200]; rand = [0.5, 0.25] => [50, 50]
    const h = testContext({ random: scriptedRandom([0.5, 0.25]) });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 3 }), h, s);
    expect(h.runtime.slept).toEqual([50, 50]);
  });

  it('applies the cap BEFORE the jitter inside the loop', async () => {
    // exp(3) uncapped = 400; cap 250 => 250; halved => 125.
    // Cap-after-jitter would give min(250, 0.5 * 400) = 200.
    const h = testContext({ random: constantRandom(0.5) });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 4, maxDelayMs: 250 }), h, s);
    expect(h.runtime.slept).toEqual([50, 100, 125]);
  });

  it('fixed strategy sleeps baseDelayMs every time', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 4, strategy: 'fixed', baseDelayMs: 30 }), h, s);
    expect(h.runtime.slept).toEqual([30, 30, 30]);
  });

  it('advances the virtual clock by exactly the slept time', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 3, strategy: 'exponential' }), h, s);
    expect(h.runtime.now()).toBe(300);
  });
});

/* ------------------------------------------------------------------ *
 * `retry:scheduled`
 * ------------------------------------------------------------------ */

describe('retry:scheduled event', () => {
  it('emits once per sleep, naming the UPCOMING attempt and the exact delay', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 3, strategy: 'exponential' }), h, s);

    expect(h.emitted('retry:scheduled')).toEqual([
      {
        callId: h.call.callId,
        providerId: h.provider.id,
        attempt: 2,
        delayMs: 100,
        at: 0,
      },
      {
        callId: h.call.callId,
        providerId: h.provider.id,
        attempt: 3,
        delayMs: 200,
        at: 100,
      },
    ]);
  });

  it('emits nothing when the first attempt succeeds', async () => {
    const h = testContext();
    await run(retryPolicy(), h, script(h, [], ok()));
    expect(h.emitted('retry:scheduled')).toEqual([]);
  });
});

/* ------------------------------------------------------------------ *
 * Which error surfaces (R4 §1.6, adapted — see retry.ts header)
 * ------------------------------------------------------------------ */

describe('error surfaced when everything fails', () => {
  it('RT-8: aggregates >= 2 failures into RetryExhaustedError, last error as cause', async () => {
    const h = testContext({ random: constantRandom(0) });
    const first = new TransportError('first');
    const second = new TransportError('second');
    const third = new TransportError('third');
    const s = script(h, [err(first), err(second)], err(third));
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, s);

    // R4 §1.6: three failures aggregate. This is the behaviour the unit could
    // not express while `ErrorCode` had no `RETRY_EXHAUSTED` member; it threw
    // the last error unwrapped instead and said so in its header.
    const error = outcome.ok ? undefined : outcome.error;
    expect(hasCode(error, 'RETRY_EXHAUSTED')).toBe(true);
    const exhausted = error as RetryExhaustedError;
    expect(exhausted.attempts).toBe(3);
    expect(exhausted.errors).toEqual([first, second, third]);
    expect(exhausted.cause, 'Node prints the cause chain').toBe(third);
    expect(exhausted.providerId).toBe(h.provider.id);
    // Inherited from the last failure, so the fallback chain above still
    // advances to the next provider instead of stranding the call here.
    expect(exhausted.retryable).toBe(true);

    const record = retryFailures(h.call);
    expect(record?.attempts).toBe(3);
    expect(record?.errors).toEqual([first, second, third]);
    expect(record?.providerId).toBe(h.provider.id);
    expect(record?.predicateErrors).toEqual([]);
  });

  it('a SINGLE failure is still rethrown unwrapped, so `code` survives', async () => {
    const h = testContext();
    const only = new TransportError('only');
    const outcome = await run(retryPolicy({ maxAttempts: 1 }), h, script(h, [], err(only)));

    // `maxAttempts: 1` must behave exactly like no retry wrapper at all.
    expect(outcome).toEqual({ ok: false, error: only });
    expect(hasCode(outcome.ok ? undefined : outcome.error, 'TRANSPORT')).toBe(true);
  });

  it('a non-retryable error after one attempt is rethrown unwrapped too', async () => {
    const h = testContext();
    const fatal = new ValidationError('bad input', []);
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, script(h, [], err(fatal)));

    expect(outcome).toEqual({ ok: false, error: fatal });
    expect(retryFailures(h.call)?.attempts).toBe(1);
  });

  it('records nothing on ctx.state when the call succeeds', async () => {
    const h = testContext();
    await run(retryPolicy(), h, script(h, [], ok()));
    expect(retryFailures(h.call)).toBeUndefined();
  });

  it('RT-25: preserves non-Error throws verbatim', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [err('oops'), err(null)], err('last'));
    const outcome = await run(retryPolicy({ maxAttempts: 3, isRetryable: () => true }), h, s);

    const error = outcome.ok ? undefined : outcome.error;
    expect(hasCode(error, 'RETRY_EXHAUSTED')).toBe(true);
    // Verbatim: a `null` throw stays `null`, a string stays a string.
    expect((error as RetryExhaustedError).errors).toEqual(['oops', null, 'last']);
    expect((error as RetryExhaustedError).cause).toBe('last');
    // A non-Interlayer last error cannot claim retryability, so it does not.
    expect((error as RetryExhaustedError).retryable).toBe(false);
    expect(retryFailures(h.call)?.errors).toEqual(['oops', null, 'last']);
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * The retryable predicate
 * ------------------------------------------------------------------ */

describe('retryable predicate', () => {
  it('RT-9: a predicate returning false on attempt 1 invokes once and propagates the bare error', async () => {
    const h = testContext();
    const boom = new TransportError('down');
    const s = script(h, [], err(boom));
    const outcome = await run(retryPolicy({ maxAttempts: 5, isRetryable: () => false }), h, s);

    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
    expect(outcome).toEqual({ ok: false, error: boom });
  });

  it('RT-10: a predicate returning false on attempt 2 of 5 invokes exactly twice', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [], err(new TransportError('down')));
    const policy = retryPolicy({ maxAttempts: 5, isRetryable: (_e, attempt) => attempt < 2 });
    await run(policy, h, s);

    expect(s.seen.length).toBe(2);
    expect(h.runtime.slept.length).toBe(1);
  });

  it('receives the 1-based retry index as its second argument', async () => {
    const h = testContext({ random: constantRandom(0) });
    const seenAttempts: number[] = [];
    const policy = retryPolicy({
      maxAttempts: 3,
      isRetryable: (_e, attempt) => {
        seenAttempts.push(attempt);
        return true;
      },
    });
    await run(policy, h, script(h, [], err(new TransportError('x'))));
    expect(seenAttempts).toEqual([1, 2]);
  });

  it('RT-31: a THROWING predicate never masks the original error', async () => {
    const h = testContext();
    const original = new TransportError('the real failure');
    const policy = retryPolicy({
      maxAttempts: 3,
      isRetryable: () => {
        throw new Error('buggy predicate');
      },
    });
    const s = script(h, [], err(original));
    const outcome = await run(policy, h, s);

    expect(outcome).toEqual({ ok: false, error: original });
    expect(s.seen.length).toBe(1);
    expect(retryFailures(h.call)?.predicateErrors).toHaveLength(1);
  });
});

describe('isTransient — the default predicate (R4 §1.3)', () => {
  it('RT-29: retries every listed HTTP status', () => {
    for (const status of [408, 429, 500, 502, 503, 504]) {
      expect(isTransient(httpError(status), 1)).toBe(true);
    }
  });

  it('RT-29: retries every listed Node network error code', () => {
    const codes = [
      'ECONNRESET',
      'ECONNREFUSED',
      'ETIMEDOUT',
      'EPIPE',
      'EAI_AGAIN',
      'ENOTFOUND',
      'EHOSTUNREACH',
      'ENETUNREACH',
      'EBUSY',
    ];
    for (const code of codes) expect(isTransient(syscallError(code), 1)).toBe(true);
  });

  it('RT-30: does NOT retry client-error statuses or a plain TypeError', () => {
    for (const status of [400, 401, 403, 404, 422]) {
      expect(isTransient(httpError(status), 1)).toBe(false);
    }
    expect(isTransient(new TypeError('bug in our own code'), 1)).toBe(false);
    // R4 §1.3's list is exact and deliberately narrower than core's
    // `isRetryableStatus` (which also admits 425) for RAW, non-Interlayer errors.
    expect(isTransient(httpError(425), 1)).toBe(false);
  });

  it('never retries a cancellation, however it is spelled', () => {
    expect(isTransient(new CancelledError('stop'), 1)).toBe(false);
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    expect(isTransient(abort, 1)).toBe(false);
  });

  it('RT-20: never retries CircuitOpenError, even though core marks it retryable', () => {
    const open = new CircuitOpenError('openai', 0, 10_000, 0);
    expect(open.retryable).toBe(true); // retryable ELSEWHERE, not here
    expect(isTransient(open, 1)).toBe(false);
  });

  it('honours the `retryable` flag on an Interlayer error', () => {
    expect(isTransient(new TransportError('down'), 1)).toBe(true);
    expect(isTransient(new ValidationError('bad input', []), 1)).toBe(false);
    expect(isTransient(new ProviderError('server', {}, 503), 1)).toBe(true);
    expect(isTransient(new ProviderError('client', {}, 400), 1)).toBe(false);
    expect(isTransient(new TimeoutError(1_000, 'attempt'), 1)).toBe(true);
    expect(isTransient(new TimeoutError(1_000, 'call'), 1)).toBe(false);
  });

  it('retries a foreign error merely NAMED TimeoutError', () => {
    const foreign = new Error('timed out');
    foreign.name = 'TimeoutError';
    expect(isTransient(foreign, 1)).toBe(true);
  });
});

describe('RT-20 end to end', () => {
  it('does not retry a CircuitOpenError through the policy', async () => {
    const h = testContext();
    const open = new CircuitOpenError('openai', 0, 10_000, 0);
    const s = script(h, [], err(open));
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, s);

    expect(s.seen.length).toBe(1);
    expect(outcome).toEqual({ ok: false, error: open });
    expect(h.runtime.slept).toEqual([]);
  });
});

/* ------------------------------------------------------------------ *
 * Cancellation
 * ------------------------------------------------------------------ */

describe('cancellation', () => {
  it('RT-17: aborting BEFORE the first attempt yields CancelledError and ZERO invocations', async () => {
    const h = testContext();
    h.abort();
    const s = script(h, [], ok());
    const outcome = await run(retryPolicy(), h, s);

    expect(s.seen.length).toBe(0);
    expect(outcome.ok).toBe(false);
    expect(hasCode(outcome.ok ? undefined : outcome.error, 'CANCELLED')).toBe(true);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('surfaces an Interlayer abort reason unchanged rather than re-wrapping it', async () => {
    const h = testContext();
    const reason = new CancelledError('caller went away');
    h.abort(reason);
    const outcome = await run(retryPolicy(), h, script(h, [], ok()));
    expect(outcome).toEqual({ ok: false, error: reason });
  });

  it('RT-18: aborting DURING the backoff sleep cancels, clears the timer, and stops', async () => {
    const h = testContext();
    const policy = retryPolicy({ maxAttempts: 3, strategy: 'fixed', baseDelayMs: 30_000 });
    const s = script(h, [], err(new TransportError('down')));
    const captured = capture(policy.execute(h.attempt, s.next));

    await flush();
    expect(s.seen.length).toBe(1);
    expect(h.runtime.pendingTimers).toBe(1); // the backoff sleep is in flight

    h.abort();
    const outcome = await captured;

    expect(hasCode(outcome.ok ? undefined : outcome.error, 'CANCELLED')).toBe(true);
    expect(s.seen.length).toBe(1); // RT-18: never invoked again
    expect(h.runtime.pendingTimers).toBe(0); // RT-32: timer cleared, no leak
    expect(h.runtime.now()).toBe(0); // no real or virtual time was burned
  });

  it('RT-19: a CancelledError from inside the operation is rethrown unwrapped and never retried', async () => {
    const h = testContext();
    const inner = new CancelledError('inner stop');
    const s = script(h, [], err(inner));
    const outcome = await run(retryPolicy({ maxAttempts: 3 }), h, s);

    expect(outcome).toEqual({ ok: false, error: inner });
    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
    // Never aggregated into the failure record either.
    expect(retryFailures(h.call)).toBeUndefined();
  });
});

/* ------------------------------------------------------------------ *
 * Budget (R4 §1.4)
 * ------------------------------------------------------------------ */

describe('budgetMs — the cooperative ceiling', () => {
  it('RT-22: refuses to sleep past a budget it cannot meet, without sleeping at all', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    const policy = retryPolicy({
      maxAttempts: 5,
      strategy: 'exponential',
      budgetMs: 50, // the first 100ms sleep already overruns it
    });
    await run(policy, h, s);

    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
    expect(h.runtime.now()).toBe(0);
  });

  it('RT-21: stops early, well before maxAttempts, once the budget is spent', async () => {
    const h = testContext();
    const s = script(h, [], err(new TransportError('down')));
    const policy = retryPolicy({ maxAttempts: 5, strategy: 'exponential', budgetMs: 250 });
    await run(policy, h, s);

    // sleep 100 (remaining 250) -> elapsed 100; next delay 200 >= remaining 150 -> stop.
    expect(s.seen.length).toBe(2);
    expect(h.runtime.slept).toEqual([100]);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('derives the budget from ctx.deadlineAt when none is configured (R4 §1.4)', async () => {
    const h = testContext({ deadlineAt: 150 });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 5, strategy: 'exponential' }), h, s);

    expect(s.seen.length).toBe(2);
    expect(h.runtime.slept).toEqual([100]);
  });

  it('an explicit budgetMs overrides the derived deadline budget', async () => {
    const h = testContext({ deadlineAt: 10 });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 3, strategy: 'exponential', budgetMs: 10_000 }), h, s);
    expect(s.seen.length).toBe(3);
  });

  it('stops immediately when the deadline has already passed', async () => {
    const h = testContext({ deadlineAt: 0 });
    const s = script(h, [], err(new TransportError('down')));
    await run(retryPolicy({ maxAttempts: 5, strategy: 'exponential' }), h, s);
    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
  });
});

/* ------------------------------------------------------------------ *
 * Retry-After (R4 §1.3)
 * ------------------------------------------------------------------ */

describe('Retry-After honouring', () => {
  it('RT-23: a retryAfterMs of 5000 beats a computed delay of 100', async () => {
    const h = testContext();
    const s = script(h, [err(new RateLimitedError('slow down', 5_000))], ok('done'));
    const outcome = await run(retryPolicy({ maxAttempts: 3, strategy: 'exponential' }), h, s);

    expect(h.runtime.slept).toEqual([5_000]);
    expect(outcome).toEqual({ ok: true, value: 'done' });
  });

  it('RT-24: a retryAfterMs above maxDelayMs is still capped', async () => {
    const h = testContext();
    const s = script(h, [err(new RateLimitedError('slow down', 600_000))], ok('done'));
    const policy = retryPolicy({ maxAttempts: 3, strategy: 'exponential', maxDelayMs: 30_000 });
    await run(policy, h, s);
    expect(h.runtime.slept).toEqual([30_000]);
  });

  it('a retryAfterMs BELOW the computed delay does not shorten the backoff', async () => {
    const h = testContext();
    const s = script(h, [err(new RateLimitedError('slow down', 5))], ok('done'));
    const policy = retryPolicy({ maxAttempts: 3, strategy: 'fixed', baseDelayMs: 400 });
    await run(policy, h, s);
    expect(h.runtime.slept).toEqual([400]);
  });
});

/* ------------------------------------------------------------------ *
 * Idempotency (R4 §1.3 / RT-26..28)
 * ------------------------------------------------------------------ */

describe('non-idempotent operations', () => {
  it('RT-26: a non-idempotent operation is invoked exactly once on a generic failure', async () => {
    const h = testContext();
    const s = script(h, [], err(httpError(503)));
    await run(retryPolicy({ maxAttempts: 3, idempotent: false }), h, s);

    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
  });

  it('RT-27: a non-idempotent operation IS retried on a connect-level failure', async () => {
    const h = testContext({ random: constantRandom(0) });
    const s = script(h, [], err(syscallError('ECONNREFUSED')));
    await run(retryPolicy({ maxAttempts: 3, idempotent: false }), h, s);

    expect(s.seen.length).toBe(3);
  });

  it('the connect-failure exemption can be switched off', async () => {
    const h = testContext();
    const s = script(h, [], err(syscallError('ECONNREFUSED')));
    const policy = retryPolicy({
      maxAttempts: 3,
      idempotent: false,
      retryNonIdempotentOnConnectFailure: false,
    });
    await run(policy, h, s);
    expect(s.seen.length).toBe(1);
  });

  it('RT-28: a non-idempotent operation is NEVER retried on a timeout', async () => {
    const h = testContext();
    const s = script(h, [], err(new TimeoutError(1_000, 'attempt')));
    await run(retryPolicy({ maxAttempts: 3, idempotent: false }), h, s);

    expect(s.seen.length).toBe(1);
    expect(h.runtime.slept).toEqual([]);
  });

  it('ECONNRESET is retryable in general but is NOT a connect-level exemption', async () => {
    const h = testContext();
    const s = script(h, [], err(syscallError('ECONNRESET')));
    await run(retryPolicy({ maxAttempts: 3, idempotent: false }), h, s);
    expect(s.seen.length).toBe(1);
  });
});

/* ------------------------------------------------------------------ *
 * Leak guards (RT-32)
 * ------------------------------------------------------------------ */

describe('RT-32: zero outstanding timers after every terminal state', () => {
  it('after success', async () => {
    const h = testContext({ random: constantRandom(0.25) });
    await run(retryPolicy({ maxAttempts: 3 }), h, script(h, [err(new TransportError('a'))], ok()));
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('after exhaustion', async () => {
    const h = testContext({ random: constantRandom(0.25) });
    await run(retryPolicy({ maxAttempts: 3 }), h, script(h, [], err(new TransportError('a'))));
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('after a predicate short-circuit', async () => {
    const h = testContext();
    await run(
      retryPolicy({ maxAttempts: 3 }),
      h,
      script(h, [], err(new ValidationError('no', []))),
    );
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('after a budget short-circuit', async () => {
    const h = testContext();
    const policy = retryPolicy({ maxAttempts: 5, strategy: 'exponential', budgetMs: 250 });
    await run(policy, h, script(h, [], err(new TransportError('a'))));
    expect(h.runtime.pendingTimers).toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * Re-entrancy against the real composer
 * ------------------------------------------------------------------ */

describe('re-entrancy', () => {
  it('calls next() sequentially, so compose()’s concurrent-re-entry guard never fires', async () => {
    const h = testContext({ random: constantRandom(0) });
    let inFlight = 0;
    let maxInFlight = 0;
    const next: Next<AttemptContext, unknown> = async () => {
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await h.runtime.sleep(0);
      inFlight--;
      throw new TransportError('down');
    };
    await settle(retryPolicy({ maxAttempts: 3 }).execute(h.attempt, next), h.runtime);
    expect(maxInFlight).toBe(1);
  });
});
