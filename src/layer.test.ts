/**
 * `createLayer` end to end — the integration suite.
 *
 * Everything here runs on `createFakeRuntime()`. NO TEST SLEEPS: a 30-second
 * total timeout and a 10-second breaker cooldown both settle in microseconds of
 * wall time, and every assertion about time is an assertion about an exact
 * virtual instant.
 *
 * The last block builds R6 §5.2's published usage example as executable code.
 * It is the acceptance criterion for the whole design, and where it could not
 * be written verbatim the deviation is named in a comment right there.
 */

import { describe, expect, it } from 'vitest';
import { createFakeRuntime, type FakeRuntime, fakeProvider } from '../test/support/index.ts';
import {
  type AnyInterlayerError,
  hasCode,
  isInterlayerError,
  ProviderError,
  TransportError,
} from './core/errors.ts';
import type { CallMiddleware, InterlayerEvents } from './core/types.ts';
import { createLayer } from './layer.ts';
import { capability, defineContract, defineProvider } from './registry/index.ts';

/* ------------------------------------------------------------------ *
 * Fixtures
 * ------------------------------------------------------------------ */

interface ChatRequest {
  readonly prompt: string;
}
interface ChatReply {
  readonly text: string;
  readonly by: string;
}
interface EmbedRequest {
  readonly texts: readonly string[];
}
interface EmbedReply {
  readonly vectors: readonly number[][];
}

// `chat` is declared IDEMPOTENT here on purpose. `createLayer` now reads
// `capability({ idempotent })` and wires it into the retry policy's
// idempotency gate (R6 §2 rule 3: "the layer never retries a non-idempotent
// capability unless told to"), so a contract that declares `false` and a test
// that then asserts three attempts cannot both be right. This fixture used to
// say `false` only because nothing read the flag. Every assertion below is
// unchanged; the contract is what was wrong.
const ai = defineContract({
  chat: capability<ChatRequest, ChatReply>({ idempotent: true }),
  embed: capability<EmbedRequest, EmbedReply>({ idempotent: true }),
});

/** A scripted handler: throws the first `failures` errors, then succeeds. */
function flaky(
  id: string,
  failures: number,
  error: () => unknown = () => new TransportError('down'),
) {
  let seen = 0;
  return {
    get calls(): number {
      return seen;
    },
    handler: async (input: ChatRequest): Promise<ChatReply> => {
      seen++;
      if (seen <= failures) throw error();
      return { text: input.prompt.toUpperCase(), by: id };
    },
  };
}

/**
 * Drives the virtual clock until `promise` settles, ONE DUE INSTANT AT A TIME.
 *
 * Retry backoff, rate-limit waits and every timeout are virtual timers; without
 * something turning the crank they never fire and the awaited call hangs until
 * `testTimeout`. But `runAll()` is the wrong crank: it drains every timer
 * present when it is called, so a 250 ms provider sleep and the 30 s total
 * timeout sitting behind it both fire in one drain and the call "times out"
 * after succeeding. Stepping to `clock.nextTimerAt` fires them in real due
 * order, stops the moment the promise settles, and leaves `runtime.now()` on
 * an exact instant so the assertions below can be equalities.
 */
async function settle<T>(runtime: FakeRuntime, promise: Promise<T>): Promise<T> {
  let done = false;
  const tracked = promise.then(
    (value) => {
      done = true;
      return value;
    },
    (error: unknown) => {
      done = true;
      throw error;
    },
  );
  tracked.catch(() => undefined); // never an unhandled rejection while pumping

  for (let guard = 0; guard < 500 && !done; guard++) {
    await drainMicrotasks();
    if (done) break;
    const next = runtime.clock.nextTimerAt;
    if (next === undefined) break; // nothing left to move; it must be resolving
    await runtime.advance(Math.max(0, next - runtime.now()));
  }
  return tracked;
}

/**
 * Empties the microtask queue completely.
 *
 * `await Promise.resolve()` advances it by one tick, and the pipeline is deep
 * enough — compose, four policies, the router, the emitter — that counting
 * ticks is guesswork. A `setImmediate` turn is a real macrotask, so every
 * microtask queued ahead of it has already run by the time it fires. This is
 * the ONE real timer in this file and it is not the clock under test.
 */
function drainMicrotasks(): Promise<void> {
  return new Promise<void>((resolve) => {
    setImmediate(resolve);
  });
}

/** The rejection of a call, as a typed error. Fails loudly if it resolved. */
async function rejectionOf(
  runtime: FakeRuntime,
  promise: Promise<unknown>,
): Promise<AnyInterlayerError> {
  try {
    const value = await settle(runtime, promise);
    expect.unreachable(`expected a rejection, got ${JSON.stringify(value)}`);
  } catch (error) {
    if (!isInterlayerError(error)) throw error;
    return error;
  }
}

/* ------------------------------------------------------------------ *
 * 1. The happy path
 * ------------------------------------------------------------------ */

describe('createLayer — happy path', () => {
  it('routes to the first provider, types the input and infers the output', async () => {
    const runtime = createFakeRuntime();
    const openai = defineProvider(ai, {
      id: 'openai',
      capabilities: {
        chat: async (input) => ({ text: input.prompt.toUpperCase(), by: 'openai' }),
      },
    });
    const layer = createLayer({ contract: ai, providers: [openai], runtime });

    const reply = await settle(runtime, layer.call('chat', { prompt: 'hello' }));

    // `reply` is `ChatReply` with no annotation anywhere in this test.
    expect(reply.text).toBe('HELLO');
    expect(reply.by).toBe('openai');
    await layer.close();
  });

  it('callWithMeta reports the provider, attempts and duration', async () => {
    const runtime = createFakeRuntime();
    const slow = defineProvider(ai, {
      id: 'slow',
      capabilities: {
        chat: async (input, ctx) => {
          await ctx.runtime.sleep(250, ctx.signal);
          return { text: input.prompt, by: 'slow' };
        },
      },
    });
    const layer = createLayer({ contract: ai, providers: [slow], runtime });

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));

    expect(meta.value).toEqual({ text: 'x', by: 'slow' });
    expect(meta.providerId).toBe('slow');
    expect(meta.provider).toBe('slow'); // R6 §5.2 destructures this name
    expect(meta.attempts).toBe(1);
    expect(meta.durationMs).toBe(250); // exact: virtual time, not a range
    expect(meta.errors).toEqual([]);
    await layer.close();
  });

  it('exposes contract and providers for introspection', async () => {
    const runtime = createFakeRuntime();
    const a = defineProvider(ai, {
      id: 'a',
      capabilities: { chat: async () => ({ text: '', by: 'a' }) },
    });
    const b = defineProvider(ai, {
      id: 'b',
      capabilities: { embed: async () => ({ vectors: [] }) },
    });
    const layer = createLayer({ contract: ai, providers: [a, b], runtime });

    expect(layer.contract).toBe(ai);
    expect(layer.providers.map((p) => p.id)).toEqual(['a', 'b']);
    await layer.close();
  });

  it('tryCall returns a Result instead of throwing', async () => {
    const runtime = createFakeRuntime();
    const dead = defineProvider(ai, {
      id: 'dead',
      capabilities: {
        chat: () => Promise.reject(new TransportError('down')),
      },
    });
    const layer = createLayer({
      contract: ai,
      providers: [dead],
      resilience: { retry: false },
      runtime,
    });

    const result = await settle(runtime, layer.tryCall('chat', { prompt: 'x' }));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.code).toBe('TRANSPORT');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Fallback
 * ------------------------------------------------------------------ */

describe('createLayer — fallback', () => {
  it('advances to the second provider when the first fails', async () => {
    const runtime = createFakeRuntime();
    const primary = flaky('primary', Number.POSITIVE_INFINITY);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'primary', capabilities: { chat: primary.handler } }),
        defineProvider(ai, {
          id: 'backup',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'backup' }) },
        }),
      ],
      resilience: { retry: false },
      runtime,
    });

    const advanced: InterlayerEvents['fallback:advance'][] = [];
    layer.on('fallback:advance', (e) => advanced.push(e));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'hi' }));

    expect(meta.value.by).toBe('backup');
    expect(meta.providerId).toBe('backup');
    // Nothing is swallowed: the suppressed failure rides along on the result.
    expect(meta.errors.map((e) => e.code)).toEqual(['TRANSPORT']);
    expect(meta.errors[0]?.providerId).toBe('primary');
    expect(advanced.map((e) => [e.fromProviderId, e.toProviderId])).toEqual([
      ['primary', 'backup'],
    ]);
    await layer.close();
  });

  it('aggregates into ALL_FAILED only when two or more providers failed', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'a',
          capabilities: { chat: () => Promise.reject(new TransportError('a down')) },
        }),
        defineProvider(ai, {
          id: 'b',
          capabilities: { chat: () => Promise.reject(new TransportError('b down')) },
        }),
      ],
      resilience: { retry: false },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('ALL_FAILED');
    if (hasCode(error, 'ALL_FAILED')) {
      expect(error.triedProviderIds).toEqual(['a', 'b']);
      expect(error.failures.map((f) => f.message)).toEqual(['a down', 'b down']);
    }
    await layer.close();
  });

  it('does NOT wrap a single failure — `code` survives to the caller', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'only',
          capabilities: { chat: () => Promise.reject(new ProviderError('bad request', {}, 400)) },
        }),
      ],
      resilience: { retry: false },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('PROVIDER_ERROR');
    expect(error.message).toBe('bad request');
    await layer.close();
  });

  it('distinguishes "nobody declares it" from "nobody is available right now"', async () => {
    // Two different facts deserve two different errors, and the facade is where
    // they are told apart: `registry.supports()` is deliberately independent of
    // `enabled` so that folding them together is not even possible.
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'p' }) },
        }),
      ],
      runtime,
    });

    // A capability the CONTRACT declares but no provider implements is not in
    // the layer's callable set at all — `supports()` says so, and `call()`
    // would not compile (see the type assertions in the acceptance block).
    expect(layer.supports('embed')).toBe(false);

    // Disabled: still declared, so this is NO_PROVIDER.
    layer.registry.setEnabled('p', false);
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }))).code).toBe(
      'NO_PROVIDER',
    );

    // Unregistered: nothing declares it any more, so it is UNSUPPORTED_CAPABILITY.
    layer.registry.unregister('p');
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }))).code).toBe(
      'UNSUPPORTED_CAPABILITY',
    );
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Retry
 * ------------------------------------------------------------------ */

describe('createLayer — retry', () => {
  it('retries the same provider and succeeds without ever falling back', async () => {
    const runtime = createFakeRuntime({ seed: 7 });
    const provider = flaky('primary', 2);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'primary', capabilities: { chat: provider.handler } }),
        defineProvider(ai, {
          id: 'backup',
          capabilities: { chat: async () => ({ text: 'never', by: 'backup' }) },
        }),
      ],
      resilience: { retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 100 } },
      runtime,
    });

    const scheduled: number[] = [];
    layer.on('retry:scheduled', (e) => scheduled.push(e.delayMs));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'go' }));

    expect(provider.calls).toBe(3);
    expect(meta.providerId).toBe('primary');
    expect(meta.attempts, 'physical invocations, not providers').toBe(3);
    expect(scheduled, 'fixed backoff, exact virtual delays').toEqual([100, 100]);
    // Two 100 ms sleeps and nothing else: the fallback chain never advanced.
    expect(meta.durationMs).toBe(200);
    await layer.close();
  });

  it('maxAttempts is TOTAL invocations including the first (R4 §1.2)', async () => {
    const runtime = createFakeRuntime();
    const provider = flaky('p', Number.POSITIVE_INFINITY);
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'p', capabilities: { chat: provider.handler } })],
      resilience: { retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 } },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(provider.calls, '3 total, not 4').toBe(3);
    // R4 §1.6: two or more failures aggregate; the last one is the cause.
    expect(error.code).toBe('RETRY_EXHAUSTED');
    if (hasCode(error, 'RETRY_EXHAUSTED')) {
      expect(error.attempts).toBe(3);
      expect(error.errors).toHaveLength(3);
    }
    await layer.close();
  });

  it('retries, exhausts, and only THEN falls back to the next provider', async () => {
    const runtime = createFakeRuntime();
    const primary = flaky('primary', Number.POSITIVE_INFINITY);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'primary', capabilities: { chat: primary.handler } }),
        defineProvider(ai, {
          id: 'backup',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'backup' }) },
        }),
      ],
      resilience: { retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 10 } },
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));

    expect(primary.calls).toBe(2);
    expect(meta.providerId).toBe('backup');
    expect(meta.attempts, '2 on primary + 1 on backup').toBe(3);
    // RetryExhaustedError inherits `retryable` from the last TransportError,
    // which is the only reason the chain advanced at all.
    expect(meta.errors.map((e) => e.code)).toEqual(['RETRY_EXHAUSTED']);
    await layer.close();
  });

  it('never retries a non-retryable error', async () => {
    const runtime = createFakeRuntime();
    const provider = flaky('p', Number.POSITIVE_INFINITY, () => new ProviderError('nope', {}, 400));
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'p', capabilities: { chat: provider.handler } })],
      resilience: { retry: { maxAttempts: 5 } },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(provider.calls).toBe(1);
    expect(error.code).toBe('PROVIDER_ERROR');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. Circuit breaker
 * ------------------------------------------------------------------ */

describe('createLayer — circuit breaker', () => {
  it('opens after repeated failure and then fails fast without calling the provider', async () => {
    const runtime = createFakeRuntime();
    const provider = flaky('dead', Number.POSITIVE_INFINITY);
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'dead', capabilities: { chat: provider.handler } })],
      resilience: {
        retry: false,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 10_000 },
      },
      runtime,
    });

    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect(provider.calls).toBe(2);
    expect(transitions.map((t) => [t.from, t.to])).toEqual([['closed', 'open']]);

    const open = await rejectionOf(runtime, layer.call('chat', { prompt: '3' }));
    expect(open.code).toBe('CIRCUIT_OPEN');
    expect(provider.calls, 'the third call never reached the provider').toBe(2);
    if (hasCode(open, 'CIRCUIT_OPEN')) {
      expect(open.key).toBe('dead');
      // `halfOpenAt - now`, so it is a usable Retry-After and not the constant
      // configured `resetMs`.
      expect(open.retryAfterMs).toBe(10_000);
    }
    await layer.close();
  });

  it('an open breaker on one provider does NOT fail-fast a healthy sibling', async () => {
    const runtime = createFakeRuntime();
    const broken = flaky('broken', Number.POSITIVE_INFINITY);
    const healthy = flaky('healthy', 0);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'broken', capabilities: { chat: broken.handler } }),
        defineProvider(ai, { id: 'healthy', capabilities: { chat: healthy.handler } }),
      ],
      resilience: { retry: false, breaker: { minimumThroughput: 2, failureRatio: 0.5 } },
      runtime,
    });

    // Two calls trip `broken`; both still succeed on `healthy`.
    for (const prompt of ['1', '2', '3']) {
      const meta = await settle(runtime, layer.callWithMeta('chat', { prompt }));
      expect(meta.providerId).toBe('healthy');
    }
    // The third call fails fast on `broken` (CIRCUIT_OPEN) and still reaches
    // `healthy`: state is keyed per provider, exactly as it must be.
    expect(broken.calls).toBe(2);
    expect(healthy.calls).toBe(3);
    await layer.close();
  });

  it('half-opens after the cooldown and closes on a successful probe', async () => {
    const runtime = createFakeRuntime();
    let broken = true;
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: async (input) => {
              if (broken) throw new TransportError('down');
              return { text: input.prompt, by: 'p' };
            },
          },
        }),
      ],
      resilience: {
        retry: false,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 5_000 },
      },
      runtime,
    });

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: '3' }))).code).toBe(
      'CIRCUIT_OPEN',
    );

    await runtime.advance(5_000); // the cooldown, on the virtual clock
    broken = false;
    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'probe' }));
    expect(meta.value.by).toBe('p');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 5. Timeouts and deadlines
 * ------------------------------------------------------------------ */

describe('createLayer — timeouts', () => {
  const neverSettles = (): Promise<ChatReply> => new Promise<ChatReply>(() => undefined);

  it('the attempt timeout bounds ONE provider call', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: { retry: false, timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 10_000 } },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope).toBe('attempt');
      expect(error.timeoutMs).toBe(50);
      expect(error.retryable, 'another provider may be faster').toBe(true);
    }
    expect(runtime.now()).toBe(50);
    expect(runtime.pendingTimers, 'every timer was cleared in a finally').toBe(0);
    await layer.close();
  });

  it('the total timeout bounds the whole call, across providers and retries', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'a', capabilities: { chat: neverSettles } }),
        defineProvider(ai, { id: 'b', capabilities: { chat: neverSettles } }),
      ],
      resilience: {
        retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 10 },
        timeout: { attemptTimeoutMs: 30, totalTimeoutMs: 100 },
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');
    expect(runtime.now(), 'not 5 attempts x 30 ms x 2 providers').toBe(100);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a per-call timeoutMs overrides the configured total budget', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: { retry: false, timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 30_000 } },
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { timeoutMs: 25 }),
    );
    expect(error.code).toBe('TIMEOUT');
    expect(runtime.now()).toBe(25);
    await layer.close();
  });

  it('deadlineAt is ABSOLUTE, is honoured with every policy off, and reads as TIMEOUT', async () => {
    const runtime = createFakeRuntime({ startTime: 1_000 });
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      // Everything disabled: only the facade can be enforcing this.
      resilience: { retry: false, timeout: false, breaker: false, rateLimit: false },
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { deadlineAt: 1_040 }),
    );

    // The whole point of defect 4: a blown deadline is a TIMEOUT, never a
    // CANCELLED. `CANCELLED` is non-retryable, never falls back, and tells an
    // operator the caller hung up when in fact we ran out of time.
    expect(error.code).toBe('TIMEOUT');
    expect(hasCode(error, 'CANCELLED')).toBe(false);
    expect(runtime.now(), 'an instant on the clock, not a duration').toBe(1_040);
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a caller abort is still a CANCELLED, and stops the chain where it stands', async () => {
    const runtime = createFakeRuntime();
    const second = flaky('second', 0);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'first',
          capabilities: {
            chat: async (input, ctx) => {
              await ctx.runtime.sleep(1_000, ctx.signal);
              return { text: input.prompt, by: 'first' };
            },
          },
        }),
        defineProvider(ai, { id: 'second', capabilities: { chat: second.handler } }),
      ],
      resilience: { retry: false, timeout: false },
      runtime,
    });

    const controller = new AbortController();
    const promise = layer.call('chat', { prompt: 'x' }, { signal: controller.signal });
    for (let i = 0; i < 5; i++) await Promise.resolve(); // let the first provider start
    controller.abort();

    const error = await rejectionOf(runtime, promise);
    expect(error.code).toBe('CANCELLED');
    expect(second.calls, 'a cancelled call does not march through the rest').toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 6. only(), supports(), on(), use(), close()
 * ------------------------------------------------------------------ */

describe('createLayer — the rest of the surface', () => {
  const three = () =>
    [
      defineProvider(ai, {
        id: 'openai',
        capabilities: { chat: async () => ({ text: 'o', by: 'openai' }) },
      }),
      defineProvider(ai, {
        id: 'anthropic',
        capabilities: { chat: async () => ({ text: 'a', by: 'anthropic' }) },
      }),
      defineProvider(ai, {
        id: 'voyage',
        capabilities: { embed: async () => ({ vectors: [[1]] }) },
      }),
    ] as const;

  it('only() pins the chain, in the order given, and never mutates the parent', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    const pinned = layer.only('anthropic');
    expect(pinned.providers.map((p) => p.id)).toEqual(['anthropic']);
    expect((await settle(runtime, pinned.call('chat', { prompt: 'x' }))).by).toBe('anthropic');
    // The parent is untouched — `only()` returns a view, it does not reconfigure.
    expect((await settle(runtime, layer.call('chat', { prompt: 'x' }))).by).toBe('openai');
    expect(layer.providers.map((p) => p.id)).toEqual(['openai', 'anthropic', 'voyage']);

    // Order is the caller's fallback order, not registration order.
    const reordered = layer.only('anthropic', 'openai');
    expect(reordered.providers.map((p) => p.id)).toEqual(['anthropic', 'openai']);
    await layer.close();
  });

  it('only() composes by INTERSECTION — a restriction cannot be widened', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    const narrowed = layer.only('anthropic').only('anthropic', 'openai');
    expect(narrowed.providers.map((p) => p.id)).toEqual(['anthropic']);

    // Nor by a per-call hint: `providers` is intersected with the pin too, so a
    // hint that contradicts the pin leaves NOTHING — which is NO_PROVIDER, not
    // a silent win for either side. Neither "the pin wins" nor "the hint wins"
    // is defensible: both would let one of the two restrictions evaporate.
    const conflict = await rejectionOf(
      runtime,
      layer.only('anthropic').call('chat', { prompt: 'x' }, { providers: ['openai'] }),
    );
    expect(conflict.code).toBe('NO_PROVIDER');

    // A hint INSIDE the pin is honoured normally.
    const meta = await settle(
      runtime,
      layer
        .only('anthropic', 'openai')
        .callWithMeta('chat', { prompt: 'x' }, { providers: ['openai'] }),
    );
    expect(meta.providerId).toBe('openai');
    await layer.close();
  });

  it('only() with an id that implements nothing for this capability is NO_PROVIDER', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    const error = await rejectionOf(runtime, layer.only('voyage').call('chat', { prompt: 'x' }));
    expect(error.code).toBe('NO_PROVIDER');
    await layer.close();
  });

  it('supports() answers for what is actually implemented', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    expect(layer.supports('chat')).toBe(true);
    expect(layer.supports('embed')).toBe(true);
    expect(layer.supports('summarise')).toBe(false); // not in the contract at all
    await layer.close();
  });

  it('on() returns its own unsubscribe closure', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    const seen: string[] = [];
    const stop = layer.on('call:start', (e) => seen.push(e.capability));
    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(seen).toEqual(['chat']);

    // No identity trap: unsubscribing needs the closure, not the function.
    stop();
    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(seen).toEqual(['chat']);

    stop(); // idempotent
    await layer.close();
  });

  it('a throwing listener cannot break the call path', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    const reported: unknown[] = [];
    layer.on('listener:error', (e) => reported.push(e.error));
    layer.on('call:start', () => {
      throw new Error('observer blew up');
    });

    const reply = await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(reply.by).toBe('openai');
    expect(reported).toHaveLength(1);
    await layer.close();
  });

  it('use() wraps the whole call, once, outside provider selection', async () => {
    const runtime = createFakeRuntime();
    const primary = flaky('openai', 1);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'openai', capabilities: { chat: primary.handler } }),
        defineProvider(ai, {
          id: 'anthropic',
          capabilities: { chat: async () => ({ text: 'a', by: 'anthropic' }) },
        }),
      ],
      resilience: { retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 1 } },
      runtime,
    });

    const spans: string[] = [];
    const tracing: CallMiddleware = async (ctx, next) => {
      spans.push(`start:${ctx.capability}`);
      try {
        return await next();
      } finally {
        spans.push(`end:${ctx.capability}`);
      }
    };
    expect(layer.use(tracing)).toBe(layer); // chainable

    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    // ONCE per call, not once per attempt: two attempts happened inside it.
    expect(spans).toEqual(['start:chat', 'end:chat']);
    expect(primary.calls).toBe(2);
    await layer.close();
  });

  it('close() disposes providers, is idempotent, and works as `await using`', async () => {
    const runtime = createFakeRuntime();
    let disposed = 0;
    const provider = defineProvider(ai, {
      id: 'p',
      capabilities: { chat: async () => ({ text: '', by: 'p' }) },
      dispose: () => {
        disposed++;
      },
    });

    {
      await using layer = createLayer({ contract: ai, providers: [provider], runtime });
      await settle(runtime, layer.call('chat', { prompt: 'x' }));
    } // `[Symbol.asyncDispose]` runs here

    expect(disposed).toBe(1);

    const second = createLayer({ contract: ai, providers: [provider], runtime });
    await second.close();
    await second.close(); // idempotent: no second teardown
    expect(disposed).toBe(2);
  });

  it('registers a pre-erased ProviderRecord with no cast at all', () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: three(), runtime });

    // The shared harness produces a `ProviderRecord`, and `Registrable` is what
    // lets it in without `as unknown as Provider<C>`. A cast in a test harness
    // is a cast that can hide a real mismatch.
    layer.registry.register(fakeProvider('scripted', { capabilities: ['chat'] }).record);
    expect(layer.registry.get('scripted')?.id).toBe('scripted');
  });
});

/* ------------------------------------------------------------------ *
 * 7. Per-capability resilience
 * ------------------------------------------------------------------ */

describe('createLayer — perCapability', () => {
  it('gives a named capability its own policy set, leaving the default alone', async () => {
    const runtime = createFakeRuntime();
    const chat = flaky('p', Number.POSITIVE_INFINITY);
    let embedCalls = 0;
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: chat.handler,
            embed: () => {
              embedCalls++;
              return Promise.reject(new TransportError('down'));
            },
          },
        }),
      ],
      resilience: { retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 } },
      perCapability: { embed: { retry: false } },
      runtime,
    });

    await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(chat.calls, 'the layer default applies').toBe(3);

    await rejectionOf(runtime, layer.call('embed', { texts: ['a'] }));
    expect(embedCalls, 'the override REPLACES the default, it does not merge').toBe(1);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 8. THE ACCEPTANCE CRITERION — R6 §5.2's published usage example.
 *
 * Built as running code. Where it could not be written verbatim the deviation
 * is named inline; each one is a settled Wave-1/Wave-2 decision that landed
 * after R6 was written, not a shortcoming discovered here.
 * ------------------------------------------------------------------ */

describe('R6 §5.2 — the literal usage example, as executable code', () => {
  it('steps 1-10 run end to end', async () => {
    const runtime = createFakeRuntime();
    const log: string[] = [];
    const metrics: string[] = [];

    // 1 ── declare what your application needs, independent of who provides it.
    //      VERBATIM.
    const contract = defineContract({
      chat: capability<ChatRequest, ChatReply>({ idempotent: false }),
      embed: capability<EmbedRequest, EmbedReply>({ idempotent: true }),
      transcribe: capability<{ url: string }, { text: string }>({ idempotent: true }),
    });

    // 2 ── implement it, once per backend. No parameter annotations needed.
    //      DEVIATION: `id:`, not `name:`; `dispose:`, not `close:`. The guide's
    //      reconciliation table (§2) chose R3's `id` because `providerId` is
    //      load-bearing across the error context and the whole event map.
    let poolClosed = 0;
    const openai = defineProvider(contract, {
      id: 'openai',
      capabilities: {
        chat: async (input, { signal }) => {
          signal.throwIfAborted();
          return { text: `openai:${input.prompt}`, by: 'openai' };
        },
        embed: async (input) => ({ vectors: input.texts.map(() => [0.1]) }),
      },
      dispose: () => {
        poolClosed++;
      },
    });

    const anthropic = defineProvider(contract, {
      id: 'anthropic',
      capabilities: {
        chat: async (input) => ({ text: `anthropic:${input.prompt}`, by: 'anthropic' }),
      },
    });

    const deepgram = defineProvider(contract, {
      id: 'deepgram',
      capabilities: { transcribe: async (input) => ({ text: `heard ${input.url}` }) },
    });

    // 3 ── wire it. Array order is the default fallback order.
    //      DEVIATION: the resilience option NAMES are core's (`policy.ts`,
    //      R4 §0.1) rather than R6's draft — `timeout.attemptTimeoutMs` not
    //      `timeoutMs`, `retry.maxAttempts` not `attempts`, `breaker.
    //      failureRatio/minimumThroughput/resetMs` not `failureThreshold/
    //      samples/resetAfterMs`, `rateLimit.refillPerSec/capacity` not
    //      `perSecond/burst`. `concurrency` does NOT exist: no bulkhead unit was
    //      built in this wave (`BULKHEAD_FULL` is reserved in `ErrorCode`).
    const layer = createLayer({
      contract,
      providers: [openai, anthropic, deepgram],
      resilience: {
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 30_000 },
        retry: { maxAttempts: 3, strategy: 'exponential', baseDelayMs: 200 },
        breaker: { failureRatio: 0.5, minimumThroughput: 20, resetMs: 30_000 },
        rateLimit: { refillPerSec: 20, capacity: 40 },
      },
      perCapability: {
        embed: { timeout: { attemptTimeoutMs: 2_000 }, retry: { maxAttempts: 5 } },
        transcribe: { timeout: { attemptTimeoutMs: 120_000 }, retry: false }, // explicit off
      },
      runtime,
    });

    // 4 ── call it. `input` is checked against ChatRequest; `reply` is ChatReply.
    //      VERBATIM.
    const reply = await settle(runtime, layer.call('chat', { prompt: 'hello' }));
    expect(reply.text).toBe('openai:hello'); // `reply.text` is a string

    // 5 ── per-call overrides.
    //      DEVIATION: the option is `providers`, not `route`. Core's
    //      `CallOptions` names it `providers` and `byHints()` reads that.
    const urgent = await settle(
      runtime,
      layer.call(
        'chat',
        { prompt: 'urgent' },
        { providers: ['anthropic', 'openai'], timeoutMs: 2_000 },
      ),
    );
    expect(urgent.by).toBe('anthropic');

    // 6 ── pin a provider (evals, canaries, reproducing a bug). VERBATIM.
    const control = await settle(runtime, layer.only('anthropic').call('chat', { prompt: 'c' }));
    expect(control.by).toBe('anthropic');

    // 7 ── when you need to know what actually happened. VERBATIM, including
    //      the `provider` field name.
    const { value, provider, attempts, durationMs, errors } = await settle(
      runtime,
      layer.callWithMeta('embed', { texts: ['a', 'b'] }),
    );
    metrics.push(`ai.embed ${String(durationMs)} ${provider} ${String(attempts)}`);
    expect(value.vectors).toHaveLength(2);
    expect(errors).toEqual([]);
    expect(metrics).toEqual(['ai.embed 0 openai 1']);

    // 8 ── observe. `on` returns its own unsubscribe; no `off(fn)` identity trap.
    //      DEVIATION: event names are the core `InterlayerEvents` keys —
    //      `fallback:advance` and `breaker:transition`, not `fallback`/`breaker`
    //      — and the payload fields are `fromProviderId`/`toProviderId`/`reason`
    //      (an `ErrorCode` string) rather than `from`/`to`/`reason.code`.
    const stop = layer.on('fallback:advance', (e) => {
      log.push(`${e.fromProviderId} -> ${e.toProviderId} (${e.reason})`);
    });
    layer.on('breaker:transition', (e) => metrics.push(`breaker ${e.key} ${e.to}`));

    // 9 ── cross-cutting concerns as onion middleware (koa/hono shape).
    //      DEVIATION: the middleware sees a `CallContext`, so the provider is
    //      not knowable at this level — provider identity belongs to the ATTEMPT
    //      scope, and the call stack runs once for the whole fallback chain.
    layer.use(async (call, next) => {
      log.push(`span:${call.capability}`);
      try {
        return await next();
      } finally {
        log.push(`span-end:${call.capability}`);
      }
    });

    // 10 ── errors: discriminated by `code`, and nothing is ever swallowed.
    //       VERBATIM in shape. `AllProvidersFailedError.failures` is
    //       `readonly AnyInterlayerError[]`, each with `.providerId` and `.cause`.
    const broken = createLayer({
      contract,
      providers: [
        defineProvider(contract, {
          id: 'a',
          capabilities: {
            chat: () =>
              Promise.reject(new TransportError('boom a', { cause: new Error('boom a') })),
          },
        }),
        defineProvider(contract, {
          id: 'b',
          capabilities: {
            chat: () =>
              Promise.reject(new TransportError('boom b', { cause: new Error('boom b') })),
          },
        }),
      ],
      resilience: { retry: false },
      runtime,
    });

    let handled = '';
    try {
      await settle(runtime, broken.call('chat', { prompt: 'x' }));
    } catch (err) {
      if (isInterlayerError(err)) {
        switch (err.code) {
          case 'TIMEOUT':
            handled = 'degrade';
            break;
          case 'CIRCUIT_OPEN':
            handled = 'queueForLater';
            break;
          case 'ALL_FAILED': {
            handled = 'all-failed';
            expect(err.failures.map((f) => f.providerId)).toEqual(['a', 'b']);
            expect(err.failures.map((f) => (f.cause as Error).message)).toEqual([
              'boom a',
              'boom b',
            ]);
            break;
          }
          default:
            handled = err.code;
        }
      }
    }
    expect(handled).toBe('all-failed');
    await broken.close();

    // The fallback event and the middleware both fired on a real call.
    const fellBack = await settle(
      runtime,
      layer.only('openai', 'anthropic').callWithMeta('chat', { prompt: 'trace' }),
    );
    expect(fellBack.providerId).toBe('openai');
    expect(log).toEqual(['span:chat', 'span-end:chat']);
    stop();

    // `await using layer = createLayer(...)` also works — but only where the
    // SYNTAX parses. `package.json` declares `engines.node >= 22.12`, and V8 on
    // Node 22 rejects `await using` outright (`SyntaxError`); `Symbol.asyncDispose`
    // itself is honoured, which is why the test above can call it directly.
    // Vitest transpiles the declaration away, so a passing test here is not
    // evidence that a consumer on the declared floor can write it.
    // `await layer.close()` is the portable form and the one to document.
    await layer.close();
    expect(poolClosed).toBe(1);
  });

  it('the type-level claims in the example hold', async () => {
    const runtime = createFakeRuntime();
    const contract = defineContract({
      chat: capability<ChatRequest, ChatReply>(),
      transcribe: capability<{ url: string }, { text: string }>(),
    });
    const chatOnly = defineProvider(contract, {
      id: 'chat-only',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'chat-only' }) },
    });
    const layer = createLayer({ contract, providers: [chatOnly], runtime });

    /**
     * Compile-time assertions. NEVER CALLED — every line in here is a rejection
     * at runtime as well as an error at compile time, and running them would
     * only add unhandled rejections to the report. `@ts-expect-error` is checked
     * by `tsc`, so an assertion that stopped being true fails `npm run
     * typecheck` with `TS2578: Unused '@ts-expect-error' directive`.
     */
    const typeAssertions = async (): Promise<void> => {
      // @ts-expect-error — 'summarise' is not a capability of this contract.
      await layer.call('summarise', { prompt: 'x' });

      // @ts-expect-error — `transcribe` is DECLARED but nobody implements it,
      // so it is not in the layer's `Impl` union. That is R6 §5.1's whole
      // point: the callable set comes from the providers, not the contract.
      await layer.call('transcribe', { url: 'x' });

      // @ts-expect-error — the input is checked against ChatRequest.
      await layer.call('chat', { promt: 'typo' });

      // @ts-expect-error — 'nope' is not a registered provider id.
      layer.only('nope');
    };
    expect(typeof typeAssertions).toBe('function');

    const reply = await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(reply.text).toBe('x');
    // @ts-expect-error — the output is ChatReply, which has no `vectors`.
    expect(reply.vectors).toBeUndefined();

    await layer.close();
  });

  it('a bare `throw new Error()` advances the fallback chain', async () => {
    // Regression. This once did NOT fall back, and it was the most likely
    // surprise in the library: `toInterlayerError` turns an unrecognised throw
    // into a `ProviderError` with `retryable: false` (R4 §1.3 rule 8 — a
    // `TypeError` in our own code must not be run three times), and
    // `defaultShouldFallback` used to advance on `error.retryable`. So two
    // providers that both `throw new Error('boom')` produced ONE
    // `PROVIDER_ERROR` and provider b was never tried at all.
    //
    // `retryable` answers "call the SAME provider again?"; fallback asks
    // whether a DIFFERENT one could do better. Provider a's bug says nothing
    // about provider b, so the chain now advances.
    const runtime = createFakeRuntime();
    const bare = (id: string) =>
      defineProvider(ai, {
        id,
        capabilities: { chat: () => Promise.reject(new Error(`boom ${id}`)) },
      });

    const layer = createLayer({
      contract: ai,
      providers: [bare('a'), bare('b')],
      resilience: { retry: false },
      runtime,
    });
    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('ALL_FAILED');
    if (hasCode(error, 'ALL_FAILED')) {
      expect(error.triedProviderIds, 'both providers tried').toEqual(['a', 'b']);
      expect(error.failures, 'every suppressed cause retained').toHaveLength(2);
    }
    await layer.close();

    // A caller who wants the old fail-fast behaviour still opts in explicitly.
    const pinned = createLayer({
      contract: ai,
      providers: [bare('a'), bare('b')],
      resilience: { retry: false },
      shouldFallback: () => false,
      runtime,
    });
    const first = await rejectionOf(runtime, pinned.call('chat', { prompt: 'x' }));
    expect(first.code).toBe('PROVIDER_ERROR');
    expect(first.providerId, 'stopped at the first provider by request').toBe('a');
    await pinned.close();
  });
});
