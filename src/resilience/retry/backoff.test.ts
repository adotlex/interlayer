import { describe, expect, it } from 'vitest';
import {
  constantRandom,
  scriptedRandom,
  seededRandom,
} from '../../../test/support/seeded-random.ts';
import {
  type BackoffParams,
  backoffDelay,
  createBackoff,
  decorrelatedDelay,
  exponentialTerm,
} from './backoff.ts';

/** R4 §0.1 defaults, with the strategy swapped per test. */
function params(patch: Partial<BackoffParams> = {}): BackoffParams {
  return {
    strategy: 'exponential',
    baseDelayMs: 100,
    factor: 2,
    maxDelayMs: 30_000,
    ...patch,
  };
}

/** The first `count` delays of a strategy, drawn from one shared PRNG. */
function sequence(o: BackoffParams, count: number, random = constantRandom(0)): number[] {
  const backoff = createBackoff(o, random);
  return Array.from({ length: count }, (_, i) => backoff.next(i + 1));
}

describe('exponentialTerm — the capped, unjittered curve', () => {
  it('RT-11: base 100 / factor 2 / cap 30_000 gives exactly [100, 200, 400, 800, 1600]', () => {
    const o = params();
    expect([1, 2, 3, 4, 5].map((n) => exponentialTerm(n, o))).toEqual([100, 200, 400, 800, 1600]);
  });

  it('RT-12: maxDelayMs 1000 caps the same curve at [100, 200, 400, 800, 1000, 1000]', () => {
    const o = params({ maxDelayMs: 1000 });
    expect([1, 2, 3, 4, 5, 6].map((n) => exponentialTerm(n, o))).toEqual([
      100, 200, 400, 800, 1000, 1000,
    ]);
  });

  it('retryIndex is 1-based: the FIRST retry waits exactly baseDelayMs', () => {
    expect(exponentialTerm(1, params())).toBe(100);
  });

  it('clamps a retryIndex below 1 rather than producing base / factor', () => {
    expect(exponentialTerm(0, params())).toBe(100);
    expect(exponentialTerm(-5, params())).toBe(100);
  });

  it('maxDelayMs wins when it is smaller than baseDelayMs (the cap always applies)', () => {
    expect(exponentialTerm(1, params({ baseDelayMs: 5_000, maxDelayMs: 250 }))).toBe(250);
  });

  it('collapses onto the cap instead of overflowing to Infinity at a large index', () => {
    expect(exponentialTerm(2_000, params())).toBe(30_000);
  });
});

describe('backoffDelay — fixed', () => {
  it('RT-13: every delay is exactly baseDelayMs regardless of retry index', () => {
    expect(sequence(params({ strategy: 'fixed' }), 5)).toEqual([100, 100, 100, 100, 100]);
  });

  it('is still bounded by maxDelayMs', () => {
    expect(backoffDelay(1, params({ strategy: 'fixed', maxDelayMs: 40 }), constantRandom(0))).toBe(
      40,
    );
  });

  it('draws ZERO random values', () => {
    const random = scriptedRandom([0.5]);
    backoffDelay(3, params({ strategy: 'fixed' }), random);
    expect(random.drawn).toBe(0);
  });
});

describe('backoffDelay — exponential (no jitter)', () => {
  it('reproduces the uncapped curve and draws ZERO random values', () => {
    const random = scriptedRandom([0.5]);
    expect(sequence(params({ strategy: 'exponential' }), 4, random)).toEqual([100, 200, 400, 800]);
    expect(random.drawn).toBe(0);
  });
});

describe('backoffDelay — full jitter (the DEFAULT)', () => {
  const o = params({ strategy: 'full' });

  it('RT-14: random() -> 0 gives exactly 0', () => {
    expect(backoffDelay(3, o, constantRandom(0))).toBe(0);
  });

  it('RT-14: random() -> 0.5 gives exactly exp(n) / 2', () => {
    // exp(3) = 100 * 2**2 = 400
    expect(backoffDelay(3, o, constantRandom(0.5))).toBe(200);
  });

  it('RT-14: random() -> 0.999... stays strictly below exp(n)', () => {
    const d = backoffDelay(3, o, constantRandom(0.9999999));
    expect(d).toBeLessThan(400);
    expect(d).toBeGreaterThan(399);
  });

  it('is an EXACT function of (n, rand()) — a scripted PRNG pins every value', () => {
    // exp = [100, 200, 400]; rand = [0.5, 0.25, 0.125]
    const random = scriptedRandom([0.5, 0.25, 0.125]);
    expect(sequence(o, 3, random)).toEqual([50, 50, 50]);
    expect(random.drawn).toBe(3);
  });

  it('CAP BEFORE JITTER: cap 1000 at n=5 yields 500, not the 800 of cap-after-jitter', () => {
    // exp(5) uncapped = 1600. Capped first => 1000, halved => 500.
    // Capped last  => min(1000, 0.5 * 1600) = 800. The two are distinguishable.
    const capped = params({ strategy: 'full', maxDelayMs: 1000 });
    expect(backoffDelay(5, capped, constantRandom(0.5))).toBe(500);
  });

  it('never exceeds maxDelayMs across a long seeded run', () => {
    const capped = params({ strategy: 'full', maxDelayMs: 1000 });
    const random = seededRandom(7);
    for (let n = 1; n <= 200; n++) {
      const d = backoffDelay(n, capped, random);
      expect(d).toBeGreaterThanOrEqual(0);
      expect(d).toBeLessThan(1000);
    }
  });
});

describe('backoffDelay — equal jitter', () => {
  const o = params({ strategy: 'equal' });

  it('RT-15: random() -> 0 gives exactly exp(n) / 2', () => {
    expect(backoffDelay(3, o, constantRandom(0))).toBe(200);
  });

  it('RT-15: random() -> 1 gives exactly exp(n)', () => {
    expect(backoffDelay(3, o, constantRandom(1))).toBe(400);
  });

  it('stays within [exp/2, exp] across a long seeded run', () => {
    const random = seededRandom(11);
    for (let n = 1; n <= 8; n++) {
      const exp = exponentialTerm(n, o);
      const d = backoffDelay(n, o, random);
      expect(d).toBeGreaterThanOrEqual(exp / 2);
      expect(d).toBeLessThanOrEqual(exp);
    }
  });
});

describe('decorrelatedDelay', () => {
  const o = params({ strategy: 'decorrelated', maxDelayMs: 30_000 });

  it('RT-16: is pure — the same `prev` twice yields the same result', () => {
    const a = decorrelatedDelay(500, o, constantRandom(0.375));
    const b = decorrelatedDelay(500, o, constantRandom(0.375));
    expect(a).toBe(b);
    // base + rand * (prev*3 - base) = 100 + 0.375 * (1500 - 100) = 625
    expect(a).toBe(625);
  });

  it('RT-16: random() -> 0 yields exactly baseDelayMs (the lower bound)', () => {
    expect(decorrelatedDelay(9_999, o, constantRandom(0))).toBe(100);
  });

  it('RT-16: random() -> 1 yields prev * 3, still bounded by the cap', () => {
    expect(decorrelatedDelay(500, o, constantRandom(1))).toBe(1500);
    expect(decorrelatedDelay(50_000, o, constantRandom(1))).toBe(30_000);
  });

  it('RT-16: every value of a long chain sits in [baseDelayMs, maxDelayMs]', () => {
    const capped = params({ strategy: 'decorrelated', maxDelayMs: 20_000 });
    const backoff = createBackoff(capped, seededRandom(3));
    for (let n = 1; n <= 500; n++) {
      const d = backoff.next(n);
      expect(d).toBeGreaterThanOrEqual(100);
      expect(d).toBeLessThanOrEqual(20_000);
    }
  });

  it('seeds `prev` from baseDelayMs on the first step', () => {
    // prev = base = 100 => 100 + 1 * (300 - 100) = 300
    expect(backoffDelay(1, o, constantRandom(1))).toBe(300);
  });
});

describe('createBackoff — the stateful sequencer', () => {
  it('threads the previous delay so decorrelated actually decorrelates', () => {
    const backoff = createBackoff(params({ strategy: 'decorrelated' }), constantRandom(1));
    // prev seeds at 100 and triples each step: 300, 900, 2700.
    expect([backoff.next(1), backoff.next(2), backoff.next(3)]).toEqual([300, 900, 2700]);
  });

  it('reset() re-seeds the decorrelated chain back to baseDelayMs', () => {
    const backoff = createBackoff(params({ strategy: 'decorrelated' }), constantRandom(1));
    backoff.next(1);
    backoff.next(2);
    expect(backoff.previousDelayMs).toBe(900);
    backoff.reset();
    expect(backoff.previousDelayMs).toBe(100);
    expect(backoff.next(1)).toBe(300);
  });

  it('exposes the last produced delay for the non-stateful strategies too', () => {
    const backoff = createBackoff(params({ strategy: 'exponential' }), constantRandom(0));
    backoff.next(3);
    expect(backoff.previousDelayMs).toBe(400);
  });
});

describe('numeric hygiene', () => {
  it('never returns a negative delay', () => {
    const weird = params({ strategy: 'full', baseDelayMs: 0, maxDelayMs: 0 });
    expect(backoffDelay(4, weird, constantRandom(0.9))).toBe(0);
  });

  it('never returns NaN for a NaN-producing configuration', () => {
    const weird = params({ strategy: 'exponential', baseDelayMs: Number.NaN });
    expect(backoffDelay(2, weird, constantRandom(0))).toBe(0);
  });

  it('a factor of 1 degenerates to a flat baseDelayMs curve', () => {
    expect(sequence(params({ factor: 1 }), 4)).toEqual([100, 100, 100, 100]);
  });
});
