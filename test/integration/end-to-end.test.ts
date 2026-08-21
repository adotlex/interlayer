/**
 * The assembled layer, end to end, THROUGH THE PUBLISHED BARREL.
 *
 * Every import below comes from `src/index.ts` — the exact specifier a consumer
 * writes as `from 'interlayer'` — because a unit test that reaches into
 * `src/routing/router.ts` proves nothing about what was published. The only
 * non-barrel imports are the frozen fake clock and this suite's own drivers.
 *
 * Nothing here sleeps. A 30-second total timeout and a 10-second breaker
 * cooldown both settle in microseconds of virtual time, which is also what lets
 * every timing assertion below be an equality rather than a range.
 */

import { describe, expect, it } from 'vitest';
import {
  capability,
  createLayer,
  defineContract,
  defineProvider,
  hasCode,
  type InterlayerEvents,
  ProviderError,
  TransportError,
} from '../../src/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import {
  counter,
  drainMicrotasks,
  neverSettles,
  recordEventNames,
  rejectionOf,
  settle,
} from './harness.ts';

/* ------------------------------------------------------------------ *
 * Fixtures — the shape a real consumer writes.
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
  readonly by: string;
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

/** A handler that throws its first `failures` calls and then succeeds. */
function flaky(
  id: string,
  failures: number,
  error: () => unknown = () => new TransportError(`${id} is down`),
): { readonly calls: number; handler: (input: ChatRequest) => Promise<ChatReply> } {
  const seen = counter();
  return {
    get calls(): number {
      return seen.count;
    },
    handler: async (input: ChatRequest): Promise<ChatReply> => {
      if (seen.bump() <= failures) throw error();
      return { text: input.prompt.toUpperCase(), by: id };
    },
  };
}

/** Resilience with everything off — the baseline a focused test starts from. */
const NOTHING = { retry: false, timeout: false, breaker: false, rateLimit: false } as const;

/* ------------------------------------------------------------------ *
 * 1. Happy path
 * ------------------------------------------------------------------ */

describe('integration — happy path', () => {
  it('registers providers, calls a capability and returns a typed result', async () => {
    const runtime = createFakeRuntime();
    const openai = defineProvider(ai, {
      id: 'openai',
      capabilities: {
        chat: async (input) => ({ text: input.prompt.toUpperCase(), by: 'openai' }),
        embed: async (input) => ({ vectors: input.texts.map(() => [0.5]), by: 'openai' }),
      },
    });
    const anthropic = defineProvider(ai, {
      id: 'anthropic',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'anthropic' }) },
    });
    const layer = createLayer({ contract: ai, providers: [openai, anthropic], runtime });

    // No annotation anywhere: `reply` is `ChatReply`, `vectors` is number[][].
    const reply = await settle(runtime, layer.call('chat', { prompt: 'hello' }));
    expect(reply).toEqual({ text: 'HELLO', by: 'openai' });

    const embedded = await settle(runtime, layer.call('embed', { texts: ['a', 'b'] }));
    expect(embedded.vectors).toEqual([[0.5], [0.5]]);

    // Registration order is the default candidate order, and the whole call
    // left nothing armed behind it.
    expect(layer.providers.map((p) => p.id)).toEqual(['openai', 'anthropic']);
    await drainMicrotasks();
    expect(runtime.pendingTimers, 'every policy cleared its timer').toBe(0);
    await layer.close();
  });

  it('callWithMeta reports value, provider, attempts, durationMs and errors', async () => {
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
    expect(meta.attempts, 'one physical invocation').toBe(1);
    expect(meta.durationMs, 'exact virtual time, not a range').toBe(250);
    expect(meta.errors).toEqual([]);
    // BOTH names carry the same string. See the report: `providerId` and
    // `provider` are an unsettled duplication on the public result type.
    expect(meta.providerId).toBe('slow');
    expect(meta.provider).toBe('slow');
    await layer.close();
  });

  it('tryCall turns a domain failure into a Result instead of a rejection', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'dead',
          capabilities: { chat: () => Promise.reject(new TransportError('down')) },
        }),
      ],
      resilience: NOTHING,
      runtime,
    });

    const result = await settle(runtime, layer.tryCall('chat', { prompt: 'x' }));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('TRANSPORT');
      expect(result.error.providerId).toBe('dead');
    }
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Fallback — which provider won, and what it cost
 * ------------------------------------------------------------------ */

describe('integration — fallback', () => {
  it('advances to the second provider and callWithMeta names the winner', async () => {
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
      resilience: { ...NOTHING },
      runtime,
    });

    const advances: InterlayerEvents['fallback:advance'][] = [];
    layer.on('fallback:advance', (e) => advances.push(e));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'hi' }));

    expect(meta.value.by).toBe('backup');
    expect(meta.provider).toBe('backup');
    expect(meta.attempts, 'one on primary, one on backup').toBe(2);
    // The suppressed failure rides along on the successful result: a fallback
    // never swallows a cause.
    expect(meta.errors.map((e) => e.code)).toEqual(['TRANSPORT']);
    expect(meta.errors.at(0)?.providerId).toBe('primary');
    expect(advances.map((e) => [e.fromProviderId, e.toProviderId, e.reason])).toEqual([
      ['primary', 'backup', 'TRANSPORT'],
    ]);
    await layer.close();
  });

  it('emits the full lifecycle in order for one fallback call', async () => {
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
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'b' }) },
        }),
      ],
      resilience: { ...NOTHING },
      runtime,
    });

    const names = recordEventNames(layer.events);
    await settle(runtime, layer.call('chat', { prompt: 'x' }));

    expect(names).toEqual([
      'call:start',
      'provider:selected',
      'attempt:start',
      'attempt:failure',
      'fallback:advance',
      'provider:selected',
      'attempt:start',
      'attempt:success',
      'call:success',
    ]);
    await layer.close();
  });

  it('aggregates into ALL_FAILED only when two or more providers were tried', async () => {
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
          capabilities: { chat: () => Promise.reject(new ProviderError('b refused', {}, 503)) },
        }),
      ],
      resilience: { ...NOTHING },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('ALL_FAILED');
    if (hasCode(error, 'ALL_FAILED')) {
      expect(error.triedProviderIds).toEqual(['a', 'b']);
      expect(error.failures.map((f) => f.code)).toEqual(['TRANSPORT', 'PROVIDER_ERROR']);
      expect(error.failures.map((f) => f.providerId)).toEqual(['a', 'b']);
    }
    await layer.close();
  });

  it('a single failure is NEVER wrapped — `code` survives to the caller', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'only',
          capabilities: { chat: () => Promise.reject(new ProviderError('bad request', {}, 400)) },
        }),
      ],
      resilience: { ...NOTHING },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('PROVIDER_ERROR');
    expect(error.message).toBe('bad request');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Retry
 * ------------------------------------------------------------------ */

describe('integration — retry then success', () => {
  it('fails twice, succeeds on the third attempt, and never falls back', async () => {
    const runtime = createFakeRuntime();
    const primary = flaky('primary', 2);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'primary', capabilities: { chat: primary.handler } }),
        defineProvider(ai, {
          id: 'backup',
          capabilities: { chat: async () => ({ text: 'never', by: 'backup' }) },
        }),
      ],
      resilience: {
        ...NOTHING,
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 100 },
      },
      runtime,
    });

    const scheduled: InterlayerEvents['retry:scheduled'][] = [];
    layer.on('retry:scheduled', (e) => scheduled.push(e));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'go' }));

    expect(primary.calls).toBe(3);
    expect(meta.value.by, 'the backup was never reached').toBe('primary');
    expect(meta.attempts, 'physical invocations, not providers').toBe(3);
    // Two fixed 100 ms sleeps and nothing else.
    expect(scheduled.map((e) => e.delayMs)).toEqual([100, 100]);
    expect(
      scheduled.map((e) => e.attempt),
      'the UPCOMING attempt',
    ).toEqual([2, 3]);
    expect(meta.durationMs).toBe(200);
    // A retried-then-successful call reports no suppressed errors at the call
    // level: `errors` is the FALLBACK ledger, and the chain never advanced.
    expect(meta.errors, 'per-provider retries are not call-level failures').toEqual([]);
    await layer.close();
  });

  it('attempts and errors stay accurate across retry AND fallback', async () => {
    const runtime = createFakeRuntime();
    const primary = flaky('primary', Number.POSITIVE_INFINITY);
    const backup = flaky('backup', 1);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'primary', capabilities: { chat: primary.handler } }),
        defineProvider(ai, { id: 'backup', capabilities: { chat: backup.handler } }),
      ],
      resilience: { ...NOTHING, retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 10 } },
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));

    expect(primary.calls, 'retried to exhaustion before advancing').toBe(2);
    expect(backup.calls, 'then retried once on the backup').toBe(2);
    expect(meta.provider).toBe('backup');
    expect(meta.attempts, '2 on primary + 2 on backup').toBe(4);
    // One entry per PROVIDER that failed, not one per physical call: primary's
    // two failures were aggregated by the retry loop first.
    expect(meta.errors.map((e) => e.code)).toEqual(['RETRY_EXHAUSTED']);
    const suppressed = meta.errors.at(0);
    expect(suppressed?.providerId).toBe('primary');
    if (hasCode(suppressed, 'RETRY_EXHAUSTED')) {
      expect(suppressed.attempts).toBe(2);
      expect(suppressed.errors, 'every physical failure retained').toHaveLength(2);
    }
    expect(meta.durationMs, 'two 10 ms backoffs, one per provider').toBe(20);
    await layer.close();
  });

  it('`willRetry` on attempt:failure agrees with what actually happened', async () => {
    // ADVISORY, and deliberately asserted far from any budget boundary: retry's
    // wall-clock budget is not knowable from the terminal that emits this.
    const runtime = createFakeRuntime();
    const provider = flaky('p', 2);
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'p', capabilities: { chat: provider.handler } })],
      resilience: { ...NOTHING, retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 } },
      runtime,
    });

    const failures: InterlayerEvents['attempt:failure'][] = [];
    layer.on('attempt:failure', (e) => failures.push(e));

    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(failures.map((e) => [e.attempt, e.willRetry])).toEqual([
      [1, true],
      [2, true],
    ]);
    await layer.close();
  });

  it('a non-retryable failure is never retried', async () => {
    const runtime = createFakeRuntime();
    const provider = flaky('p', Number.POSITIVE_INFINITY, () => new ProviderError('nope', {}, 400));
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'p', capabilities: { chat: provider.handler } })],
      resilience: { ...NOTHING, retry: { maxAttempts: 5, strategy: 'fixed', baseDelayMs: 1 } },
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

describe('integration — breaker opening', () => {
  it('trips on repeated failure, then fails fast WITHOUT invoking the handler', async () => {
    const runtime = createFakeRuntime();
    const dead = flaky('dead', Number.POSITIVE_INFINITY);
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'dead', capabilities: { chat: dead.handler } })],
      resilience: {
        ...NOTHING,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 10_000 },
      },
      runtime,
    });

    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect(dead.calls).toBe(2);
    expect(transitions.map((t) => [t.from, t.to])).toEqual([['closed', 'open']]);

    const open = await rejectionOf(runtime, layer.call('chat', { prompt: '3' }));
    expect(open.code).toBe('CIRCUIT_OPEN');
    expect(dead.calls, 'the third call never reached the provider').toBe(2);
    if (hasCode(open, 'CIRCUIT_OPEN')) {
      expect(open.key, 'breaker state is keyed on the provider id').toBe('dead');
      // `halfOpenAt - now`, a usable Retry-After rather than the configured
      // constant.
      expect(open.retryAfterMs).toBe(10_000);
    }
    await layer.close();
  });

  it('a fail-fast costs zero attempts but still shows up in `errors`', async () => {
    const runtime = createFakeRuntime();
    const dead = flaky('dead', Number.POSITIVE_INFINITY);
    const healthy = flaky('healthy', 0);
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, { id: 'dead', capabilities: { chat: dead.handler } }),
        defineProvider(ai, { id: 'healthy', capabilities: { chat: healthy.handler } }),
      ],
      resilience: { ...NOTHING, breaker: { minimumThroughput: 2, failureRatio: 0.5 } },
      runtime,
    });

    // Two calls trip `dead`; both still succeed on `healthy`.
    for (const prompt of ['1', '2']) {
      const warm = await settle(runtime, layer.callWithMeta('chat', { prompt }));
      expect(warm.provider).toBe('healthy');
      expect(warm.attempts, 'one wasted call on dead, one real one on healthy').toBe(2);
    }

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: '3' }));
    expect(meta.provider).toBe('healthy');
    // An open breaker on one provider must never fail-fast a healthy sibling.
    expect(dead.calls, 'frozen at the two calls that tripped it').toBe(2);
    expect(healthy.calls).toBe(3);
    // The rejected candidate contributes an error but NOT an attempt: nothing
    // physical happened on `dead`.
    expect(meta.errors.map((e) => e.code)).toEqual(['CIRCUIT_OPEN']);
    expect(meta.attempts, 'only the healthy invocation was physical').toBe(1);
    await layer.close();
  });

  it('half-opens after the cooldown and closes again on a successful probe', async () => {
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
        ...NOTHING,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 5_000 },
      },
      runtime,
    });

    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: '3' }))).code).toBe(
      'CIRCUIT_OPEN',
    );

    // The cooldown is evaluated lazily on the next call — no timer is held.
    expect(runtime.pendingTimers, 'the breaker never arms a timer').toBe(0);
    await runtime.advance(5_000);
    broken = false;

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'probe' }));
    expect(meta.value.by).toBe('p');
    expect(transitions.map((t) => t.to)).toEqual(['open', 'half-open', 'closed']);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 5. Timeouts — and the timer ledger
 * ------------------------------------------------------------------ */

describe('integration — timeouts', () => {
  it('abandons a slow provider at the attempt budget and leaks no timer', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: {
        ...NOTHING,
        timeout: { attemptTimeoutMs: 50, totalTimeoutMs: 10_000 },
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) {
      expect(error.scope).toBe('attempt');
      expect(error.timeoutMs).toBe(50);
      expect(error.retryable, 'another provider may well be faster').toBe(true);
      expect(error.providerId).toBe('hung');
    }
    expect(runtime.now(), 'abandoned exactly at the budget').toBe(50);
    await drainMicrotasks();
    expect(runtime.pendingTimers, 'both the attempt and total timers were cleared').toBe(0);
    await layer.close();
  });

  it('the total budget bounds the whole fallback chain, not each hop', async () => {
    const runtime = createFakeRuntime();
    const hung = (id: string) => defineProvider(ai, { id, capabilities: { chat: neverSettles } });
    const layer = createLayer({
      contract: ai,
      providers: [hung('a'), hung('b'), hung('c'), hung('d')],
      resilience: {
        ...NOTHING,
        timeout: { attemptTimeoutMs: 30, totalTimeoutMs: 100 },
      },
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));

    expect(error.code).toBe('TIMEOUT');
    if (hasCode(error, 'TIMEOUT')) expect(error.scope).toBe('call');
    expect(runtime.now(), 'not 4 hops x 30 ms').toBe(100);
    await drainMicrotasks();
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a per-call timeoutMs overrides the configured total budget', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: {
        ...NOTHING,
        timeout: { attemptTimeoutMs: 10_000, totalTimeoutMs: 30_000 },
      },
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { timeoutMs: 25 }),
    );
    expect(error.code).toBe('TIMEOUT');
    expect(runtime.now()).toBe(25);
    await drainMicrotasks();
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('an absolute deadlineAt holds even with every policy switched off', async () => {
    const runtime = createFakeRuntime({ startTime: 1_000 });
    const layer = createLayer({
      contract: ai,
      providers: [defineProvider(ai, { id: 'hung', capabilities: { chat: neverSettles } })],
      resilience: NOTHING,
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { deadlineAt: 1_040 }),
    );

    // A blown deadline is a TIMEOUT, never a CANCELLED: CANCELLED reads as "the
    // caller hung up" and would tell an operator the wrong story.
    expect(error.code).toBe('TIMEOUT');
    expect(runtime.now(), 'an instant on the clock, not a duration').toBe(1_040);
    await drainMicrotasks();
    expect(runtime.pendingTimers).toBe(0);
    await layer.close();
  });

  it('a deadline already in the past fails before entering any provider', async () => {
    const runtime = createFakeRuntime({ startTime: 5_000 });
    const called = counter();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: async (input) => {
              called.bump();
              return { text: input.prompt, by: 'p' };
            },
          },
        }),
      ],
      runtime,
    });

    const error = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { deadlineAt: 4_000 }),
    );
    expect(error.code).toBe('TIMEOUT');
    expect(called.count, 'no provider was entered').toBe(0);
    expect(runtime.pendingTimers, 'and no 0 ms timer was armed').toBe(0);
    await layer.close();
  });

  it('a caller abort stops the chain where it stands and reads as CANCELLED', async () => {
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
      resilience: NOTHING,
      runtime,
    });

    const controller = new AbortController();
    const promise = layer.call('chat', { prompt: 'x' }, { signal: controller.signal });
    await drainMicrotasks(); // let the first provider actually start
    controller.abort();

    const error = await rejectionOf(runtime, promise);
    expect(error.code).toBe('CANCELLED');
    expect(second.calls, 'a cancelled call does not march through the rest').toBe(0);
    await drainMicrotasks();
    expect(runtime.pendingTimers, 'the interrupted sleep cleared its timer').toBe(0);
    await layer.close();
  });
});
