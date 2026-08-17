import { describe, expect, it } from 'vitest';
import { DEFAULTS } from '../../core/policy.ts';
import {
  assertTimeoutMs,
  composeDeadline,
  deadlineAtFrom,
  describeMs,
  earliestDeadline,
  isExpired,
  remainingMs,
  resolveTimeoutOptions,
  tighterTimeoutMs,
  UNBOUNDED_MS,
} from './deadline.ts';

describe('assertTimeoutMs', () => {
  it('accepts 0, finite positives and Infinity, returning the value unchanged', () => {
    expect(assertTimeoutMs(0, 'x')).toBe(0);
    expect(assertTimeoutMs(1, 'x')).toBe(1);
    expect(assertTimeoutMs(30_000, 'x')).toBe(30_000);
    expect(assertTimeoutMs(UNBOUNDED_MS, 'x')).toBe(Number.POSITIVE_INFINITY);
  });

  it('throws RangeError for negative and NaN budgets (R4 TO-8)', () => {
    expect(() => assertTimeoutMs(-1, 'attemptTimeoutMs')).toThrow(RangeError);
    expect(() => assertTimeoutMs(-0.0001, 'attemptTimeoutMs')).toThrow(RangeError);
    expect(() => assertTimeoutMs(Number.NaN, 'attemptTimeoutMs')).toThrow(RangeError);
    expect(() => assertTimeoutMs(Number.NEGATIVE_INFINITY, 'attemptTimeoutMs')).toThrow(RangeError);
  });

  it('names the offending option in the message', () => {
    expect(() => assertTimeoutMs(-1, 'totalTimeoutMs')).toThrow(/totalTimeoutMs/);
  });
});

describe('remainingMs / isExpired', () => {
  it('treats an absent deadline as unbounded', () => {
    expect(remainingMs(undefined, 1_000)).toBe(Number.POSITIVE_INFINITY);
    expect(isExpired(undefined, Number.MAX_SAFE_INTEGER)).toBe(false);
  });

  it('reports the exact budget left and clamps at zero — never negative', () => {
    expect(remainingMs(1_000, 400)).toBe(600);
    expect(remainingMs(1_000, 1_000)).toBe(0);
    expect(remainingMs(1_000, 5_000)).toBe(0);
  });

  it('expires exactly at the deadline instant, not after it', () => {
    expect(isExpired(1_000, 999)).toBe(false);
    expect(isExpired(1_000, 1_000)).toBe(true);
    expect(isExpired(1_000, 1_001)).toBe(true);
  });
});

describe('deadlineAtFrom', () => {
  it('projects a finite budget onto the clock', () => {
    expect(deadlineAtFrom(500, 250)).toBe(750);
    expect(deadlineAtFrom(0, 0)).toBe(0);
  });

  it('gives an unbounded budget no instant at all', () => {
    expect(deadlineAtFrom(500, UNBOUNDED_MS)).toBeUndefined();
  });
});

describe('tighterTimeoutMs / earliestDeadline', () => {
  it('picks the smaller budget, in either argument order', () => {
    expect(tighterTimeoutMs(100, 10_000)).toBe(100);
    expect(tighterTimeoutMs(10_000, 100)).toBe(100);
    expect(tighterTimeoutMs(100, UNBOUNDED_MS)).toBe(100);
    expect(tighterTimeoutMs(UNBOUNDED_MS, UNBOUNDED_MS)).toBe(Number.POSITIVE_INFINITY);
  });

  it('treats an absent deadline as "no deadline", never as zero', () => {
    expect(earliestDeadline(undefined, undefined)).toBeUndefined();
    expect(earliestDeadline(500, undefined)).toBe(500);
    expect(earliestDeadline(undefined, 500)).toBe(500);
    expect(earliestDeadline(500, 400)).toBe(400);
    expect(earliestDeadline(400, 500)).toBe(400);
  });
});

describe('composeDeadline', () => {
  it('uses the configured timeout when no deadline is in force', () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: 250 })).toEqual({
      timeoutMs: 250,
      deadlineAt: 1_250,
      expired: false,
      source: 'timeout',
    });
  });

  it('is unbounded only when both inputs are', () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: UNBOUNDED_MS })).toEqual({
      timeoutMs: Number.POSITIVE_INFINITY,
      deadlineAt: undefined,
      expired: false,
      source: 'unbounded',
    });
  });

  it('lets the tighter of the two win — deadline side', () => {
    // 10s attempt budget, but only 300ms of call deadline left.
    expect(composeDeadline({ now: 1_000, timeoutMs: 10_000, deadlineAt: 1_300 })).toEqual({
      timeoutMs: 300,
      deadlineAt: 1_300,
      expired: false,
      source: 'deadline',
    });
  });

  it('lets the tighter of the two win — timeout side', () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: 100, deadlineAt: 9_999 })).toEqual({
      timeoutMs: 100,
      deadlineAt: 1_100,
      expired: false,
      source: 'timeout',
    });
  });

  it('clamps an unbounded timeout to an inherited deadline', () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: UNBOUNDED_MS, deadlineAt: 1_500 })).toEqual({
      timeoutMs: 500,
      deadlineAt: 1_500,
      expired: false,
      source: 'deadline',
    });
  });

  it("breaks an exact tie in favour of the policy's own configured timeout", () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: 500, deadlineAt: 1_500 })).toEqual({
      timeoutMs: 500,
      deadlineAt: 1_500,
      expired: false,
      source: 'timeout',
    });
  });

  it('reports expiry for a zero timeout (R4 TO-6)', () => {
    expect(composeDeadline({ now: 1_000, timeoutMs: 0 })).toEqual({
      timeoutMs: 0,
      deadlineAt: 1_000,
      expired: true,
      source: 'timeout',
    });
  });

  it('reports expiry for a deadline already reached or passed', () => {
    expect(composeDeadline({ now: 2_000, timeoutMs: 10_000, deadlineAt: 2_000 })).toMatchObject({
      timeoutMs: 0,
      expired: true,
      source: 'deadline',
    });
    expect(composeDeadline({ now: 5_000, timeoutMs: 10_000, deadlineAt: 2_000 })).toMatchObject({
      timeoutMs: 0,
      deadlineAt: 2_000,
      expired: true,
      source: 'deadline',
    });
  });

  it('rejects an invalid budget with RangeError', () => {
    expect(() => composeDeadline({ now: 0, timeoutMs: -5 })).toThrow(RangeError);
    expect(() => composeDeadline({ now: 0, timeoutMs: Number.NaN })).toThrow(RangeError);
  });

  it('is pure: the same input yields an equal result every time', () => {
    const input = { now: 1_000, timeoutMs: 10_000, deadlineAt: 1_300 } as const;
    expect(composeDeadline(input)).toEqual(composeDeadline(input));
    expect(input).toEqual({ now: 1_000, timeoutMs: 10_000, deadlineAt: 1_300 });
  });

  it('composes transitively: nesting a third budget can only tighten it further', () => {
    const call = composeDeadline({ now: 0, timeoutMs: 30_000 });
    const attempt = composeDeadline({
      now: 0,
      timeoutMs: 10_000,
      deadlineAt: call.deadlineAt,
    });
    const inner = composeDeadline({ now: 0, timeoutMs: 50_000, deadlineAt: attempt.deadlineAt });
    expect(attempt.timeoutMs).toBe(10_000);
    expect(inner.timeoutMs).toBe(10_000);
    expect(inner.deadlineAt).toBe(10_000);
    expect(inner.source).toBe('deadline');
  });
});

describe('resolveTimeoutOptions', () => {
  it('falls back to the R4 defaults for both fields', () => {
    expect(resolveTimeoutOptions()).toEqual({
      attemptTimeoutMs: DEFAULTS.timeout.attemptTimeoutMs,
      totalTimeoutMs: DEFAULTS.timeout.totalTimeoutMs,
    });
    expect(resolveTimeoutOptions({})).toEqual(resolveTimeoutOptions());
  });

  it('treats an explicitly-undefined field as unset', () => {
    expect(resolveTimeoutOptions({ attemptTimeoutMs: undefined })).toEqual({
      attemptTimeoutMs: 10_000,
      totalTimeoutMs: 30_000,
    });
  });

  it('applies overrides, including 0 and Infinity', () => {
    expect(resolveTimeoutOptions({ attemptTimeoutMs: 0, totalTimeoutMs: UNBOUNDED_MS })).toEqual({
      attemptTimeoutMs: 0,
      totalTimeoutMs: Number.POSITIVE_INFINITY,
    });
  });

  it('throws RangeError at resolution time for either bad field', () => {
    expect(() => resolveTimeoutOptions({ attemptTimeoutMs: -1 })).toThrow(RangeError);
    expect(() => resolveTimeoutOptions({ totalTimeoutMs: Number.NaN })).toThrow(RangeError);
  });

  it('permits an inverted pair: the deadline clamp makes it harmless, not an error', () => {
    const resolved = resolveTimeoutOptions({ attemptTimeoutMs: 30_000, totalTimeoutMs: 1_000 });
    expect(resolved.attemptTimeoutMs).toBe(30_000);
    // What saves it: composing against the call deadline tightens the attempt to 1s.
    const call = composeDeadline({ now: 0, timeoutMs: resolved.totalTimeoutMs });
    const attempt = composeDeadline({
      now: 0,
      timeoutMs: resolved.attemptTimeoutMs,
      deadlineAt: call.deadlineAt,
    });
    expect(attempt.timeoutMs).toBe(1_000);
  });
});

describe('describeMs', () => {
  it('renders a finite budget with its unit and an infinite one as "unbounded"', () => {
    expect(describeMs(10_000)).toBe('10000ms');
    expect(describeMs(0)).toBe('0ms');
    expect(describeMs(UNBOUNDED_MS)).toBe('unbounded');
  });
});
