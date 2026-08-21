import { describe, expect, it } from 'vitest';
import { fakeProvider, testContext } from '../../test/support/index.ts';
import {
  AllProvidersFailedError,
  CancelledError,
  CircuitOpenError,
  ConfigError,
  hasCode,
  RateLimitedError,
  TimeoutError,
  TransportError,
  toInterlayerError,
  ValidationError,
} from '../core/errors.ts';
import type { AnyInterlayerError, CallContext, ProviderRecord } from '../core/types.ts';
import { abortErrorFor, defaultShouldFallback, runFallbackChain } from './fallback.ts';

function provider(id: string): ProviderRecord {
  return fakeProvider(id).record;
}

const A = provider('a');
const B = provider('b');
const C = provider('c');
const ABC: readonly ProviderRecord[] = [A, B, C];

/** Records which providers were entered, and settles as scripted. */
function scripted(script: Readonly<Record<string, unknown>>): {
  attempt: (provider: ProviderRecord) => Promise<unknown>;
  readonly entered: readonly string[];
} {
  const entered: string[] = [];
  return {
    entered,
    attempt: async (p: ProviderRecord): Promise<unknown> => {
      entered.push(p.id);
      const outcome = script[p.id];
      if (outcome instanceof Error) throw outcome;
      return outcome;
    },
  };
}

describe('runFallbackChain — the happy path', () => {
  it('returns the first success and records the winner', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({ a: 'from-a' });
    await expect(runFallbackChain(h.call, ABC, s.attempt)).resolves.toBe('from-a');
    expect(s.entered).toEqual(['a']);
    expect(h.stats.providersTried).toBe(1);
    expect(h.stats.winnerId).toBe('a');
    expect(h.failures).toHaveLength(0);
    expect(h.emitted('fallback:advance')).toHaveLength(0);
  });

  it('emits provider:selected once per provider entered, with the candidate set', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({ a: new TransportError('down'), b: 'from-b' });
    await expect(runFallbackChain(h.call, ABC, s.attempt)).resolves.toBe('from-b');
    const selected = h.emitted('provider:selected');
    expect(selected.map((e) => e.providerId)).toEqual(['a', 'b']);
    expect(selected.map((e) => e.index)).toEqual([0, 1]);
    expect(selected[0]?.candidateIds).toEqual(['a', 'b', 'c']);
    expect(selected[0]?.capability).toBe('chat');
  });

  it('emits fallback:advance with the error code as the reason', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({ a: new TransportError('down'), b: 'ok' });
    await runFallbackChain(h.call, ABC, s.attempt);
    const advanced = h.emitted('fallback:advance');
    expect(advanced).toHaveLength(1);
    expect(advanced[0]?.fromProviderId).toBe('a');
    expect(advanced[0]?.toProviderId).toBe('b');
    expect(advanced[0]?.reason).toBe('TRANSPORT');
  });

  it('advances until one succeeds and counts every provider entered', async () => {
    const h = testContext();
    const s = scripted({
      a: new TransportError('a down'),
      b: new TransportError('b down'),
      c: 'from-c',
    });
    await expect(runFallbackChain(h.call, ABC, s.attempt)).resolves.toBe('from-c');
    expect(s.entered).toEqual(['a', 'b', 'c']);
    expect(h.stats.providersTried).toBe(3);
    expect(h.stats.winnerId).toBe('c');
    // A fallback never swallows a cause, even when the call ultimately succeeds.
    expect(h.failures.map((e) => e.providerId)).toEqual(['a', 'b']);
  });
});

describe('runFallbackChain — aggregation (the rule R3 caught in its own draft)', () => {
  it('surfaces a SINGLE failure UNWRAPPED — never inside AllProvidersFailedError', async () => {
    const h = testContext({ capability: 'chat' });
    const boom = new TransportError('only provider is down');
    const s = scripted({ a: boom });

    const thrown = await runFallbackChain(h.call, [A], s.attempt).catch((e: unknown) => e);

    expect(thrown).not.toBeInstanceOf(AllProvidersFailedError);
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect((thrown as AnyInterlayerError).code).toBe('TRANSPORT');
    expect((thrown as AnyInterlayerError).message).toBe('only provider is down');
    expect((thrown as AnyInterlayerError).providerId).toBe('a');
  });

  it('aggregates at TWO failures, preserving every cause in order', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({ a: new TransportError('a down'), b: new RateLimitedError('b busy') });

    const thrown = await runFallbackChain(h.call, [A, B], s.attempt).catch((e: unknown) => e);

    expect(thrown).toBeInstanceOf(AllProvidersFailedError);
    expect(hasCode(thrown, 'ALL_FAILED')).toBe(true);
    const agg = thrown as AllProvidersFailedError;
    expect(agg.failures.map((e) => e.code)).toEqual(['TRANSPORT', 'RATE_LIMITED']);
    expect(agg.triedProviderIds).toEqual(['a', 'b']);
    expect(agg.capability).toBe('chat');
  });

  it('aggregates all three when three providers fail', async () => {
    const h = testContext();
    const s = scripted({
      a: new TransportError('a'),
      b: new TransportError('b'),
      c: new TransportError('c'),
    });
    const thrown = await runFallbackChain(h.call, ABC, s.attempt).catch((e: unknown) => e);
    expect((thrown as AllProvidersFailedError).failures).toHaveLength(3);
  });

  it('does not aggregate when the chain stops at the FIRST, non-retryable failure', async () => {
    const h = testContext();
    // ValidationError is not retryable: no other provider would do better.
    const s = scripted({ a: new ValidationError('bad input', []), b: 'never reached' });

    const thrown = await runFallbackChain(h.call, ABC, s.attempt).catch((e: unknown) => e);

    expect(hasCode(thrown, 'VALIDATION')).toBe(true);
    expect(s.entered).toEqual(['a']);
    expect(h.stats.providersTried).toBe(1);
    expect(h.emitted('fallback:advance')).toHaveLength(0);
  });

  it('normalises a non-Interlayer throw and stamps the provider id on it', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({ a: new Error('raw failure') });
    const thrown = await runFallbackChain(h.call, [A], s.attempt).catch((e: unknown) => e);
    expect(hasCode(thrown, 'PROVIDER_ERROR')).toBe(true);
    expect((thrown as AnyInterlayerError).providerId).toBe('a');
    expect((thrown as AnyInterlayerError).capability).toBe('chat');
  });
});

describe('runFallbackChain — zero candidates', () => {
  it('throws NoProviderError rather than resolving to an empty success', async () => {
    const h = testContext({ capability: 'chat' });
    const s = scripted({});
    const thrown = await runFallbackChain(h.call, [], s.attempt).catch((e: unknown) => e);
    expect(hasCode(thrown, 'NO_PROVIDER')).toBe(true);
    expect(s.entered).toEqual([]);
    expect(h.stats.providersTried).toBe(0);
  });

  it('throws NoProviderError when maxProviders admits nobody', async () => {
    const h = testContext();
    const s = scripted({ a: 'ok' });
    const thrown = await runFallbackChain(h.call, ABC, s.attempt, { maxProviders: 0 }).catch(
      (e: unknown) => e,
    );
    expect(hasCode(thrown, 'NO_PROVIDER')).toBe(true);
    expect(s.entered).toEqual([]);
  });
});

describe('runFallbackChain — cancellation', () => {
  it('surfaces CancelledError immediately when the signal is already aborted', async () => {
    const h = testContext();
    const s = scripted({ a: 'ok' });
    h.abort();

    const thrown = await runFallbackChain(h.call, ABC, s.attempt).catch((e: unknown) => e);

    expect(hasCode(thrown, 'CANCELLED')).toBe(true);
    expect(s.entered).toEqual([]);
    expect(h.stats.providersTried).toBe(0);
  });

  it('stops the walk instead of burning through the remaining providers', async () => {
    const h = testContext();
    const s = scripted({ a: new CancelledError('caller went away'), b: 'never reached' });

    const thrown = await runFallbackChain(h.call, ABC, s.attempt).catch((e: unknown) => e);

    expect(hasCode(thrown, 'CANCELLED')).toBe(true);
    expect(s.entered).toEqual(['a']);
    expect(h.emitted('fallback:advance')).toHaveLength(0);
  });

  it('surfaces the cancellation UNWRAPPED even after an earlier provider failed', async () => {
    const h = testContext();
    const s = scripted({
      a: new TransportError('a down'),
      b: new CancelledError('caller went away'),
      c: 'never reached',
    });

    const thrown = await runFallbackChain(h.call, ABC, s.attempt).catch((e: unknown) => e);

    // Two failures were recorded, but a caller abort is not a provider failure:
    // it is not aggregated, so `err.code === 'CANCELLED'` still holds.
    expect(thrown).not.toBeInstanceOf(AllProvidersFailedError);
    expect(hasCode(thrown, 'CANCELLED')).toBe(true);
    expect(s.entered).toEqual(['a', 'b']);
    expect(h.failures).toHaveLength(2);
  });

  it('stops at the aborting provider even when its error is retryable', async () => {
    const h = testContext();
    const entered: string[] = [];
    const attempt = async (p: ProviderRecord): Promise<unknown> => {
      entered.push(p.id);
      h.abort();
      throw new TransportError('down');
    };

    const thrown = await runFallbackChain(h.call, ABC, attempt).catch((e: unknown) => e);

    // The provider's own error is never replaced — but the walk stops dead
    // instead of burning through b and c.
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect(entered).toEqual(['a']);
    expect(h.emitted('fallback:advance')).toHaveLength(0);
  });

  it('normalises a platform AbortError rejection into CancelledError', async () => {
    const h = testContext();
    const entered: string[] = [];
    const attempt = async (p: ProviderRecord): Promise<unknown> => {
      entered.push(p.id);
      h.abort();
      throw h.call.signal.reason;
    };

    const thrown = await runFallbackChain(h.call, ABC, attempt).catch((e: unknown) => e);

    expect(hasCode(thrown, 'CANCELLED')).toBe(true);
    expect(entered).toEqual(['a']);
  });

  it('abortErrorFor surfaces an Interlayer abort reason as itself', () => {
    const h = testContext({ capability: 'chat' });
    h.abort(new CancelledError('deadline elapsed'));
    const error = abortErrorFor(h.call);
    expect(hasCode(error, 'CANCELLED')).toBe(true);
    expect(error.message).toBe('deadline elapsed');
    expect(error.capability).toBe('chat');
  });

  it('abortErrorFor wraps a bare platform abort reason as CancelledError', () => {
    const h = testContext();
    h.abort();
    const error = abortErrorFor(h.call);
    expect(hasCode(error, 'CANCELLED')).toBe(true);
    expect(error.cause).toBeDefined();
  });
});

describe('shouldFallback', () => {
  it('defaults to error.retryable', () => {
    const ctx: CallContext = testContext().call;
    expect(defaultShouldFallback(new TransportError('x'), ctx)).toBe(true);
    expect(defaultShouldFallback(new RateLimitedError('x'), ctx)).toBe(true);
    expect(defaultShouldFallback(new ValidationError('x', []), ctx)).toBe(false);
  });

  it('never falls back on a cancellation, retryable or not', () => {
    const ctx: CallContext = testContext().call;
    expect(defaultShouldFallback(new CancelledError(), ctx)).toBe(false);
  });

  it('never falls back once the call signal is aborted', () => {
    const h = testContext();
    h.abort();
    expect(defaultShouldFallback(new TransportError('x'), h.call)).toBe(false);
  });

  it('honours a custom predicate — stopping early', async () => {
    const h = testContext();
    const s = scripted({ a: new TransportError('a down'), b: 'never reached' });
    const thrown = await runFallbackChain(h.call, ABC, s.attempt, {
      shouldFallback: (): boolean => false,
    }).catch((e: unknown) => e);
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect(s.entered).toEqual(['a']);
  });

  it('honours a custom predicate — continuing past a non-retryable error', async () => {
    const h = testContext();
    const s = scripted({ a: new ValidationError('bad', []), b: 'from-b' });
    await expect(
      runFallbackChain(h.call, ABC, s.attempt, { shouldFallback: (): boolean => true }),
    ).resolves.toBe('from-b');
    expect(s.entered).toEqual(['a', 'b']);
  });

  it('receives the error and the call context', async () => {
    const h = testContext({ capability: 'chat' });
    const seen: string[] = [];
    const s = scripted({ a: new TransportError('a down'), b: 'ok' });
    await runFallbackChain(h.call, ABC, s.attempt, {
      shouldFallback: (error: AnyInterlayerError, ctx: CallContext): boolean => {
        seen.push(`${error.code}:${ctx.capability}`);
        return true;
      },
    });
    expect(seen).toEqual(['TRANSPORT:chat']);
  });
});

describe('runFallbackChain — maxProviders', () => {
  it('caps the walk and aggregates only what it tried', async () => {
    const h = testContext();
    const s = scripted({
      a: new TransportError('a'),
      b: new TransportError('b'),
      c: new TransportError('c'),
    });

    const thrown = await runFallbackChain(h.call, ABC, s.attempt, { maxProviders: 2 }).catch(
      (e: unknown) => e,
    );

    expect(s.entered).toEqual(['a', 'b']);
    expect((thrown as AllProvidersFailedError).failures).toHaveLength(2);
    // No advance is announced when the cap, not the error, ends the chain.
    expect(h.emitted('fallback:advance').map((e) => e.toProviderId)).toEqual(['b']);
  });

  it('a cap of 1 leaves the single failure unwrapped', async () => {
    const h = testContext();
    const s = scripted({ a: new TransportError('a down'), b: 'never reached' });
    const thrown = await runFallbackChain(h.call, ABC, s.attempt, { maxProviders: 1 }).catch(
      (e: unknown) => e,
    );
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect(s.entered).toEqual(['a']);
    expect(h.emitted('fallback:advance')).toHaveLength(0);
  });
});

describe('defaultShouldFallback — plain throws must advance the chain', () => {
  const ctx = (): CallContext => testContext().call;

  it('falls back on a bare `throw new Error()` from a provider', () => {
    // The regression this file exists for: an unclassified provider throw
    // becomes a non-retryable PROVIDER_ERROR. Keying fallback off `retryable`
    // stranded the call on provider A while healthy alternates sat unused.
    const err = toInterlayerError(new Error('boom'), {
      providerId: 'a',
      capability: 'chat',
      callId: 'c1',
    });
    expect(err.code).toBe('PROVIDER_ERROR');
    expect(err.retryable).toBe(false);
    expect(defaultShouldFallback(err, ctx())).toBe(true);
  });

  it.each([
    ['TIMEOUT', new TimeoutError(10, 'attempt')],
    ['RATE_LIMITED', new RateLimitedError('limited', 5)],
    ['CIRCUIT_OPEN', new CircuitOpenError('a', 0, 1000, 0)],
  ] as const)('falls back on %s', (_code, err) => {
    expect(defaultShouldFallback(err as AnyInterlayerError, ctx())).toBe(true);
  });

  it.each([
    ['CANCELLED', new CancelledError('caller stopped')],
    ['CONFIG', new ConfigError('bad options')],
  ] as const)('does NOT fall back on %s — a different provider is futile', (_code, err) => {
    expect(defaultShouldFallback(err as AnyInterlayerError, ctx())).toBe(false);
  });
});
