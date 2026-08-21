/**
 * `cause` CHAINS, END TO END.
 *
 * Five layers of wrapping sit between a provider handler and the caller —
 * attempt timeout, breaker, rate limit, retry, fallback — and every one of them
 * is an opportunity to throw a fresh error and lose the original. That loss is
 * invisible: the call still fails with a plausible-looking code, and the only
 * symptom is an on-call engineer who cannot tell WHY.
 *
 * So: the error a handler threw must still be reachable from what the caller
 * catches, by identity (`toBe`, not `toEqual`), after the whole stack.
 */

import { describe, expect, it } from 'vitest';
import { formatErrorChain, hasCode, TransportError } from '../../src/core/errors.ts';
import type { InterlayerEvents, ResilienceOptions } from '../../src/index.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import {
  causes,
  codeChain,
  drainMicrotasks,
  NO_POLICIES,
  probe,
  rejectionOf,
  type Say,
  settle,
} from './harness.ts';

/** Every policy on, but tuned so the suite settles in virtual microseconds. */
const FULL_STACK: ResilienceOptions = {
  retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 50 },
  rateLimit: { capacity: 10, refillPerSec: 10 },
  breaker: { minimumThroughput: 10 },
  timeout: { attemptTimeoutMs: 5_000, totalTimeoutMs: 60_000 },
};

/** A throw the retry predicate classifies as transient, so retries actually happen. */
function transientRaw(message: string): Error {
  return Object.assign(new Error(message), { status: 503 });
}

/**
 * Reads one key off a `toJSON()` projection.
 *
 * `Record<string, unknown>` is an index signature, so `noPropertyAccessFromIndexSignature`
 * demands `json['code']` while Biome's `useLiteralKeys` demands `json.code`. A
 * non-literal key satisfies both.
 */
function field(record: Record<string, unknown>, key: string): unknown {
  return record[key];
}

describe('cause chains survive the full stack', () => {
  it('the original handler throw is reachable through retry AND fallback wrapping', async () => {
    const runtime = createFakeRuntime();
    const thrown: Error[] = [];
    const scripted = (id: string) => {
      let n = 0;
      return async (): Promise<Say> => {
        n++;
        const raw = transientRaw(`${id}#${String(n)}`);
        thrown.push(raw);
        throw raw;
      };
    };
    const a = defineProvider(probe, { id: 'a', capabilities: { run: scripted('a') } });
    const b = defineProvider(probe, { id: 'b', capabilities: { run: scripted('b') } });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      resilience: FULL_STACK,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // Four physical throws: two providers x two attempts each.
    expect(thrown.map((e) => e.message)).toEqual(['a#1', 'a#2', 'b#1', 'b#2']);

    // The chain the caller sees, outermost first.
    expect(codeChain(error)).toEqual(['ALL_FAILED', 'RETRY_EXHAUSTED', 'Error']);
    expect(hasCode(error, 'ALL_FAILED')).toBe(true);
    if (!hasCode(error, 'ALL_FAILED')) return;

    // `cause` follows the LAST provider all the way down to the raw throw.
    const chain = causes(error);
    expect(chain[0]).toBe(error.failures[1]);
    expect(chain[1]).toBe(thrown[3]);

    // Nothing is lost: every one of the four originals is still reachable.
    const reachable = error.failures.flatMap((failure) =>
      hasCode(failure, 'RETRY_EXHAUSTED') ? [...failure.errors] : [failure],
    );
    expect(reachable).toEqual(thrown);
    expect(reachable[0]).toBe(thrown[0]);
    expect(reachable[3]).toBe(thrown[3]);

    // And the whole thing renders for a log line without any of it being lost.
    const rendered = formatErrorChain(error);
    expect(rendered).toContain('AllProvidersFailedError');
    expect(rendered).toContain('RetryExhaustedError');
    expect(rendered).toContain('b#2');
    await layer.close();
  });

  it('one provider, one non-retryable throw, all four policies on -> cause is the exact object', async () => {
    const runtime = createFakeRuntime();
    const root = new Error('the actual bug');
    const only = defineProvider(probe, {
      id: 'only',
      capabilities: {
        run: async (): Promise<Say> => {
          throw root;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [only],
      runtime,
      resilience: FULL_STACK,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // No aggregate anywhere: one attempt, one provider, one error, wrapped once
    // by the normalisation funnel and no more.
    expect(codeChain(error)).toEqual(['PROVIDER_ERROR', 'Error']);
    expect(error.cause).toBe(root);
    await layer.close();
  });

  it('a cause the HANDLER attached survives the router stamping identity onto the error', async () => {
    const runtime = createFakeRuntime();
    const root = new Error('ECONNRESET at socket');
    const thrown = new TransportError('upstream unreachable', { cause: root });
    const bare = defineProvider(probe, {
      id: 'bare',
      capabilities: {
        run: async (): Promise<Say> => {
          throw thrown;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [bare],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // `withContext` must COPY (the thrower knew none of this), and the copy is
    // where a naive implementation drops `cause`, `stack` and the prototype.
    expect(error).not.toBe(thrown);
    expect(error).toBeInstanceOf(TransportError);
    expect(error.providerId).toBe('bare');
    expect(error.capability).toBe('run');
    expect(error.callId).toBeDefined();
    expect(error.cause).toBe(root);
    expect(error.message).toBe('upstream unreachable');
    await layer.close();
  });

  /**
   * ── FAILING ON PURPOSE — DEFECT #1, `InterlayerError.withContext()` ──────
   *
   * `withContext` documents that it "Preserves prototype (so `instanceof` and
   * the brand survive), `stack`, `message` and `cause`". It preserves three of
   * those four. `stack` is silently lost, and the copy's `stack` is
   * `undefined` — not stale, not truncated: absent.
   *
   * WHY: V8 installs `stack` as a lazy own ACCESSOR whose getter resolves the
   * structured trace from its RECEIVER. Copying the descriptor onto a fresh
   * `Object.create(proto)` re-binds the receiver to an object V8 never captured
   * a trace for, so the getter answers `undefined`. Verified directly:
   * `Object.getOwnPropertyDescriptor(err, 'stack')` is `{get, set}`, never
   * `{value}`, with or without `Error.captureStackTrace`.
   *
   * WHY IT MATTERS: the copy branch is the NORMAL path, not an edge case. The
   * class comment says so — "providers legitimately throw
   * `new TransportError('down')` with no idea of their own id; the router
   * stamps identity on the way out". Every such throw reaches the caller with
   * no stack at all, so the one artefact that says WHERE the failure came from
   * is destroyed by the code whose job is to add context to it.
   *
   * A fix has to re-read `stack` as a VALUE before the copy (or run
   * `Error.captureStackTrace(next)` and overwrite it), not clone the
   * descriptor. NOT FIXED HERE: `src/**` is read-only to this wave.
   */
  it('[KNOWN BUG] withContext() drops the stack trace it documents that it preserves', async () => {
    const runtime = createFakeRuntime();
    const thrown = new TransportError('upstream unreachable');
    const bare = defineProvider(probe, {
      id: 'bare',
      capabilities: {
        run: async (): Promise<Say> => {
          throw thrown;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [bare],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(error).not.toBe(thrown); // the copy branch really was taken
    expect(typeof thrown.stack).toBe('string'); // the original has one
    expect(error.stack).toBe(thrown.stack); // FAILS: the copy's stack is undefined
    await layer.close();
  });

  it('a cause survives a rate-limit wait sitting between retry and the handler', async () => {
    const runtime = createFakeRuntime();
    const thrown: Error[] = [];
    const throttled = defineProvider(probe, {
      id: 'throttled',
      capabilities: {
        run: async (): Promise<Say> => {
          const raw = transientRaw(`attempt ${String(thrown.length + 1)}`);
          thrown.push(raw);
          throw raw;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [throttled],
      runtime,
      resilience: {
        breaker: false,
        timeout: false,
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 0 },
        // One token: the retry has to queue for a refill before it may run.
        rateLimit: { capacity: 1, refillPerSec: 10 },
      },
    });
    const throttles: InterlayerEvents['ratelimit:throttled'][] = [];
    layer.on('ratelimit:throttled', (payload) => {
      throttles.push(payload);
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // The queue really was entered, so the error crossed it.
    expect(throttles).toHaveLength(1);
    expect(throttles[0]?.waitMs).toBe(100);
    expect(runtime.now()).toBe(100);

    expect(codeChain(error)).toEqual(['RETRY_EXHAUSTED', 'Error']);
    expect(error.cause).toBe(thrown[1]);
    if (!hasCode(error, 'RETRY_EXHAUSTED')) return;
    expect(error.errors[0]).toBe(thrown[0]);
    expect(error.errors[1]).toBe(thrown[1]);
    await layer.close();
  });

  it('a suppressed failure keeps its cause on the SUCCESS path too', async () => {
    const runtime = createFakeRuntime();
    const root = new Error('first backend is down');
    const bad = defineProvider(probe, {
      id: 'bad',
      capabilities: {
        run: async (): Promise<Say> => {
          throw root;
        },
      },
    });
    const good = defineProvider(probe, {
      id: 'good',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'good' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [bad, good],
      runtime,
      resilience: NO_POLICIES,
    });

    const meta = await settle(runtime, layer.callWithMeta('run', { q: 'x' }));

    expect(meta.providerId).toBe('good');
    // A fallback never swallows a cause, even when the call ultimately succeeds.
    expect(meta.errors).toHaveLength(1);
    expect(meta.errors[0]?.code).toBe('PROVIDER_ERROR');
    expect(meta.errors[0]?.providerId).toBe('bad');
    expect(meta.errors[0]?.cause).toBe(root);
    await layer.close();
  });
});

describe('cause projection for logs', () => {
  it('toJSON() renders the cause without ever leaking a stack', async () => {
    const runtime = createFakeRuntime();
    const root = new Error('root cause');
    const a = defineProvider(probe, {
      id: 'a',
      capabilities: {
        run: async (): Promise<Say> => {
          throw root;
        },
      },
    });
    const b = defineProvider(probe, {
      id: 'b',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new TransportError('b down');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));
    const json = error.toJSON();

    expect(field(json, 'code')).toBe('ALL_FAILED');
    expect(json).not.toHaveProperty('stack');
    // An Interlayer cause is projected recursively…
    const cause = field(json, 'cause') as Record<string, unknown>;
    expect(field(cause, 'code')).toBe('TRANSPORT');
    expect(cause).not.toHaveProperty('stack');

    if (!hasCode(error, 'ALL_FAILED')) {
      expect.unreachable('expected ALL_FAILED');
      return;
    }
    // …and a foreign one down to name + message only.
    const first = error.failures[0];
    expect(first === undefined ? undefined : field(first.toJSON(), 'cause')).toEqual({
      name: 'Error',
      message: 'root cause',
    });
    await layer.close();
  });
});

describe('where the chain deliberately stops', () => {
  it('a handler that ignores its signal loses the race, and its error goes to the event stream', async () => {
    const runtime = createFakeRuntime();
    const late = new Error('arrived after the deadline');
    // Deliberately does NOT pass `ctx.signal` to sleep: this is the provider
    // that keeps working after the caller has been told the attempt timed out.
    const deaf = defineProvider(probe, {
      id: 'deaf',
      capabilities: {
        run: async (_input, ctx): Promise<Say> => {
          await ctx.runtime.sleep(500);
          throw late;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [deaf],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 200 },
      },
    });
    const failures: InterlayerEvents['attempt:failure'][] = [];
    layer.on('attempt:failure', (payload) => {
      failures.push(payload);
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    expect(runtime.now()).toBe(200);
    // DOCUMENTED LIMIT: the orphaned work's error cannot be a `cause` of an
    // error that was thrown 300ms before it existed. The chain stops here.
    expect(error.cause).toBeUndefined();
    expect(failures).toHaveLength(0);

    // …but it is not destroyed. When the orphan finally settles it is reported
    // on `attempt:failure`, and it never becomes an unhandled rejection.
    await runtime.advance(400);
    await drainMicrotasks();
    expect(failures).toHaveLength(1);
    expect(failures[0]?.error.cause).toBe(late);
    await layer.close();
  });
});
