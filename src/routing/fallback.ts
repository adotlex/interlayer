/**
 * The fallback chain walk — U6, `src/routing/`.
 *
 * Try candidates in selector order; after each failure ask `shouldFallback`
 * whether the next one is worth trying; aggregate what is left over.
 *
 * THE AGGREGATION RULE (setup guide §4/U6, R3 §4 — settled, do not soften):
 *
 *   0 attempts  -> `NoProviderError`.              Never an empty success.
 *   1 failure   -> THAT failure, UNWRAPPED.        Wrapping buries the real
 *                                                  error one level down and
 *                                                  breaks `err.code` asserts.
 *   >= 2        -> `AllProvidersFailedError`.      Every cause preserved, in
 *                                                  the order they were tried.
 *
 * THE CANCELLATION RULE: a caller abort is not a provider failure. It stops the
 * chain where it stands and surfaces as itself (`CancelledError`, or whatever
 * typed error the signal carried as its reason) rather than being buried in an
 * aggregate or burning through every remaining provider. Failures collected
 * before the abort stay on `ctx.failures` — nothing is lost, it just is not
 * what the caller is told about.
 */

import {
  AllProvidersFailedError,
  CancelledError,
  isInterlayerError,
  NoProviderError,
  toInterlayerError,
} from '../core/errors.ts';
import type { AnyInterlayerError, CallContext, ProviderRecord } from '../core/types.ts';

/** Decides whether the chain should advance to the next candidate. */
export type ShouldFallback = (error: AnyInterlayerError, ctx: CallContext) => boolean;

/**
 * What the chain runs per candidate. `index` is the position in the SELECTED
 * order, which is why a plain `(provider) => …` (the shape `Router.route`
 * takes) is assignable here unchanged.
 */
export type FallbackAttempt = (provider: ProviderRecord, index: number) => Promise<unknown>;

export interface FallbackOptions {
  /** Max providers tried per call. Default: unlimited. */
  readonly maxProviders?: number | undefined;
  /** Default: {@link defaultShouldFallback}. */
  readonly shouldFallback?: ShouldFallback | undefined;
}

/** A caller-initiated stop, as opposed to a provider that failed. */
export function isCancellation(error: AnyInterlayerError): boolean {
  return error.code === 'CANCELLED';
}

/**
 * The default policy: fall back exactly when a DIFFERENT provider could
 * plausibly do better.
 *
 * `error.retryable` already encodes that for the whole taxonomy (transport,
 * attempt timeout, rate limited and open-circuit are retryable; validation,
 * config and cancellation are not). The two explicit guards exist because a
 * cancelled call must never march through the remaining providers — the caller
 * asked us to stop, and "try harder" is the wrong answer to that.
 */
export function defaultShouldFallback(error: AnyInterlayerError, ctx: CallContext): boolean {
  if (isCancellation(error)) return false;
  if (ctx.signal.aborted) return false;
  return error.retryable;
}

/**
 * Turns whatever aborted the call signal into a typed error.
 *
 * A deadline aborts with an `InterlayerError` as its reason; that error is the
 * truth and is surfaced as-is (merely stamped with call identity). Anything
 * else — including the bare `DOMException` the platform supplies when
 * `abort()` is called with no reason — becomes a `CancelledError` carrying the
 * original reason as `cause`.
 */
export function abortErrorFor(ctx: CallContext): AnyInterlayerError {
  const reason: unknown = ctx.signal.reason;
  const identity = { callId: ctx.callId, capability: ctx.capability };
  if (isInterlayerError(reason)) return reason.withContext(identity);
  return new CancelledError('Call was cancelled', { ...identity, cause: reason });
}

/**
 * Walks `candidates` in order until one succeeds.
 *
 * Records `provider:selected` / `fallback:advance` on the emitter and maintains
 * `ctx.stats.providersTried`, `ctx.stats.winnerId` and `ctx.failures`. Never
 * resolves to `undefined` on an empty chain — see the aggregation rule above.
 */
export async function runFallbackChain(
  ctx: CallContext,
  candidates: readonly ProviderRecord[],
  attempt: FallbackAttempt,
  options: FallbackOptions = {},
): Promise<unknown> {
  const shouldFallback = options.shouldFallback ?? defaultShouldFallback;
  const limit = options.maxProviders ?? Number.POSITIVE_INFINITY;
  const candidateIds = candidates.map((provider) => provider.id);
  const errors: AnyInterlayerError[] = [];

  for (let index = 0; index < candidates.length && index < limit; index++) {
    const provider = candidates[index];
    if (provider === undefined) break;

    // Checked before every provider, not just the first: the signal may abort
    // while an earlier candidate was in flight.
    if (ctx.signal.aborted) throw abortErrorFor(ctx);

    ctx.events.emit('provider:selected', {
      callId: ctx.callId,
      capability: ctx.capability,
      providerId: provider.id,
      candidateIds,
      index,
      at: ctx.runtime.now(),
    });
    ctx.stats.providersTried++;

    try {
      const value = await attempt(provider, index);
      ctx.stats.winnerId = provider.id;
      return value;
    } catch (raw) {
      const error = toInterlayerError(raw, {
        providerId: provider.id,
        capability: ctx.capability,
        callId: ctx.callId,
      });
      errors.push(error);
      ctx.failures.push(error);

      // Not a provider failure — the caller stopped us. Surface it as itself.
      if (isCancellation(error)) throw error;

      if (!shouldFallback(error, ctx)) break;

      const next = candidates[index + 1];
      if (next === undefined || index + 1 >= limit) break;
      ctx.events.emit('fallback:advance', {
        callId: ctx.callId,
        fromProviderId: provider.id,
        toProviderId: next.id,
        reason: error.code,
        at: ctx.runtime.now(),
      });
    }
  }

  const identity = { callId: ctx.callId, capability: ctx.capability };

  if (errors.length === 0) {
    throw new NoProviderError(
      candidates.length === 0
        ? `No provider available for "${ctx.capability}"`
        : `No provider was tried for "${ctx.capability}" (maxProviders=${String(limit)})`,
      identity,
    );
  }

  const first = errors[0];
  // NEVER wrap a single failure: it buries the real error and breaks `code`.
  if (errors.length === 1 && first !== undefined) throw first;

  throw new AllProvidersFailedError(errors, identity);
}
