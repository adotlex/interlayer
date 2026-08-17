import { describe, expect, it } from 'vitest';
import { constantRandom, scriptedRandom, seededRandom } from './seeded-random.ts';

/** The exact full-jitter formula U2 implements: cap first, then jitter. */
const fullJitter = (retryIndex: number, rand: () => number): number =>
  rand() * Math.min(30_000, 100 * 2 ** (retryIndex - 1));

describe('seededRandom', () => {
  it('is byte-identical across two generators with the same seed', () => {
    const a = seededRandom(42);
    const b = seededRandom(42);
    const seqA = Array.from({ length: 10 }, () => a());
    const seqB = Array.from({ length: 10 }, () => b());
    expect(seqA).toEqual(seqB);
  });

  it('diverges for a different seed', () => {
    const a = Array.from({ length: 5 }, seededRandom(42));
    const b = Array.from({ length: 5 }, seededRandom(43));
    expect(a).not.toEqual(b);
  });

  it('stays inside [0, 1)', () => {
    const r = seededRandom(7);
    for (let i = 0; i < 1000; i++) {
      const v = r();
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });

  it('reproduces the same delay sequence run after run', () => {
    const delays = (): number[] => {
      const r = seededRandom(42);
      return [fullJitter(1, r), fullJitter(2, r), fullJitter(3, r)];
    };
    expect(delays()).toEqual(delays());
  });
});

describe('scriptedRandom', () => {
  it('returns the scripted values in order', () => {
    const r = scriptedRandom([0.1, 0.2, 0.3]);
    expect([r(), r(), r()]).toEqual([0.1, 0.2, 0.3]);
  });

  it('repeats the last value once exhausted', () => {
    const r = scriptedRandom([0.5]);
    expect([r(), r(), r()]).toEqual([0.5, 0.5, 0.5]);
  });

  it('tracks how many values were drawn', () => {
    const r = scriptedRandom([0.1, 0.2]);
    expect(r.drawn).toBe(0);
    r();
    expect(r.drawn).toBe(1);
    expect(r.remaining).toBe(1);
  });

  it('makes full jitter an EXACT assertion, not a range', () => {
    const r = scriptedRandom([0.5, 0.25, 1]);
    expect(fullJitter(1, r)).toBe(50); // 0.50 * 100
    expect(fullJitter(2, r)).toBe(50); // 0.25 * 200
    expect(fullJitter(3, r)).toBe(400); // 1.00 * 400
  });
});

describe('constantRandom', () => {
  it('kills jitter entirely at 0', () => {
    const r = constantRandom(0);
    expect(fullJitter(1, r)).toBe(0);
    expect(fullJitter(5, r)).toBe(0);
  });
});
