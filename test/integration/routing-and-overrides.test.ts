/**
 * Selection, routing limits and per-capability policy, driven through the
 * facade rather than through `createRouter`.
 *
 * The selectors were unit-tested against a hand-built `CallContext`; none of
 * them had ever been reached through `createLayer`, which is the only path a
 * consumer has. Everything below therefore goes in via `layer.call` and asserts
 * on the provider that actually served the request.
 */

import { describe, expect, it } from 'vitest';
import {
  capability,
  chainSelectors,
  createLayer,
  defineContract,
  defineProvider,
  filterByTags,
  hasCode,
  type InterlayerEvents,
  roundRobin,
  sticky,
  TransportError,
  weighted,
} from '../../src/index.ts';
import { createFakeRuntime, scriptedRandom } from '../support/index.ts';
import { counter, rejectionOf, settle } from './harness.ts';

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

/** Everything off, so the only thing under test is selection. */
const NOTHING = { retry: false, timeout: false, breaker: false, rateLimit: false } as const;

function chatProvider(id: string, tags?: readonly string[]) {
  return defineProvider(ai, {
    id,
    capabilities: { chat: async (input) => ({ text: input.prompt, by: id }) },
    ...(tags === undefined ? {} : { traits: { tags } }),
  });
}

/* ------------------------------------------------------------------ *
 * 1. Selectors through the facade
 * ------------------------------------------------------------------ */

describe('integration — selectors', () => {
  it('the default selector is registration order, and it is the fallback order', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [chatProvider('a'), chatProvider('b'), chatProvider('c')],
      resilience: NOTHING,
      runtime,
    });

    const selected: string[] = [];
    layer.on('provider:selected', (e) => selected.push(e.providerId));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));
    expect(meta.provider).toBe('a');
    expect(selected).toEqual(['a']);
    // The whole candidate list travels on the event, so an operator can see
    // what WOULD have been tried without provoking a failure.
    await layer.close();
  });

  it('roundRobin() rotates the lead provider one position per call', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [chatProvider('a'), chatProvider('b'), chatProvider('c')],
      selector: roundRobin(),
      resilience: NOTHING,
      runtime,
    });

    const winners: string[] = [];
    for (const prompt of ['1', '2', '3', '4']) {
      winners.push((await settle(runtime, layer.callWithMeta('chat', { prompt }))).provider);
    }
    expect(winners, 'exact sequence: no clock, no PRNG, pure rotation').toEqual([
      'a',
      'b',
      'c',
      'a',
    ]);
    await layer.close();
  });

  it('DOCUMENTED SURPRISE: one selector instance is shared by every capability', async () => {
    // Capabilities without a `perCapability` override all run on the SAME
    // default stack, and therefore on the same `roundRobin()` closure. The
    // rotation counter is global to the layer, not per capability: an `embed`
    // call advances the position that the next `chat` call will start from.
    const runtime = createFakeRuntime();
    const both = (id: string) =>
      defineProvider(ai, {
        id,
        capabilities: {
          chat: async (input) => ({ text: input.prompt, by: id }),
          embed: async (input) => ({ vectors: input.texts.map(() => [1]), by: id }),
        },
      });
    const layer = createLayer({
      contract: ai,
      providers: [both('a'), both('b')],
      selector: roundRobin(),
      resilience: NOTHING,
      runtime,
    });

    const first = await settle(runtime, layer.callWithMeta('chat', { prompt: '1' }));
    const middle = await settle(runtime, layer.callWithMeta('embed', { texts: ['t'] }));
    const third = await settle(runtime, layer.callWithMeta('chat', { prompt: '2' }));

    // Two consecutive `chat` calls both land on 'a' because the `embed` call in
    // between consumed the rotation that would otherwise have moved chat to 'b'.
    expect([first.provider, middle.provider, third.provider]).toEqual(['a', 'b', 'a']);
    await layer.close();
  });

  it('weighted() draws from the injected PRNG, so the permutation is exact', async () => {
    // `scriptedRandom` pins the draw; nothing else in this configuration
    // consumes `runtime.random` (retry is off, so there is no jitter).
    const runtime = createFakeRuntime({ random: scriptedRandom([0.9, 0.1]) });
    const layer = createLayer({
      contract: ai,
      providers: [chatProvider('cheap'), chatProvider('premium')],
      selector: weighted({ weights: { cheap: 1, premium: 3 } }),
      resilience: NOTHING,
      runtime,
    });

    // r = 0.9 * 4 = 3.6 -> past `cheap`(1) into `premium`(3).
    expect((await settle(runtime, layer.callWithMeta('chat', { prompt: '1' }))).provider).toBe(
      'premium',
    );
    // r = 0.1 * 4 = 0.4 -> inside `cheap`(1).
    expect((await settle(runtime, layer.callWithMeta('chat', { prompt: '2' }))).provider).toBe(
      'cheap',
    );
    await layer.close();
  });

  it('weighted() keeps a zero-weight provider as a last-resort fallback', async () => {
    const runtime = createFakeRuntime({ random: scriptedRandom([0.5]) });
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'drained',
          capabilities: { chat: () => Promise.reject(new TransportError('drained is down')) },
        }),
        chatProvider('spare'),
      ],
      // `spare` weighs 0: never DRAWN, but never dropped either.
      selector: weighted({ weights: { drained: 1, spare: 0 } }),
      resilience: NOTHING,
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));
    expect(meta.provider, 'the zero-weight provider is still reachable').toBe('spare');
    expect(meta.errors.map((e) => e.providerId)).toEqual(['drained']);
    await layer.close();
  });

  it('sticky() pins a key to a provider and orders the rest behind it', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [chatProvider('openai'), chatProvider('anthropic'), chatProvider('voyage')],
      selector: sticky(),
      resilience: NOTHING,
      runtime,
    });

    const winnerFor = async (stickyKey: string): Promise<string> =>
      (await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }, { stickyKey }))).provider;

    // Rendezvous hashing: same key + same pool => byte-identical order, forever.
    expect(await winnerFor('user-1')).toBe('voyage');
    expect(await winnerFor('user-1')).toBe('voyage');
    expect(await winnerFor('user-2')).toBe('openai');

    // Removing a provider remaps only the keys that pointed AT it — the whole
    // reason this is rendezvous hashing and not `hash(key) % length`.
    layer.registry.unregister('voyage');
    expect(await winnerFor('user-1'), 'remapped, because its provider left').toBe('anthropic');
    expect(await winnerFor('user-2'), 'untouched').toBe('openai');

    // With no key at all it is the identity selector.
    expect((await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }))).provider).toBe(
      'openai',
    );
    await layer.close();
  });

  it('sticky() still falls back when the pinned provider fails', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        chatProvider('openai'),
        chatProvider('anthropic'),
        defineProvider(ai, {
          id: 'voyage',
          capabilities: { chat: () => Promise.reject(new TransportError('voyage is down')) },
        }),
      ],
      selector: sticky(),
      resilience: NOTHING,
      runtime,
    });

    // 'user-1' hashes to [voyage, anthropic, openai]; voyage is dead.
    const meta = await settle(
      runtime,
      layer.callWithMeta('chat', { prompt: 'x' }, { stickyKey: 'user-1' }),
    );
    expect(meta.provider).toBe('anthropic');
    expect(meta.errors.map((e) => e.providerId)).toEqual(['voyage']);
    await layer.close();
  });

  it('tags filter the pool, both as a selector and as a per-call hint', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        chatProvider('us-east', ['us']),
        chatProvider('eu-west', ['eu', 'gdpr']),
        chatProvider('eu-north', ['eu']),
      ],
      resilience: NOTHING,
      runtime,
    });

    // As a per-call hint — handled by `byHints`, whichever selector is installed.
    const eu = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }, { tags: ['eu'] }));
    expect(eu.provider).toBe('eu-west');

    // Conjunctive, not disjunctive: ALL tags must be present.
    const gdpr = await settle(
      runtime,
      layer.callWithMeta('chat', { prompt: 'x' }, { tags: ['eu', 'gdpr'] }),
    );
    expect(gdpr.provider).toBe('eu-west');

    // A tag nobody carries empties the pool, which is NO_PROVIDER — not a
    // silent fall-through to everything.
    const none = await rejectionOf(
      runtime,
      layer.call('chat', { prompt: 'x' }, { tags: ['moon'] }),
    );
    expect(none.code).toBe('NO_PROVIDER');
    await layer.close();
  });

  it('chainSelectors composes a filter with a rotation', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        chatProvider('slow-1', ['batch']),
        chatProvider('fast-1', ['realtime']),
        chatProvider('fast-2', ['realtime']),
      ],
      selector: chainSelectors(filterByTags(['realtime']), roundRobin()),
      resilience: NOTHING,
      runtime,
    });

    const winners: string[] = [];
    for (const prompt of ['1', '2', '3']) {
      winners.push((await settle(runtime, layer.callWithMeta('chat', { prompt }))).provider);
    }
    expect(winners, 'the batch provider is filtered out before rotation').toEqual([
      'fast-1',
      'fast-2',
      'fast-1',
    ]);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. maxProviders
 * ------------------------------------------------------------------ */

describe('integration — maxProviders', () => {
  const dead = (id: string) =>
    defineProvider(ai, {
      id,
      capabilities: { chat: () => Promise.reject(new TransportError(`${id} is down`)) },
    });

  it('stops the chain after N candidates and reports only those as tried', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [dead('a'), dead('b'), dead('c')],
      maxProviders: 2,
      resilience: NOTHING,
      runtime,
    });

    const advances: InterlayerEvents['fallback:advance'][] = [];
    layer.on('fallback:advance', (e) => advances.push(e));

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('ALL_FAILED');
    if (hasCode(error, 'ALL_FAILED')) {
      expect(error.triedProviderIds, "'c' was never entered").toEqual(['a', 'b']);
    }
    expect(
      advances.map((e) => e.toProviderId),
      'exactly one hop',
    ).toEqual(['b']);
    await layer.close();
  });

  it('maxProviders: 1 disables fallback, and the single failure stays unwrapped', async () => {
    const runtime = createFakeRuntime();
    const secondCalls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [
        dead('a'),
        defineProvider(ai, {
          id: 'b',
          capabilities: {
            chat: async (input) => {
              secondCalls.bump();
              return { text: input.prompt, by: 'b' };
            },
          },
        }),
      ],
      maxProviders: 1,
      resilience: NOTHING,
      runtime,
    });

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code, 'not wrapped in ALL_FAILED').toBe('TRANSPORT');
    expect(error.providerId).toBe('a');
    expect(secondCalls.count).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. Per-capability resilience
 * ------------------------------------------------------------------ */

describe('integration — perCapability overrides', () => {
  it('REPLACES the default for that capability rather than merging into it', async () => {
    const runtime = createFakeRuntime();
    const chatCalls = counter();
    const embedCalls = counter();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: () => {
              chatCalls.bump();
              return Promise.reject(new TransportError('down'));
            },
            embed: () => {
              embedCalls.bump();
              return Promise.reject(new TransportError('down'));
            },
          },
        }),
      ],
      resilience: { ...NOTHING, retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 } },
      perCapability: { embed: { retry: false } },
      runtime,
    });

    await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(chatCalls.count, 'the layer default applies').toBe(3);

    await rejectionOf(runtime, layer.call('embed', { texts: ['a'] }));
    expect(embedCalls.count, 'the override replaced the default outright').toBe(1);
    await layer.close();
  });

  it('DOCUMENTED SURPRISE: an override gets its own breaker, so provider health does not carry', async () => {
    // "A capability with an override gets its OWN breaker and limiter
    // instances" (LayerConfig). The consequence at runtime: a provider whose
    // breaker is OPEN for `chat` still receives `embed` traffic, because the
    // two stacks keep separate state for the same provider id. Whether that is
    // right is a policy question — but it is not what "one breaker per
    // provider" leads a reader to expect, and it is invisible from the outside.
    const runtime = createFakeRuntime();
    const chatCalls = counter();
    const embedCalls = counter();
    const breaker = { minimumThroughput: 2, failureRatio: 0.5, resetMs: 60_000 } as const;
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: () => {
              chatCalls.bump();
              return Promise.reject(new TransportError('down'));
            },
            embed: () => {
              embedCalls.bump();
              return Promise.reject(new TransportError('down'));
            },
          },
        }),
      ],
      resilience: { ...NOTHING, breaker },
      perCapability: { embed: { ...NOTHING, breaker } },
      runtime,
    });

    await rejectionOf(runtime, layer.call('chat', { prompt: '1' }));
    await rejectionOf(runtime, layer.call('chat', { prompt: '2' }));
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: '3' }))).code).toBe(
      'CIRCUIT_OPEN',
    );
    expect(chatCalls.count).toBe(2);

    // Same provider, same process, demonstrably unhealthy — and yet:
    const embedError = await rejectionOf(runtime, layer.call('embed', { texts: ['a'] }));
    expect(embedError.code, 'the embed breaker has never seen a failure').toBe('TRANSPORT');
    expect(embedCalls.count, 'the call really did reach the provider').toBe(1);
    await layer.close();
  });

  it('an override stack still honours only() and per-call provider hints', async () => {
    const runtime = createFakeRuntime();
    const embedder = (id: string) =>
      defineProvider(ai, {
        id,
        capabilities: { embed: async (input) => ({ vectors: input.texts.map(() => [1]), by: id }) },
      });
    const layer = createLayer({
      contract: ai,
      providers: [embedder('primary'), embedder('secondary')],
      resilience: { ...NOTHING, retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 1 } },
      perCapability: { embed: { ...NOTHING, retry: false } },
      runtime,
    });

    // The pin is applied by `byHints`, which every router chains ahead of its
    // own selector — including the one built for the override.
    const pinned = await settle(
      runtime,
      layer.only('secondary').callWithMeta('embed', { texts: ['a'] }),
    );
    expect(pinned.provider).toBe('secondary');

    const hinted = await settle(
      runtime,
      layer.callWithMeta('embed', { texts: ['a'] }, { providers: ['secondary', 'primary'] }),
    );
    expect(hinted.provider).toBe('secondary');
    await layer.close();
  });

  it('an override is built lazily and disposed by close()', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: async (input) => ({ text: input.prompt, by: 'p' }),
            embed: async (input) => ({ vectors: input.texts.map(() => [1]), by: 'p' }),
          },
        }),
      ],
      perCapability: { embed: { timeout: { attemptTimeoutMs: 2_000 } } },
      runtime,
    });

    // Two calls on the overridden capability reuse ONE stack (the second must
    // not rebuild it, or every call would get a fresh breaker and limiter).
    await settle(runtime, layer.call('embed', { texts: ['a'] }));
    await settle(runtime, layer.call('embed', { texts: ['b'] }));
    await settle(runtime, layer.call('chat', { prompt: 'x' }));

    await layer.close();
    expect(runtime.pendingTimers, 'both stacks released their timers').toBe(0);
  });
});

/* ------------------------------------------------------------------ *
 * 4. health — the surface nothing calls
 * ------------------------------------------------------------------ */

describe('integration — health probes', () => {
  it('DEAD SURFACE: `health` is carried onto the record and never consulted', async () => {
    // `ProviderDefinition.health` type-checks, survives erasure onto
    // `ProviderRecord`, and is documented as "never called on the hot path".
    // It is also never called on any COLD path: nothing in the registry, the
    // router, the selectors or the facade reads it, and the public `Layer` has
    // no verb that would. A consumer who declares one gets nothing for it —
    // an unhealthy provider is still selected first.
    const runtime = createFakeRuntime();
    const probes = counter();
    const sick = defineProvider(ai, {
      id: 'sick',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'sick' }) },
      health: async () => {
        probes.bump();
        return { healthy: false, detail: 'disk full' };
      },
    });
    const layer = createLayer({
      contract: ai,
      providers: [sick, chatProvider('well')],
      resilience: NOTHING,
      runtime,
    });

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));
    expect(meta.provider, 'self-declared unhealthy, selected anyway').toBe('sick');
    expect(probes.count, 'the probe was never invoked').toBe(0);

    // The only way to reach it today is to drive it yourself off the record.
    const probe = layer.providers.at(0)?.health;
    expect(typeof probe).toBe('function');
    if (probe !== undefined) {
      const status = await probe({ runtime, signal: new AbortController().signal });
      expect(status).toEqual({ healthy: false, detail: 'disk full' });
      expect(probes.count).toBe(1);
    }
    await layer.close();
  });
});
