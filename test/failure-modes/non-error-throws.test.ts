/**
 * WHAT A PROVIDER CAN THROW, AND WHAT THE CALLER GETS.
 *
 * `throw` accepts any value in JavaScript and real backends exercise most of
 * them: a string from a hand-rolled client, a bare `{ status, body }` from an
 * HTTP wrapper, `undefined` from a `reject()` with no argument, a `DOMException`
 * from `fetch`. `toInterlayerError` is the single funnel that turns all of it
 * into a typed error with the original preserved as `cause`, and the facade
 * promises the caller an `InterlayerError` subclass ALWAYS.
 *
 * This file throws the awkward values at it and reads back what a caller
 * catches. Two of them come back wrong; both are marked and left failing.
 */

import { describe, expect, it } from 'vitest';
import { hasCode } from '../../src/core/errors.ts';
import type { InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime, type FakeRuntime } from '../support/index.ts';
import {
  expectNoLeakedTimers,
  NO_POLICIES,
  neverSettles,
  probe,
  rejectionOf,
  rejects,
  type Say,
  settle,
  throwsSynchronously,
} from './harness.ts';

/** One provider whose handler throws `thrown`, with every policy switched off. */
function layerThrowing(runtime: FakeRuntime, thrown: unknown) {
  const bad = defineProvider(probe, {
    id: 'bad',
    capabilities: {
      run: async (): Promise<Say> => {
        throw thrown;
      },
    },
  });
  return createLayer({ contract: probe, providers: [bad], runtime, resilience: NO_POLICIES });
}

/* ------------------------------------------------------------------ *
 * 1. Primitives
 * ------------------------------------------------------------------ */

describe('non-Error throws are normalised, never leaked raw', () => {
  it('a string becomes PROVIDER_ERROR carrying the string as both message and cause', async () => {
    const runtime = createFakeRuntime();
    const layer = layerThrowing(runtime, 'connection reset by peer');

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('connection reset by peer');
    expect(error.cause).toBe('connection reset by peer');
    expect(error).toBeInstanceOf(Error); // a real Error, so try/catch and logs work
    await layer.close();
  });

  it('a number becomes PROVIDER_ERROR with the number stringified and kept as cause', async () => {
    const runtime = createFakeRuntime();
    const layer = layerThrowing(runtime, 503);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('503');
    expect(error.cause).toBe(503);
    // A bare number is not an HTTP status: only `status`/`statusCode` on an
    // object are, so this must NOT be classified as retryable.
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  it('null becomes PROVIDER_ERROR and null survives as the cause', async () => {
    const runtime = createFakeRuntime();
    const layer = layerThrowing(runtime, null);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('null');
    expect(error.cause).toBeNull();
    await layer.close();
  });

  it('undefined becomes PROVIDER_ERROR; the cause is unrecoverable and says so', async () => {
    const runtime = createFakeRuntime();
    const layer = layerThrowing(runtime, undefined);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('undefined');
    // DOCUMENTED LIMIT: `cause: undefined` is indistinguishable from "no
    // cause", so `throw undefined` is the one value the funnel cannot round
    // trip. The message is the only surviving evidence — and it is enough to
    // recognise the shape of the bug in the provider.
    expect(error.cause).toBeUndefined();
    expect(Object.hasOwn(error, 'cause')).toBe(false);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Non-Error objects
 * ------------------------------------------------------------------ */

describe('non-Error objects', () => {
  it('a bare { status } object is classified from its status and kept as cause', async () => {
    const runtime = createFakeRuntime();
    const raw = { status: 503, body: 'service unavailable' };
    const layer = layerThrowing(runtime, raw);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    if (!hasCode(error, 'PROVIDER_ERROR')) return;
    expect(error.status).toBe(503);
    expect(error.retryable).toBe(true);
    expect(error.cause).toBe(raw); // nothing is lost: the whole object is there
    await layer.close();
  });

  it('statusCode is honoured as well as status, and 4xx is not retryable', async () => {
    const runtime = createFakeRuntime();
    const layer = layerThrowing(runtime, { statusCode: 400, body: 'bad request' });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    if (!hasCode(error, 'PROVIDER_ERROR')) {
      expect.unreachable('expected PROVIDER_ERROR');
      return;
    }
    expect(error.status).toBe(400);
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  it('a platform AbortError is read as a caller withdrawal', async () => {
    const runtime = createFakeRuntime();
    // What `fetch` rejects with when the signal we handed it was aborted.
    const layer = layerThrowing(
      runtime,
      new DOMException('This operation was aborted', 'AbortError'),
    );

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'CANCELLED')).toBe(true);
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  /**
   * ── FAILING ON PURPOSE — DEFECT #2, `toInterlayerError` / `isAbortLike` ──
   *
   * `isAbortLike()` treats a foreign error NAMED `TimeoutError` as an abort and
   * returns a `CancelledError`. But a `DOMException` named `TimeoutError` is
   * exactly what `fetch(url, { signal: AbortSignal.timeout(ms) })` rejects with
   * — i.e. the PROVIDER's own upstream deadline, not the caller hanging up.
   *
   * The relabel is the one R4 §2.3 exists to prevent, and its cost is the worst
   * in the taxonomy, because `CANCELLED` is the only code that is BOTH
   * non-retryable AND in `NO_FALLBACK_CODES`:
   *
   *   - the retry loop refuses to try again (`isTransient` rule 1),
   *   - `runFallbackChain` throws it immediately as a caller withdrawal,
   *     without so much as consulting `shouldFallback`,
   *   - so a healthy alternate sits unused while the call fails.
   *
   * That is precisely the outage the `defaultShouldFallback` comment describes,
   * arriving through a different door. `TimeoutError` belongs on the TIMEOUT
   * side of the funnel (retryable, falls back), leaving `isAbortLike` to mean
   * `AbortError` alone. The message is collateral damage: the surfaced error
   * reads "Aborted", and the real one survives only on `.cause`.
   *
   * NOT FIXED HERE: `src/**` is read-only to this wave.
   */
  it('[KNOWN BUG] a provider-internal fetch timeout is relabelled CANCELLED and strands the chain', async () => {
    const runtime = createFakeRuntime();
    let backupCalls = 0;
    const upstream = defineProvider(probe, {
      id: 'upstream',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new DOMException('The operation was aborted due to timeout', 'TimeoutError');
        },
      },
    });
    const backup = defineProvider(probe, {
      id: 'backup',
      capabilities: {
        run: async (): Promise<Say> => {
          backupCalls++;
          return { a: 'ok', by: 'backup' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [upstream, backup],
      runtime,
      // Retry OFF deliberately: with it on, `isTransient` rule 5 retries the
      // raw throw and the RETRY_EXHAUSTED wrapper hides the misclassification.
      resilience: NO_POLICIES,
    });

    // FAILS: resolves to CANCELLED with `backupCalls === 0`.
    const reply = await settle(runtime, layer.call('run', { q: 'x' }));

    expect(reply.by).toBe('backup');
    expect(backupCalls).toBe(1);
    await layer.close();
  });

  /**
   * ── FAILING ON PURPOSE — DEFECT #3, `toInterlayerError` is not total ─────
   *
   * The funnel is documented as "every non-Interlayer throw becomes a typed
   * error, cause preserved". It computes `String(e)` for anything that is not
   * an `Error`, and `String()` THROWS on an object with no prototype:
   * `TypeError: Cannot convert object to primitive value`.
   *
   * The funnel therefore throws from inside `runFallbackChain`'s own catch
   * block, which:
   *   - destroys the fallback chain (a healthy alternate is never tried),
   *   - never records the failure on `ctx.failures`,
   *   - and hands the caller a `PROVIDER_ERROR` describing the NORMALISER's
   *     failure — "Cannot convert object to primitive value" — instead of the
   *     provider's.
   *
   * A null-prototype object is not exotic: `Object.create(null)` is the
   * standard shape for a safe dictionary, and anything built that way can end
   * up thrown. The fix is a guarded stringify; the normalisation funnel of an
   * error taxonomy has to be total.
   *
   * NOT FIXED HERE: `src/**` is read-only to this wave.
   */
  it('[KNOWN BUG] a null-prototype throw breaks the normalisation funnel itself', async () => {
    const runtime = createFakeRuntime();
    let backupCalls = 0;
    const weird = defineProvider(probe, {
      id: 'weird',
      capabilities: {
        run: async (): Promise<Say> => {
          const bag: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
          bag['reason'] = 'upstream refused';
          throw bag;
        },
      },
    });
    const backup = defineProvider(probe, {
      id: 'backup',
      capabilities: {
        run: async (): Promise<Say> => {
          backupCalls++;
          return { a: 'ok', by: 'backup' };
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [weird, backup],
      runtime,
      resilience: NO_POLICIES,
    });

    // FAILS: the funnel throws a TypeError, the chain dies, `backupCalls === 0`.
    const reply = await settle(runtime, layer.call('run', { q: 'x' }));

    expect(reply.by).toBe('backup');
    expect(backupCalls).toBe(1);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Synchronous throw vs rejected promise
 * ------------------------------------------------------------------ */

describe('a synchronous throw and a rejected promise are indistinguishable', () => {
  it('both surface the same code, message, cause and events', async () => {
    const runtime = createFakeRuntime();
    const rootSync = new Error('same failure');
    const rootAsync = new Error('same failure');

    const syncProvider = defineProvider(probe, {
      id: 'p',
      capabilities: { run: throwsSynchronously(rootSync) },
    });
    const asyncProvider = defineProvider(probe, {
      id: 'p',
      capabilities: { run: rejects(rootAsync) },
    });

    const collect = async (
      provider: typeof syncProvider | typeof asyncProvider,
      root: Error,
    ): Promise<Record<string, unknown>> => {
      const rt = createFakeRuntime();
      const layer = createLayer({
        contract: probe,
        providers: [provider],
        runtime: rt,
        resilience: NO_POLICIES,
      });
      const failures: InterlayerEvents['attempt:failure'][] = [];
      layer.on('attempt:failure', (payload) => {
        failures.push(payload);
      });
      const error = await rejectionOf(rt, layer.call('run', { q: 'x' }));
      await layer.close();
      return {
        code: error.code,
        message: error.message,
        providerId: error.providerId,
        causeIsRoot: error.cause === root,
        attemptFailures: failures.length,
        attempt: failures[0]?.attempt,
      };
    };

    const fromSync = await collect(syncProvider, rootSync);
    const fromAsync = await collect(asyncProvider, rootAsync);

    // A handler that throws before it ever returns a promise must not be a
    // different kind of failure from one that rejects.
    expect(fromSync).toEqual(fromAsync);
    expect(fromSync['code']).toBe('PROVIDER_ERROR');
    expect(fromSync['causeIsRoot']).toBe(true);
    expect(fromSync['attemptFailures']).toBe(1);
    expect(runtime.now()).toBe(0);
  });

  it('a synchronous NON-Error throw is normalised the same way too', async () => {
    const runtime = createFakeRuntime();
    const sync = defineProvider(probe, {
      id: 'sync',
      capabilities: { run: throwsSynchronously('exploded before returning') },
    });
    const layer = createLayer({
      contract: probe,
      providers: [sync],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('exploded before returning');
    expect(error.providerId).toBe('sync');
    await layer.close();
  });

  it('a synchronous throw still advances the fallback chain', async () => {
    const runtime = createFakeRuntime();
    const explodes = defineProvider(probe, {
      id: 'explodes',
      capabilities: { run: throwsSynchronously(new TypeError('x is not a function')) },
    });
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'healthy' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [explodes, healthy],
      runtime,
      resilience: NO_POLICIES,
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('healthy');
    expect(meta.errors[0]?.code).toBe('PROVIDER_ERROR');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. A handler that never settles
 * ------------------------------------------------------------------ */

describe('a provider that never settles', () => {
  it('is bounded by the attempt timeout, enters once, and leaves no timer behind', async () => {
    const runtime = createFakeRuntime();
    let entries = 0;
    const hung = defineProvider(probe, {
      id: 'hung',
      capabilities: {
        run: (): Promise<Say> => {
          entries++;
          return neverSettles()();
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [hung],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 300, totalTimeoutMs: 60_000 },
      },
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('attempt');
    expect(entries).toBe(1);
    expect(runtime.now()).toBe(300);
    // The orphaned work is the operation's problem; the caller's deadline is
    // not, and neither timer may outlive the call.
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('falls back to a healthy provider once its attempt budget is spent', async () => {
    const runtime = createFakeRuntime();
    const hung = defineProvider(probe, { id: 'hung', capabilities: { run: neverSettles() } });
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'healthy' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [hung, healthy],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 300, totalTimeoutMs: 60_000 },
      },
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('healthy');
    expect(meta.errors).toHaveLength(1);
    expect(meta.errors[0]?.code).toBe('TIMEOUT');
    expect(runtime.now()).toBe(300);
    await expectNoLeakedTimers(runtime);
    await layer.close();
  });

  it('with no timeout policy and no deadline the call is genuinely unbounded', async () => {
    const runtime = createFakeRuntime();
    const hung = defineProvider(probe, { id: 'hung', capabilities: { run: neverSettles() } });
    const layer = createLayer({
      contract: probe,
      providers: [hung],
      runtime,
      resilience: NO_POLICIES,
    });

    let settled = false;
    const call = layer.call('run', { q: 'x' }).then(
      () => {
        settled = true;
      },
      () => {
        settled = true;
      },
    );

    // `settle` gives up when no timer is left to move — which is exactly the
    // point: nothing is watching, so nothing will ever end this call. That is
    // the documented consequence of switching every policy off, and the reason
    // `timeout: false` is a decision rather than a default.
    await settle(runtime, call);
    expect(settled).toBe(false);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});
