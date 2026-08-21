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
  drainMicrotasks,
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
   * error, cause preserved". It reaches for `String(e)` on anything that is not
   * an `Error`, and `String()` THROWS on an object with no prototype:
   * `TypeError: Cannot convert object to primitive value`. `Object.create(null)`
   * is the standard shape for a safe dictionary, so this is reachable from
   * ordinary code.
   *
   * The blast radius is smaller than it looks — `runHandler`'s rejection
   * handler is already inside a promise, so the `TypeError` becomes a rejection
   * and the chain still falls back — but the funnel's own contract is broken in
   * two visible ways:
   *
   *   - the surfaced failure describes the NORMALISER ("Cannot convert object
   *     to primitive value"), pointing an operator at a `TypeError` inside the
   *     library instead of at the provider that failed;
   *   - `cause` holds that `TypeError`, so the value the provider actually
   *     threw is gone. Nothing else in the pipeline kept a reference to it.
   *
   * The `attempt:failure` event is lost with it: `toInterlayerError` throws on
   * the line ABOVE the `events.emit`, so this attempt emits `attempt:start`
   * and nothing else. A guarded stringify fixes all three at once — the
   * normalisation funnel of an error taxonomy has to be total.
   *
   * NOT FIXED HERE: `src/**` is read-only to this wave.
   */
  it('[KNOWN BUG] a null-prototype throw loses the original value and its attempt event', async () => {
    const runtime = createFakeRuntime();
    // A prototype-less bag: the standard shape for a safe dictionary, and the
    // one object `String()` refuses to convert.
    const bag = Object.assign(Object.create(null) as object, { reason: 'upstream refused' });

    const weird = defineProvider(probe, {
      id: 'weird',
      capabilities: {
        run: async (): Promise<Say> => {
          throw bag;
        },
      },
    });
    const backup = defineProvider(probe, {
      id: 'backup',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'backup' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [weird, backup],
      runtime,
      resilience: NO_POLICIES,
    });
    const attemptFailures: InterlayerEvents['attempt:failure'][] = [];
    layer.on('attempt:failure', (payload) => {
      attemptFailures.push(payload);
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    // The chain does survive: the normaliser's TypeError is itself normalised
    // one level up and reads as an ordinary PROVIDER_ERROR.
    expect(meta.providerId).toBe('backup');
    expect(meta.errors).toHaveLength(1);

    // FAILS: `cause` is the normaliser's TypeError, not the thrown value.
    expect(meta.errors[0]?.cause).toBe(bag);
    // FAILS: no `attempt:failure` was emitted for the attempt that failed.
    expect(attemptFailures).toHaveLength(1);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Synchronous throw vs rejected promise
 * ------------------------------------------------------------------ */

/** What a caller and an observer see for one failing call. */
interface Observed {
  readonly code: string;
  readonly message: string;
  readonly providerId: string | undefined;
  readonly causeIsRoot: boolean;
  readonly attemptStarts: number;
  readonly attemptFailures: number;
}

async function observe(handler: () => Promise<Say>, root: unknown): Promise<Observed> {
  const runtime = createFakeRuntime();
  const provider = defineProvider(probe, { id: 'p', capabilities: { run: handler } });
  const layer = createLayer({
    contract: probe,
    providers: [provider],
    runtime,
    resilience: NO_POLICIES,
  });
  const starts: InterlayerEvents['attempt:start'][] = [];
  const failures: InterlayerEvents['attempt:failure'][] = [];
  layer.on('attempt:start', (payload) => {
    starts.push(payload);
  });
  layer.on('attempt:failure', (payload) => {
    failures.push(payload);
  });

  const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));
  await layer.close();
  return {
    code: error.code,
    message: error.message,
    providerId: error.providerId,
    causeIsRoot: error.cause === root,
    attemptStarts: starts.length,
    attemptFailures: failures.length,
  };
}

describe('a synchronous throw and a rejected promise are indistinguishable', () => {
  it('both surface the same code, message, provider and cause', async () => {
    const rootSync = new Error('same failure');
    const rootAsync = new Error('same failure');

    const fromSync = await observe(throwsSynchronously(rootSync), rootSync);
    const fromAsync = await observe(rejects(rootAsync), rootAsync);

    // A handler that throws before it ever returns a promise must not be a
    // different KIND of failure from one that rejects.
    expect(fromSync.code).toBe(fromAsync.code);
    expect(fromSync.message).toBe(fromAsync.message);
    expect(fromSync.providerId).toBe(fromAsync.providerId);
    expect(fromSync.causeIsRoot).toBe(true);
    expect(fromAsync.causeIsRoot).toBe(true);
    expect(fromSync.code).toBe('PROVIDER_ERROR');
  });

  /**
   * ── FAILING ON PURPOSE — DEFECT #4, `runHandler` in `src/layer.ts` ───────
   *
   * `runHandler` emits `attempt:start`, then calls the handler and attaches its
   * outcome events with `.then(onSuccess, onFailure)`. A handler that throws
   * SYNCHRONOUSLY never gets as far as `.then`, so the attempt emits
   * `attempt:start` and then nothing at all: no `attempt:success`, no
   * `attempt:failure`, no `durationMs`, no `willRetry`.
   *
   * The call itself is fine — `compose()` turns the synchronous throw into a
   * rejection at the terminal and the fallback chain behaves normally — so this
   * is invisible except through the event stream, which is exactly where it
   * does damage: every latency histogram, error-rate metric and trace span
   * built on the start/finish pair silently loses these attempts, and an
   * `attempt:start` with no partner leaks a span that never closes.
   *
   * A synchronous throw is not an exotic handler shape. Any non-`async`
   * function that validates before it dispatches — `if (!apiKey) throw …` —
   * does this, and the rest of the library already anticipates it: `compose()`
   * wraps its terminal in `try/catch` for this reason, and `runWithTimeout`
   * wraps `next()` in `Promise.resolve().then(…)` with a comment saying so.
   * `runHandler` is the one place that does not.
   *
   * NOT FIXED HERE: `src/**` is read-only to this wave.
   */
  it('[KNOWN BUG] a synchronous throw emits attempt:start with no matching outcome event', async () => {
    const rootSync = new Error('same failure');
    const rootAsync = new Error('same failure');

    const fromSync = await observe(throwsSynchronously(rootSync), rootSync);
    const fromAsync = await observe(rejects(rootAsync), rootAsync);

    expect(fromAsync.attemptStarts).toBe(1);
    expect(fromAsync.attemptFailures).toBe(1);

    expect(fromSync.attemptStarts).toBe(1);
    // FAILS: 0. The attempt started and never finished, as far as any observer
    // can tell.
    expect(fromSync.attemptFailures).toBe(1);
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
    call.catch(() => undefined);

    // Deliberately NOT awaited: there is nothing to await. No policy is
    // watching and no timer exists to fire, so this call never ends. That is
    // the documented consequence of switching every policy off — the orphaned
    // work is the operation's problem — and it is why `timeout: false` is a
    // decision rather than a default.
    for (let i = 0; i < 5; i++) await drainMicrotasks();
    expect(settled).toBe(false);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });
});
