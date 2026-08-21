/**
 * Regression tests for the error taxonomy's two behavioural defects.
 *
 *  - `CircuitOpenError.retryAfterMs` was `halfOpenAt − openedAt`, which is the
 *    configured `resetMs` and never counts down.
 *  - `RetryExhaustedError` did not exist, so R4 §1.6's aggregate-and-last rule
 *    was inexpressible and the retry loop threw the last error unwrapped.
 */

import { describe, expect, it } from 'vitest';
import {
  CircuitOpenError,
  hasCode,
  isInterlayerError,
  RetryExhaustedError,
  TransportError,
  ValidationError,
} from './errors.ts';

describe('CircuitOpenError.retryAfterMs — a usable Retry-After', () => {
  const OPENED_AT = 1_000;
  const HALF_OPEN_AT = 11_000; // resetMs 10_000

  it('counts down from NOW, not from openedAt', () => {
    // The bug: `halfOpenAt − openedAt` is 10_000 at every instant, so a caller
    // nine seconds into a ten-second cooldown was told to wait another ten.
    const atOpen = new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, OPENED_AT);
    const midway = new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, 6_000);
    const nearlyDone = new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, 10_999);

    expect(atOpen.retryAfterMs).toBe(10_000);
    expect(midway.retryAfterMs).toBe(5_000);
    expect(nearlyDone.retryAfterMs).toBe(1);
  });

  it('is undefined once the cooldown has elapsed', () => {
    expect(new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, HALF_OPEN_AT).retryAfterMs).toBe(
      undefined,
    );
    expect(new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, 20_000).retryAfterMs).toBe(undefined);
  });

  it('keeps openedAt and halfOpenAt as the raw instants they are', () => {
    const e = new CircuitOpenError('p', OPENED_AT, HALF_OPEN_AT, 6_000);
    expect(e.openedAt).toBe(OPENED_AT);
    expect(e.halfOpenAt).toBe(HALF_OPEN_AT);
    expect(e.key).toBe('p');
    expect(e.details).toEqual({ key: 'p' });
    expect(e.retryable, 'a different provider may well work').toBe(true);
  });
});

describe('RetryExhaustedError', () => {
  const first = new TransportError('first');
  const last = new TransportError('last');

  it('carries every attempt, chronologically, with the last one as cause', () => {
    const e = new RetryExhaustedError(2, [first, last]);
    expect(e.code).toBe('RETRY_EXHAUSTED');
    expect(hasCode(e, 'RETRY_EXHAUSTED')).toBe(true);
    expect(isInterlayerError(e)).toBe(true);
    expect(e.attempts).toBe(2);
    expect(e.errors).toEqual([first, last]);
    expect(e.cause).toBe(last);
    expect(e.message).toContain('TransportError: last');
  });

  it('INHERITS retryable from the last failure', () => {
    // Load-bearing: the fallback chain advances on `error.retryable`. A wrapper
    // that answered `false` for itself would strand the call on a provider that
    // is merely having a bad minute.
    expect(new RetryExhaustedError(2, [first, last]).retryable).toBe(true);
    expect(new RetryExhaustedError(2, [first, new ValidationError('bad', [])]).retryable).toBe(
      false,
    );
    expect(new RetryExhaustedError(2, ['a string throw', null]).retryable).toBe(false);
  });

  it('lets an explicit context override the inherited retryable', () => {
    expect(new RetryExhaustedError(2, [first, last], { retryable: false }).retryable).toBe(false);
  });

  it('serialises without a stack and follows the cause chain', () => {
    const json = new RetryExhaustedError(2, [first, last], { providerId: 'openai' }).toJSON();
    expect(json).toMatchObject({
      code: 'RETRY_EXHAUSTED',
      providerId: 'openai',
      retryable: true,
      cause: { code: 'TRANSPORT', message: 'last' },
    });
    expect(Object.keys(json), 'a log projection never carries the stack').not.toContain('stack');
  });
});
