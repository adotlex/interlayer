/**
 * The rest of the published facade: `only`, `supports`, `on`, `use`, `close`,
 * `Symbol.asyncDispose` — exercised the way a consumer reaches them, through
 * `src/index.ts`.
 *
 * Two tests in this file are marked KNOWN DEFECT and FAIL on purpose. They are
 * not soft assertions waiting to be relaxed; each one states the behaviour the
 * published contract promises and demonstrates that the code does something
 * else. See the T1 report.
 */

import { describe, expect, it } from 'vitest';
import {
  type CallMiddleware,
  capability,
  createEmitter,
  createLayer,
  defineContract,
  defineProvider,
  type InterlayerEvents,
  TransportError,
} from '../../src/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { counter, drainMicrotasks, rejectionOf, settle } from './harness.ts';

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

const ai = defineContract({
  chat: capability<ChatRequest, ChatReply>({ idempotent: false }),
  embed: capability<EmbedRequest, EmbedReply>({ idempotent: true }),
});

/** openai and anthropic do `chat`; voyage does `embed` and nothing else. */
function threeProviders() {
  return [
    defineProvider(ai, {
      id: 'openai',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'openai' }) },
    }),
    defineProvider(ai, {
      id: 'anthropic',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'anthropic' }) },
    }),
    defineProvider(ai, {
      id: 'voyage',
      capabilities: { embed: async (input) => ({ vectors: input.texts.map(() => [1]) }) },
    }),
  ] as const;
}

/* ------------------------------------------------------------------ *
 * 1. only()
 * ------------------------------------------------------------------ */

describe('integration — only()', () => {
  it('restricts the candidate set, in the order given, without touching the parent', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    const pinned = layer.only('anthropic');
    expect(pinned.providers.map((p) => p.id)).toEqual(['anthropic']);
    expect((await settle(runtime, pinned.call('chat', { prompt: 'x' }))).by).toBe('anthropic');

    // `only()` returns a VIEW. The parent still fans out over everything.
    expect((await settle(runtime, layer.call('chat', { prompt: 'x' }))).by).toBe('openai');
    expect(layer.providers.map((p) => p.id)).toEqual(['openai', 'anthropic', 'voyage']);

    // Pin order is the caller's fallback order, not registration order.
    expect(layer.only('anthropic', 'openai').providers.map((p) => p.id)).toEqual([
      'anthropic',
      'openai',
    ]);
    await layer.close();
  });

  it('composes by intersection — a restriction can never be widened again', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    expect(
      layer
        .only('anthropic')
        .only('anthropic', 'openai')
        .providers.map((p) => p.id),
    ).toEqual(['anthropic']);

    // A per-call `providers` hint is intersected with the pin too, so a hint
    // that contradicts the pin leaves NOTHING. Neither "the pin wins" nor "the
    // hint wins" is defensible — both would let a restriction evaporate.
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
    expect(meta.provider).toBe('openai');
    await layer.close();
  });

  it('pinning a provider that does not implement the capability is NO_PROVIDER', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    const error = await rejectionOf(runtime, layer.only('voyage').call('chat', { prompt: 'x' }));
    expect(error.code).toBe('NO_PROVIDER');
    // ... and it is NOT UNSUPPORTED_CAPABILITY: somebody does implement `chat`,
    // just not the provider the caller pinned. Two facts, two codes.
    await layer.close();
  });

  it('DOCUMENTED TRAP: only() with no arguments pins to the empty set', async () => {
    // `only(...ids)` is variadic, so `only()` type-checks and silently produces
    // a layer that can never route anything. A caller spreading a possibly-empty
    // array — `layer.only(...preferred)` — gets an outage rather than the
    // "no preference" they meant. Reported as ergonomic friction, not a defect:
    // the alternative (empty means everything) would let a typo widen a pin.
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    expect(layer.only().providers).toEqual([]);
    expect((await rejectionOf(runtime, layer.only().call('chat', { prompt: 'x' }))).code).toBe(
      'NO_PROVIDER',
    );
    await layer.close();
  });

  it('DOCUMENTED TRAP: closing a view closes the layer it was derived from', async () => {
    // `only()` is a view over the SAME registry, policies and `closed` flag, so
    // teardown is shared. `using pinned = layer.only('x')` inside a request
    // handler would tear down the whole application layer on scope exit.
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    await layer.only('anthropic').close();

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('UNSUPPORTED_CAPABILITY');
  });
});

/* ------------------------------------------------------------------ *
 * 2. supports()
 * ------------------------------------------------------------------ */

describe('integration — supports()', () => {
  it('type-guards a runtime string into the implemented capability union', async () => {
    const runtime = createFakeRuntime();
    const chatOnly = defineProvider(ai, {
      id: 'chat-only',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'chat-only' }) },
    });
    const layer = createLayer({ contract: ai, providers: [chatOnly], runtime });

    // The realistic shape: a capability name that arrived as configuration.
    const requested: string = 'chat';
    expect(layer.supports(requested)).toBe(true);
    if (layer.supports(requested)) {
      // Compiles ONLY because `supports` narrowed `string` to `'chat'`.
      const reply = await settle(runtime, layer.call(requested, { prompt: 'x' }));
      expect(reply.by).toBe('chat-only');
    }

    // Declared by the contract, implemented by nobody => not callable.
    expect(layer.supports('embed')).toBe(false);
    // Not in the contract at all.
    expect(layer.supports('summarise')).toBe(false);

    /**
     * Compile-time negatives. NEVER CALLED: every line is also a runtime
     * rejection, and running them would only add noise. `tsc` checks the
     * directives, so an assertion that stopped being true fails `npm run
     * typecheck` with TS2578.
     */
    const negatives = async (): Promise<void> => {
      const unchecked: string = 'chat';
      // @ts-expect-error — an unguarded string is not in the implemented set.
      await layer.call(unchecked, { prompt: 'x' });
      // @ts-expect-error — `embed` is declared but nobody implements it.
      await layer.call('embed', { texts: ['a'] });
      // @ts-expect-error — the input is checked against ChatRequest.
      await layer.call('chat', { promt: 'typo' });
      // @ts-expect-error — 'ghost' is not a registered provider id.
      layer.only('ghost');
    };
    expect(typeof negatives).toBe('function');
    await layer.close();
  });

  it('answers from the registry, so a provider added later is visible', async () => {
    const runtime = createFakeRuntime();
    const chatOnly = defineProvider(ai, {
      id: 'chat-only',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'chat-only' }) },
    });
    const layer = createLayer({ contract: ai, providers: [chatOnly], runtime });

    expect(layer.supports('embed')).toBe(false);
    layer.registry.register(
      defineProvider(ai, {
        id: 'voyage',
        capabilities: { embed: async (input) => ({ vectors: input.texts.map(() => [1]) }) },
      }),
    );
    // `supports()` is a RUNTIME question; the static `Impl` union was fixed at
    // `createLayer` time and does not grow. The guard is honest either way —
    // it just cannot narrow to a capability the type parameter never had.
    expect(layer.supports('embed')).toBe(true);
    await layer.close();
  });

  it('supports() ignores `enabled`, so NO_PROVIDER and UNSUPPORTED stay distinct', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    layer.registry.setEnabled('openai', false);
    layer.registry.setEnabled('anthropic', false);
    expect(layer.supports('chat'), 'still DECLARED, merely unavailable').toBe(true);
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }))).code).toBe(
      'NO_PROVIDER',
    );

    layer.registry.unregister('openai');
    layer.registry.unregister('anthropic');
    expect(layer.supports('chat'), 'now nothing declares it').toBe(false);
    expect((await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }))).code).toBe(
      'UNSUPPORTED_CAPABILITY',
    );
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. on() / unsubscribe
 * ------------------------------------------------------------------ */

describe('integration — on() and its unsubscribe closure', () => {
  it('delivers events, and the returned closure really stops delivery', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    const seen: string[] = [];
    const stop = layer.on('call:start', (e) => seen.push(e.capability));

    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(seen).toEqual(['chat']);

    stop();
    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(seen, 'no further delivery after unsubscribe').toEqual(['chat']);

    stop(); // idempotent: a second call is a no-op, not a throw
    expect(layer.events.listenerCount('call:start')).toBe(0);
    await layer.close();
  });

  it('registering the same function twice registers it once, and one closure removes it', async () => {
    // Listeners live in a `Set`, so registering the same function object twice
    // is idempotent — it fires ONCE per event, and either closure removes it.
    // Worth pinning: a caller who subscribes in a hot path (per request, say)
    // does not accumulate duplicate deliveries, but neither can they rely on
    // reference counting to keep the listener alive after one unsubscribe.
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    const hits = counter();
    const listener = (): void => {
      hits.bump();
    };
    const stopFirst = layer.on('call:start', listener);
    layer.on('call:start', listener);

    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    // Both registrations are the same function object, and a `Set` de-duplicates
    // it — so this is ONE listener, not two, and the closure removes it.
    expect(hits.count).toBe(1);

    stopFirst();
    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(hits.count).toBe(1);
    await layer.close();
  });

  it('a throwing listener is routed to listener:error and cannot break the call', async () => {
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });

    const reported: InterlayerEvents['listener:error'][] = [];
    layer.on('listener:error', (e) => reported.push(e));
    layer.on('call:start', () => {
      throw new Error('observer blew up');
    });

    const reply = await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(reply.by).toBe('openai');
    expect(reported).toHaveLength(1);
    expect(reported.at(0)?.event).toBe('call:start');
    await layer.close();
  });

  it('KNOWN DEFECT — close() wipes a SHARED emitter and silences other layers', async () => {
    // `LayerConfig.events` is documented as "Share an emitter across layers,
    // e.g. to fan every event into one sink". But `close()` ends with
    // `events.removeAllListeners()` — no argument, so it clears EVERY listener
    // and every wildcard on the emitter it was handed, including subscriptions
    // it did not create.
    //
    // The consequence in a real process: one layer shutting down (a tenant
    // going away, a feature flag flipping) silently blinds the shared metrics
    // sink for everything still running, with no error anywhere.
    //
    // A layer must only unsubscribe listeners it owns; an emitter it was GIVEN
    // is not its to empty.
    const runtime = createFakeRuntime();
    const shared = createEmitter<InterlayerEvents>();
    const seen: string[] = [];
    shared.on('call:success', (e) => seen.push(e.providerId));

    const first = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'first',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'first' }) },
        }),
      ],
      events: shared,
      runtime,
    });
    const second = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'second',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'second' }) },
        }),
      ],
      events: shared,
      runtime,
    });

    await settle(runtime, second.call('chat', { prompt: '1' }));
    expect(seen).toEqual(['second']);

    await first.close(); // the OTHER layer is closed; `second` is still serving

    await settle(runtime, second.call('chat', { prompt: '2' }));
    expect(seen, 'the shared sink must survive an unrelated layer closing').toEqual([
      'second',
      'second',
    ]);
    await second.close();
  });
});

/* ------------------------------------------------------------------ *
 * 4. use()
 * ------------------------------------------------------------------ */

describe('integration — use()', () => {
  it('wraps the whole call once, outside provider selection and retries', async () => {
    const runtime = createFakeRuntime();
    const attempts = counter();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'flaky',
          capabilities: {
            chat: async (input) => {
              if (attempts.bump() === 1) throw new TransportError('down');
              return { text: input.prompt, by: 'flaky' };
            },
          },
        }),
        defineProvider(ai, {
          id: 'backup',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'backup' }) },
        }),
      ],
      resilience: {
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 1 },
        timeout: false,
        breaker: false,
        rateLimit: false,
      },
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
    expect(layer.use(tracing), 'chainable').toBe(layer);

    await settle(runtime, layer.call('chat', { prompt: 'x' }));

    // ONCE per call, not once per attempt: two physical attempts happened
    // inside this single span. Provider identity is deliberately NOT available
    // here — the call stack runs once for the whole fallback chain, so the
    // provider belongs to the ATTEMPT scope.
    expect(spans).toEqual(['start:chat', 'end:chat']);
    expect(attempts.count).toBe(2);
    await layer.close();
  });

  it('DOCUMENTED WART: a short-circuiting middleware reports provider "<unknown>"', async () => {
    // Caching is the canonical reason to reach for middleware, and a cache hit
    // returns without calling `next()` — so no provider is ever selected and
    // `stats.winnerId` stays unset. The facade substitutes the literal string
    // `'<unknown>'`, which then travels on `callWithMeta().provider` AND on the
    // `call:success` event into whatever metrics pipeline is listening.
    const runtime = createFakeRuntime();
    const layer = createLayer({
      contract: ai,
      providers: threeProviders(),
      runtime,
    });

    const successes: InterlayerEvents['call:success'][] = [];
    layer.on('call:success', (e) => successes.push(e));
    layer.use(async () => ({ text: 'from-cache', by: 'cache' }));

    const meta = await settle(runtime, layer.callWithMeta('chat', { prompt: 'x' }));
    expect(meta.value).toEqual({ text: 'from-cache', by: 'cache' });
    expect(meta.attempts, 'nothing physical happened').toBe(0);
    expect(meta.provider).toBe('<unknown>');
    expect(successes.at(0)?.providerId).toBe('<unknown>');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 5. close() and async disposal
 * ------------------------------------------------------------------ */

describe('integration — close() and Symbol.asyncDispose', () => {
  it('disposes providers, drops listeners, leaves no timer, and is idempotent', async () => {
    const runtime = createFakeRuntime();
    const disposals = counter();
    const provider = defineProvider(ai, {
      id: 'p',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'p' }) },
      dispose: () => {
        disposals.bump();
      },
    });
    const layer = createLayer({ contract: ai, providers: [provider], runtime });
    layer.on('call:start', () => undefined);

    await settle(runtime, layer.call('chat', { prompt: 'x' }));
    expect(layer.events.listenerCount('call:start')).toBe(1);

    await layer.close();
    expect(disposals.count).toBe(1);
    expect(layer.events.listenerCount('call:start'), 'own listeners released').toBe(0);
    await drainMicrotasks();
    expect(runtime.pendingTimers).toBe(0);

    await layer.close();
    expect(disposals.count, 'idempotent: no second teardown').toBe(1);
  });

  it('`await using` releases everything at scope exit', async () => {
    const runtime = createFakeRuntime();
    const disposals = counter();
    const provider = defineProvider(ai, {
      id: 'p',
      capabilities: { chat: async (input) => ({ text: input.prompt, by: 'p' }) },
      dispose: () => {
        disposals.bump();
      },
    });

    {
      await using layer = createLayer({ contract: ai, providers: [provider], runtime });
      const reply = await settle(runtime, layer.call('chat', { prompt: 'x' }));
      expect(reply.by).toBe('p');
    } // `[Symbol.asyncDispose]()` runs here

    expect(disposals.count).toBe(1);
    expect(runtime.pendingTimers).toBe(0);
  });

  it('Symbol.asyncDispose is close() under another name', async () => {
    const runtime = createFakeRuntime();
    const disposals = counter();
    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: { chat: async (input) => ({ text: input.prompt, by: 'p' }) },
          dispose: () => {
            disposals.bump();
          },
        }),
      ],
      runtime,
    });

    await layer[Symbol.asyncDispose]();
    await layer.close();
    expect(disposals.count).toBe(1);
  });

  it('DOCUMENTED WART: calling a closed layer reports UNSUPPORTED_CAPABILITY', async () => {
    // `close()` disposes the registry, which empties it — so the facade's first
    // check (`registry.supports(capability)`) fails and the caller is told the
    // capability does not exist. It exists; the layer is shut. No `call:start`
    // or `call:failure` event is emitted either, because the throw happens
    // before the try block, so a metrics pipeline sees nothing at all.
    const runtime = createFakeRuntime();
    const layer = createLayer({ contract: ai, providers: threeProviders(), runtime });
    const names: string[] = [];
    layer.events.onAny((name) => {
      names.push(name);
    });

    await layer.close();

    const error = await rejectionOf(runtime, layer.call('chat', { prompt: 'x' }));
    expect(error.code).toBe('UNSUPPORTED_CAPABILITY');
    expect(names, 'listeners were removed by close(), so nothing is observable').toEqual([]);
  });
});
