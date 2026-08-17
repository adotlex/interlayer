/**
 * THE CIRCUIT-BREAKER POLICY — the `Policy` wrapper around the pure reducer.
 *
 * All the state-machine logic lives in `state.ts`. This file does exactly four
 * things the reducer cannot: it reads the clock (through `ctx.runtime`), it
 * decides which errors count (`isFailure`), it emits `breaker:transition`, and
 * it throws `CircuitOpenError` when a call is not admitted.
 *
 * ONE BREAKER PER PROVIDER. State is keyed by `ctx.provider.id`. A single shared
 * machine would let a dead provider fail-fast a healthy one out of the same
 * fallback chain, which is the opposite of what a breaker is for. The key is
 * also what `CircuitOpenError` and the `breaker:transition` event both carry.
 *
 * THE ADMISSION INVARIANT (R4 §3.7, a hard implementation requirement): the
 * lazy `open -> half-open` check, the admission decision and the in-flight
 * increment all happen SYNCHRONOUSLY, before the first `await`. Node is
 * single-threaded, so that sequence is atomic with respect to other calls — but
 * only if nothing yields in the middle of it. Do not insert an `await` there.
 *
 * No timers. The `open -> half-open` transition is evaluated on the next call
 * (R4 §3.1), so this policy never holds the event loop open and `dispose()` has
 * nothing to cancel.
 */

import { CircuitOpenError } from '../../core/errors.ts';
import {
  type BreakerOptions,
  definePolicy,
  type Policy,
  type PolicyFactory,
  type ResolvedBreakerOptions,
} from '../../core/policy.ts';
import type { AttemptContext, Middleware } from '../../core/types.ts';
import {
  type BreakerData,
  type BreakerEvent,
  canAdmit,
  halfOpenAt,
  initialBreakerData,
  reduceBreaker,
  resolveBreakerOptions,
  windowTotals,
} from './state.ts';

/**
 * Counts every error as a failure — R4 §5.3 verbatim.
 *
 * `CircuitOpenError` is never seen by this predicate: a rejected call never ran,
 * so there is nothing to record (CB-25). Callers who want HTTP 404s and other
 * caller-fault errors excluded pass their own `isFailure`; an excluded error is
 * recorded as a SUCCESS and still rethrown (R4 §3.7, CB-20).
 */
export const DEFAULT_IS_FAILURE: (error: unknown) => boolean = () => true;

/**
 * Rolling time-window failure-ratio breaker with a minimum-throughput guard.
 *
 * Defaults (R4 §0.1): `failureRatio 0.5`, `minimumThroughput 10`,
 * `windowMs 30_000`, `bucketMs 1_000`, `resetMs 10_000`, one half-open probe,
 * one success to close.
 *
 * ```ts
 * const breaker: Policy = circuitBreaker({ minimumThroughput: 5 });
 * ```
 *
 * Throws `ConfigError` on invalid options (CB-27).
 */
export const circuitBreaker: PolicyFactory<BreakerOptions> = (
  options?: BreakerOptions,
): Policy<AttemptContext, unknown> => {
  const resolved: ResolvedBreakerOptions = resolveBreakerOptions(options);
  const isFailure = options?.isFailure ?? DEFAULT_IS_FAILURE;
  const states = new Map<string, BreakerData>();

  /** Applies one event, persists the result and emits the transition, if any. */
  function apply(key: string, event: BreakerEvent, now: number, ctx: AttemptContext): BreakerData {
    const before = states.get(key) ?? initialBreakerData();
    const after = reduceBreaker(before, event, now, resolved);
    states.set(key, after);
    if (after.state !== before.state) {
      ctx.events.emit('breaker:transition', {
        key,
        from: before.state,
        to: after.state,
        // The window is cleared by the transition itself, so the count is taken
        // from the state BEFORE it, plus the outcome that caused it. Both halves
        // matter: a trip on the 10th failure must report 10, and a trip on a
        // success (CB-6) must report the failures already in the window.
        failures: windowTotals(before, now, resolved).failures + (event.type === 'failure' ? 1 : 0),
        at: now,
      });
    }
    return after;
  }

  const execute: Middleware<AttemptContext, unknown> = async (ctx, next) => {
    const key = ctx.provider.id;
    const startedAt = ctx.runtime.now();

    /* -- SYNCHRONOUS ADMISSION. No `await` between here and the increment. -- */
    const observed = apply(key, { type: 'tick' }, startedAt, ctx);
    if (!canAdmit(observed, startedAt, resolved)) {
      // Fail fast without invoking the operation at all (CB-7).
      throw new CircuitOpenError(key, observed.openedAt, halfOpenAt(observed, resolved), {
        providerId: key,
        capability: ctx.capability,
        callId: ctx.callId,
        attempt: ctx.attempt,
      });
    }
    const admitted = apply(key, { type: 'admit' }, startedAt, ctx);
    const generation = admitted.generation;
    /* ----------------------- end of the atomic region ---------------------- */

    try {
      const value = await next();
      apply(key, { type: 'success', generation }, ctx.runtime.now(), ctx);
      return value;
    } catch (error) {
      // An error the predicate excludes is recorded as a success and rethrown
      // unchanged: a breaker must not trip on the caller's own bad input.
      const type = isFailure(error) ? 'failure' : 'success';
      apply(key, { type, generation }, ctx.runtime.now(), ctx);
      throw error;
    }
  };

  return definePolicy<AttemptContext, unknown>({
    kind: 'circuit-breaker',
    name: 'circuit-breaker',
    scope: 'attempt',

    execute,

    /**
     * Last OBSERVED state per key. Side-effect free, and therefore unable to
     * apply the lazy `open -> half-open` transition — it takes no `now`. A key
     * can read `'open'` here while the very next call would be admitted as a
     * probe. R4 §3.1 calls this out explicitly; use `halfOpenAt` to interpret it.
     */
    describe: (): Readonly<Record<string, unknown>> => ({
      kind: 'circuit-breaker',
      options: resolved,
      keys: Object.fromEntries(
        [...states].map(([key, data]) => [
          key,
          {
            state: data.state,
            openedAt: data.openedAt,
            halfOpenAt: halfOpenAt(data, resolved),
            halfOpenInFlight: data.halfOpenInFlight,
            halfOpenSuccesses: data.halfOpenSuccesses,
            consecutiveFailures: data.consecutiveFailures,
            generation: data.generation,
            bucketCount: data.buckets.length,
          },
        ]),
      ),
    }),

    /** Idempotent. There is nothing async to release — the breaker owns no timer. */
    dispose: (): void => {
      states.clear();
    },
  });
};
