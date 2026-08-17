/**
 * The state machine, exercised as a TABLE of transitions.
 *
 * Everything here is synchronous and pure — no clock, no timers, no promises.
 * `now` is a column in the table. That is the point of splitting the reducer out
 * of the policy: a breaker's interesting behaviour is time- and
 * concurrency-dependent, and this is the only way to get at it without a
 * scheduler in the way.
 */

import { describe, expect, it } from 'vitest';
import { ConfigError } from '../../core/errors.ts';
import type { BreakerOptions, ResolvedBreakerOptions } from '../../core/policy.ts';
import type { BreakerState } from '../../core/types.ts';
import {
  type BreakerData,
  type BreakerEvent,
  canAdmit,
  createBreakerReducer,
  currentState,
  halfOpenAt,
  initialBreakerData,
  reduceBreaker,
  resolveBreakerOptions,
  windowTotals,
} from './state.ts';

interface Step {
  /** `now` for this step. Sticky: omit to reuse the previous step's clock. */
  readonly at?: number;
  readonly event: BreakerEvent;
  /** The STORED state after the step — lazy transitions need an explicit `tick`. */
  readonly state: BreakerState;
  readonly note?: string;
}

/** Runs a table of transitions, asserting the stored state after every step. */
function walk(options: BreakerOptions, steps: readonly Step[]): BreakerData {
  const resolved = resolveBreakerOptions(options);
  const reduce = createBreakerReducer(resolved);
  let data = initialBreakerData();
  let now = 0;
  for (const [i, step] of steps.entries()) {
    now = step.at ?? now;
    data = reduce(data, step.event, now);
    const label = `step ${i}: ${step.event.type}@${now}${step.note === undefined ? '' : ` (${step.note})`}`;
    expect(data.state, label).toBe(step.state);
  }
  return data;
}

const outcomes = (types: readonly ('success' | 'failure')[], state: BreakerState): Step[] =>
  types.map((type) => ({ event: { type }, state }));

describe('breaker reducer — closed', () => {
  it('CB-1: a fresh breaker is closed with an empty window and admits calls', () => {
    const data = initialBreakerData();
    const resolved = resolveBreakerOptions();
    expect(data.state).toBe('closed');
    expect(data.buckets).toEqual([]);
    expect(data.generation).toBe(0);
    expect(canAdmit(data, 0, resolved)).toBe(true);
    expect(windowTotals(data, 0, resolved)).toEqual({ successes: 0, failures: 0, total: 0 });
  });

  it('CB-2/CB-3: 9 failures stay closed under minimumThroughput 10; the 10th opens', () => {
    const data = walk({ minimumThroughput: 10, failureRatio: 0.5 }, [
      ...outcomes(Array<'failure'>(9).fill('failure'), 'closed'),
      { event: { type: 'failure' }, state: 'open', note: 'total reaches minimumThroughput' },
    ]);
    expect(data.openedAt).toBe(0);
  });

  it('CB-4: a ratio exactly equal to failureRatio trips (>=, not >)', () => {
    walk({ minimumThroughput: 10, failureRatio: 0.5 }, [
      ...outcomes(Array<'success'>(5).fill('success'), 'closed'),
      ...outcomes(Array<'failure'>(4).fill('failure'), 'closed'),
      { event: { type: 'failure' }, state: 'open', note: '5/10 === 0.5' },
    ]);
  });

  it('CB-5: 4 failures in 10 calls (0.4) stays closed', () => {
    const data = walk({ minimumThroughput: 10, failureRatio: 0.5 }, [
      ...outcomes(Array<'failure'>(4).fill('failure'), 'closed'),
      ...outcomes(Array<'success'>(6).fill('success'), 'closed'),
    ]);
    expect(windowTotals(data, 0, resolveBreakerOptions())).toEqual({
      successes: 6,
      failures: 4,
      total: 10,
    });
  });

  /* ------------------------------------------------------------------ *
   * CB-6 — the regression R4 found by running its own draft. A breaker
   * that evaluates the trip condition only in the failure path NEVER
   * TRIPS at a true 50 % failure rate, silently. Both shapes are pinned.
   * ------------------------------------------------------------------ */

  it('CB-6: alternating success/failure at the threshold ratio trips ON THE SUCCESS', () => {
    const steps: Step[] = [];
    for (let i = 0; i < 10; i++) {
      steps.push({
        event: { type: i % 2 === 0 ? 'failure' : 'success' },
        state: i === 9 ? 'open' : 'closed',
      });
    }
    // The 10th outcome — the one that trips — is a SUCCESS. A failure-only
    // evaluation leaves this breaker closed forever at a 50 % failure rate.
    expect(steps[9]?.event.type).toBe('success');
    const data = walk({ minimumThroughput: 10, failureRatio: 0.5 }, steps);
    expect(data.state).toBe('open');
  });

  it('CB-6: 5 failures then 5 successes trips on the last success (R4 §3.4 verbatim)', () => {
    walk({ minimumThroughput: 10, failureRatio: 0.5 }, [
      // `total` is still 5 when the last FAILURE arrives, so a failure-only
      // check never re-runs and the breaker never trips.
      ...outcomes(Array<'failure'>(5).fill('failure'), 'closed'),
      ...outcomes(Array<'success'>(4).fill('success'), 'closed'),
      { event: { type: 'success' }, state: 'open', note: 'total 10, failures 5' },
    ]);
  });

  it('a single failure never trips a default breaker (the min-throughput guard)', () => {
    walk({}, [{ event: { type: 'failure' }, state: 'closed', note: '1/1 = 100 %' }]);
  });
});

describe('breaker reducer — rolling window', () => {
  it('CB-18: failures separated by more than windowMs do not accumulate', () => {
    const options: BreakerOptions = { minimumThroughput: 2, windowMs: 30_000, bucketMs: 1_000 };
    const data = walk(options, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 40_000, event: { type: 'failure' }, state: 'closed', note: 'the first expired' },
    ]);
    expect(windowTotals(data, 40_000, resolveBreakerOptions(options)).total).toBe(1);
  });

  it('two failures INSIDE the window do trip a minimumThroughput 2 breaker', () => {
    walk({ minimumThroughput: 2, windowMs: 30_000, bucketMs: 1_000 }, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 29_000, event: { type: 'failure' }, state: 'open' },
    ]);
  });

  it('CB-19: bucket count stays bounded at ceil(windowMs / bucketMs) under load', () => {
    const options: BreakerOptions = { windowMs: 30_000, bucketMs: 1_000, failureRatio: 1 };
    const steps: Step[] = [];
    for (let i = 0; i < 100; i++) {
      steps.push({ at: i * 1_000, event: { type: 'success' }, state: 'closed' });
    }
    const data = walk(options, steps);
    expect(data.buckets.length).toBeLessThanOrEqual(30);
    expect(data.buckets.length).toBe(30);
  });

  it('outcomes inside one bucketMs slice share a bucket', () => {
    const data = walk({ bucketMs: 1_000 }, [
      { at: 100, event: { type: 'success' }, state: 'closed' },
      { at: 900, event: { type: 'failure' }, state: 'closed' },
    ]);
    expect(data.buckets).toEqual([{ t: 0, s: 1, f: 1 }]);
  });

  it('a backwards clock jump folds into the newest bucket instead of unsorting it', () => {
    const options: BreakerOptions = { windowMs: 30_000, bucketMs: 1_000 };
    const data = walk(options, [
      { at: 5_000, event: { type: 'failure' }, state: 'closed' },
      { at: 1_000, event: { type: 'failure' }, state: 'closed', note: 'clock went backwards' },
    ]);
    expect(data.buckets).toEqual([{ t: 5_000, s: 0, f: 2 }]);
    // The guard's real job: the window stays sorted, so pruning still drops the
    // oldest end and an expired bucket can never be resurrected.
    expect(windowTotals(data, 5_000, resolveBreakerOptions(options)).failures).toBe(2);
  });
});

describe('breaker reducer — open and the lazy half-open transition', () => {
  const opts: BreakerOptions = { minimumThroughput: 2, resetMs: 10_000 };

  it('CB-9/CB-10: half-open at exactly resetMs, not one tick earlier', () => {
    walk(opts, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 0, event: { type: 'failure' }, state: 'open' },
      { at: 9_999, event: { type: 'tick' }, state: 'open', note: 'resetMs - 1' },
      { at: 10_000, event: { type: 'tick' }, state: 'half-open', note: '>=, not >' },
    ]);
  });

  it('CB-26: nothing moves without an event — the transition is lazy, not timed', () => {
    const resolved = resolveBreakerOptions(opts);
    const data = walk(opts, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 0, event: { type: 'failure' }, state: 'open' },
    ]);
    // The stored state is still 'open' an hour later: no timer exists to change
    // it. Only a call (a `tick`) can, and `currentState` is how you ask.
    expect(data.state).toBe('open');
    expect(currentState(data, 3_600_000, resolved)).toBe('half-open');
    expect(data.state).toBe('open');
  });

  it('an open breaker admits nothing until resetMs has elapsed', () => {
    const resolved = resolveBreakerOptions(opts);
    const data = walk(opts, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 0, event: { type: 'failure' }, state: 'open' },
    ]);
    expect(canAdmit(data, 9_999, resolved)).toBe(false);
    expect(canAdmit(data, 10_000, resolved)).toBe(true);
    expect(halfOpenAt(data, resolved)).toBe(10_000);
  });

  it('the window is cleared on open, so CircuitOpenError-era data cannot linger', () => {
    const data = walk(opts, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 0, event: { type: 'failure' }, state: 'open' },
    ]);
    expect(data.buckets).toEqual([]);
  });
});

describe('breaker reducer — half-open', () => {
  const opts: BreakerOptions = { minimumThroughput: 2, resetMs: 10_000 };
  const toHalfOpen: Step[] = [
    { at: 0, event: { type: 'failure' }, state: 'closed' },
    { at: 0, event: { type: 'failure' }, state: 'open' },
    { at: 10_000, event: { type: 'tick' }, state: 'half-open' },
  ];

  it('CB-13: one successful trial closes the breaker at halfOpenSuccessesToClose 1', () => {
    walk(opts, [
      ...toHalfOpen,
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'closed' },
    ]);
  });

  it('CB-14: with halfOpenSuccessesToClose 2 the first success keeps it half-open', () => {
    const data = walk({ ...opts, halfOpenSuccessesToClose: 2 }, [
      ...toHalfOpen,
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'half-open' },
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'closed' },
    ]);
    expect(data.halfOpenSuccesses).toBe(0);
    expect(data.halfOpenInFlight).toBe(0);
  });

  it('CB-15: any single failed trial re-opens, whatever the prior successes', () => {
    walk({ ...opts, halfOpenSuccessesToClose: 3 }, [
      ...toHalfOpen,
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'half-open' },
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'half-open' },
      { event: { type: 'admit' }, state: 'half-open' },
      { at: 12_000, event: { type: 'failure' }, state: 'open', note: 'no "3 strikes"' },
    ]);
  });

  it('CB-16: re-opening restarts the FULL resetMs cooldown from the new openedAt', () => {
    const data = walk(opts, [
      ...toHalfOpen,
      { event: { type: 'admit' }, state: 'half-open' },
      { at: 15_000, event: { type: 'failure' }, state: 'open' },
      { at: 24_999, event: { type: 'tick' }, state: 'open', note: 'not the remainder' },
      { at: 25_000, event: { type: 'tick' }, state: 'half-open' },
    ]);
    expect(data.state).toBe('half-open');
  });

  it('CB-17: a freshly closed breaker starts with an empty window', () => {
    const resolved = resolveBreakerOptions(opts);
    const data = walk(opts, [
      ...toHalfOpen,
      { event: { type: 'admit' }, state: 'half-open' },
      { event: { type: 'success' }, state: 'closed' },
      { at: 11_000, event: { type: 'failure' }, state: 'closed', note: 'not a re-trip' },
    ]);
    expect(windowTotals(data, 11_000, resolved)).toEqual({
      successes: 0,
      failures: 1,
      total: 1,
    });
  });

  it('admission is capped at halfOpenMaxConcurrent and the cap is checkable', () => {
    const resolved = resolveBreakerOptions({ ...opts, halfOpenMaxConcurrent: 2 });
    const reduce = createBreakerReducer(resolved);
    let data = initialBreakerData();
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'tick' }, 10_000);

    expect(canAdmit(data, 10_000, resolved)).toBe(true);
    data = reduce(data, { type: 'admit' }, 10_000);
    expect(canAdmit(data, 10_000, resolved)).toBe(true);
    data = reduce(data, { type: 'admit' }, 10_000);
    expect(data.halfOpenInFlight).toBe(2);

    // Third trial: rejected, and `admit` is a no-op so the counter cannot drift
    // even if a caller dispatched it anyway.
    expect(canAdmit(data, 10_000, resolved)).toBe(false);
    expect(reduce(data, { type: 'admit' }, 10_000).halfOpenInFlight).toBe(2);
  });
});

describe('breaker reducer — generation counter', () => {
  const opts: BreakerOptions = { minimumThroughput: 2, resetMs: 10_000 };

  it('every transition bumps the generation', () => {
    const resolved = resolveBreakerOptions(opts);
    const reduce = createBreakerReducer(resolved);
    let data = initialBreakerData();
    expect(data.generation).toBe(0);
    data = reduce(data, { type: 'failure' }, 0);
    expect(data.generation, 'a recorded outcome alone is not a transition').toBe(0);
    data = reduce(data, { type: 'failure' }, 0);
    expect(data.generation, 'closed -> open').toBe(1);
    data = reduce(data, { type: 'tick' }, 10_000);
    expect(data.generation, 'open -> half-open').toBe(2);
    data = reduce(data, { type: 'admit' }, 10_000);
    data = reduce(data, { type: 'success' }, 10_000);
    expect(data.generation, 'half-open -> closed').toBe(3);
  });

  it('CB-22: a stale trial cannot close a circuit that cycled back into half-open', () => {
    // THE race the generation counter exists for (R4 §3.7). The `'open'` state
    // discards outcomes on its own, so a stale settlement is only dangerous once
    // the breaker has cycled back round to `'half-open'` — where a bare success
    // would close it, while a DIFFERENT probe is the one actually being trialled.
    const resolved = resolveBreakerOptions(opts);
    const reduce = createBreakerReducer(resolved);
    let data = initialBreakerData();
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'tick' }, 10_000);
    data = reduce(data, { type: 'admit' }, 10_000);
    const stale = data.generation; // trial A starts here and is slow

    data = reduce(data, { type: 'trip' }, 11_000); // re-opened by something else
    data = reduce(data, { type: 'tick' }, 21_000); // cooled down again
    expect(data.state, 'a NEW half-open generation is trialling').toBe('half-open');
    data = reduce(data, { type: 'admit' }, 21_000);

    const after = reduce(data, { type: 'success', generation: stale }, 22_000);
    expect(after.state, 'trial A must not close a circuit it no longer belongs to').toBe(
      'half-open',
    );
    expect(after).toEqual(data);
  });

  it('CB-22: a settlement from a superseded generation is discarded', () => {
    const resolved = resolveBreakerOptions(opts);
    const reduce = createBreakerReducer(resolved);
    let data = initialBreakerData();
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'failure' }, 0);
    data = reduce(data, { type: 'tick' }, 10_000);
    data = reduce(data, { type: 'admit' }, 10_000);

    // A trial starts here.
    const stale = data.generation;
    // The breaker moves underneath it (another failure re-opens it).
    data = reduce(data, { type: 'trip' }, 11_000);
    expect(data.state).toBe('open');
    expect(data.generation).not.toBe(stale);

    // The trial finally succeeds. Without the generation guard this would close
    // a circuit that has already re-opened.
    const after = reduce(data, { type: 'success', generation: stale }, 12_000);
    expect(after).toEqual(data);
    expect(after.state).toBe('open');
  });

  it('an outcome recorded while closed survives if no transition intervened', () => {
    const resolved = resolveBreakerOptions({ minimumThroughput: 10 });
    const reduce = createBreakerReducer(resolved);
    let data = initialBreakerData();
    const gen = data.generation;
    data = reduce(data, { type: 'failure', generation: gen }, 0);
    expect(windowTotals(data, 0, resolved).failures).toBe(1);
  });
});

describe('breaker reducer — consecutive mode', () => {
  it('trips after minimumThroughput consecutive failures', () => {
    walk({ mode: 'consecutive', minimumThroughput: 3 }, [
      { event: { type: 'failure' }, state: 'closed' },
      { event: { type: 'failure' }, state: 'closed' },
      { event: { type: 'failure' }, state: 'open' },
    ]);
  });

  it('a single success resets the consecutive run', () => {
    const data = walk({ mode: 'consecutive', minimumThroughput: 3 }, [
      { event: { type: 'failure' }, state: 'closed' },
      { event: { type: 'failure' }, state: 'closed' },
      { event: { type: 'success' }, state: 'closed' },
      { event: { type: 'failure' }, state: 'closed' },
      { event: { type: 'failure' }, state: 'closed' },
    ]);
    expect(data.consecutiveFailures).toBe(2);
  });
});

describe('breaker reducer — forced transitions', () => {
  it('trip opens at the supplied now; reset closes and clears', () => {
    const data = walk({ minimumThroughput: 2 }, [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 500, event: { type: 'trip' }, state: 'open' },
    ]);
    expect(data.openedAt).toBe(500);
    expect(data.buckets).toEqual([]);
    const resolved = resolveBreakerOptions({ minimumThroughput: 2 });
    expect(reduceBreaker(data, { type: 'reset' }, 600, resolved).state).toBe('closed');
  });
});

describe('option resolution', () => {
  it('fills every default from DEFAULTS.circuitBreaker (R4 §0.1)', () => {
    expect(resolveBreakerOptions()).toEqual({
      mode: 'ratio',
      failureRatio: 0.5,
      minimumThroughput: 10,
      windowMs: 30_000,
      bucketMs: 1_000,
      resetMs: 10_000,
      halfOpenMaxConcurrent: 1,
      halfOpenSuccessesToClose: 1,
    });
  });

  it('CB-27: minimumThroughput 1 is rejected by the builder (floor of 2)', () => {
    expect(() => resolveBreakerOptions({ minimumThroughput: 1 })).toThrow(ConfigError);
    try {
      resolveBreakerOptions({ minimumThroughput: 1 });
      expect.unreachable('should have thrown');
    } catch (error) {
      expect(error).toBeInstanceOf(ConfigError);
      expect((error as ConfigError).code).toBe('CONFIG');
      expect((error as ConfigError).issues[0]?.path).toEqual(['breaker', 'minimumThroughput']);
    }
  });

  it('CB-27: the machine itself does NOT validate — that is a config-layer job', () => {
    // R4 confirmed this during validation: constructing the breaker directly
    // with minimumThroughput 1 must not throw. Only the builder validates.
    const unchecked: ResolvedBreakerOptions = {
      mode: 'ratio',
      failureRatio: 0.5,
      minimumThroughput: 1,
      windowMs: 30_000,
      bucketMs: 1_000,
      resetMs: 10_000,
      halfOpenMaxConcurrent: 1,
      halfOpenSuccessesToClose: 1,
    };
    const data = reduceBreaker(initialBreakerData(), { type: 'failure' }, 0, unchecked);
    expect(data.state).toBe('open');
  });

  it('rejects the other out-of-range options', () => {
    expect(() => resolveBreakerOptions({ failureRatio: 0 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ failureRatio: 1.5 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ windowMs: 0 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ bucketMs: 60_000 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ resetMs: -1 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ halfOpenMaxConcurrent: 0 })).toThrow(ConfigError);
    expect(() => resolveBreakerOptions({ halfOpenSuccessesToClose: 0 })).toThrow(ConfigError);
  });

  it('accepts a failureRatio of exactly 1', () => {
    expect(resolveBreakerOptions({ failureRatio: 1 }).failureRatio).toBe(1);
  });
});

describe('reducer purity', () => {
  it('never mutates the state it is given', () => {
    const resolved = resolveBreakerOptions({ minimumThroughput: 2 });
    const before = initialBreakerData();
    const frozen = structuredClone(before);
    const after = reduceBreaker(before, { type: 'failure' }, 0, resolved);
    expect(before).toEqual(frozen);
    expect(after).not.toBe(before);
  });

  it('is deterministic: the same table twice yields the same state', () => {
    const options: BreakerOptions = { minimumThroughput: 4, failureRatio: 0.5 };
    const steps: Step[] = [
      { at: 0, event: { type: 'failure' }, state: 'closed' },
      { at: 1_000, event: { type: 'success' }, state: 'closed' },
      { at: 2_000, event: { type: 'failure' }, state: 'closed' },
      { at: 3_000, event: { type: 'success' }, state: 'open' },
    ];
    expect(walk(options, steps)).toEqual(walk(options, steps));
  });

  it('createBreakerReducer is exactly reduceBreaker with options bound', () => {
    const resolved = resolveBreakerOptions({ minimumThroughput: 2 });
    const reduce = createBreakerReducer(resolved);
    const start = initialBreakerData();
    expect(reduce(start, { type: 'failure' }, 7)).toEqual(
      reduceBreaker(start, { type: 'failure' }, 7, resolved),
    );
  });
});
