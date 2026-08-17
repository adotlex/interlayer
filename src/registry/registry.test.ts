/**
 * The provider registry.
 *
 * Nothing in this file sleeps, reads the wall clock or draws a random number:
 * the registry has no time-dependent behaviour, so its tests need no fake
 * timers to be deterministic. Ordering is `priority` ascending with ties broken
 * by registration index, which is a pure function of the call sequence.
 *
 * The negative cases use `@ts-expect-error`; an UNUSED directive (TS2578) means
 * the case is not firing and the test proves nothing.
 */

import { describe, expect, it } from 'vitest';
import { fakeProvider, testContext } from '../../test/support/index.ts';
import { ConfigError } from '../core/errors.ts';
import type { ImplementedBy, Provider, ProviderId, ProviderRecord } from '../core/types.ts';
import { capability, defineContract } from './capability.ts';
import { defineProvider } from './provider.ts';
import { createRegistry } from './registry.ts';

/* ---------- type-level assertion helpers ---------- */

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

/* ---------- contract & providers ---------- */

interface ChatIn {
  readonly prompt: string;
}
interface ChatOut {
  readonly text: string;
  readonly tokens: number;
}
interface EmbedIn {
  readonly texts: readonly string[];
}
interface EmbedOut {
  readonly vectors: readonly (readonly number[])[];
}
interface TranscribeIn {
  readonly audio: string;
}
interface TranscribeOut {
  readonly transcript: string;
}

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>({ idempotent: false }),
  embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
  transcribe: capability<TranscribeIn, TranscribeOut>({ idempotent: true }),
});
type Ai = typeof ai;

/** A second, structurally identical `openai` — for duplicate-id checks. */
function makeOpenai() {
  return defineProvider(ai, {
    id: 'openai',
    capabilities: {
      chat: async (input, ctx) => ({ text: `openai:${input.prompt}`, tokens: ctx.attempt }),
      embed: async (input) => ({ vectors: input.texts.map(() => [0.1]) }),
    },
    traits: { tags: ['paid'], costPerCall: 3 },
  });
}

const openai = defineProvider(ai, {
  id: 'openai',
  capabilities: {
    chat: async (input, ctx) => ({ text: `openai:${input.prompt}`, tokens: ctx.attempt }),
    embed: async (input) => ({ vectors: input.texts.map(() => [0.1]) }),
  },
  traits: { tags: ['paid'], costPerCall: 3 },
});

const anthropic = defineProvider(ai, {
  id: 'anthropic',
  capabilities: {
    chat: async (input) => ({ text: `anthropic:${input.prompt}`, tokens: 1 }),
  },
});

const deepgram = defineProvider(ai, {
  id: 'deepgram',
  capabilities: {
    transcribe: async (input) => ({ transcript: input.audio }),
  },
});

const other = defineContract({
  ping: capability<{ readonly n: number }, { readonly ok: boolean }>(),
});
const pinger = defineProvider(other, {
  id: 'pinger',
  capabilities: { ping: async () => ({ ok: true }) },
});

function ids(records: readonly ProviderRecord[]): readonly string[] {
  return records.map((r) => r.id);
}

/* ---------- N: negative cases. Compiling IS the assertion. ---------- */

function negativeCases(): unknown[] {
  const registry = createRegistry({ contract: ai });

  // N1 — a provider built against a different contract.
  // @ts-expect-error 'ping' is not assignable to 'Partial<Handlers<Ai>>'
  registry.register(pinger);

  // N2 — a register option with the wrong value type.
  // @ts-expect-error 'high' is not assignable to 'number | undefined'
  registry.register(anthropic, { priority: 'high' });

  // N3 — a register option that does not exist.
  // @ts-expect-error 'weight' does not exist in type 'RegisterOptions'
  registry.register(deepgram, { weight: 2 });

  // N4 — `setEnabled` is keyed by provider id, and ids are strings.
  // @ts-expect-error 7 is not assignable to 'string'
  registry.setEnabled(7, false);

  return [registry];
}

/* ---------- P: positive type assertions ---------- */

type P1_RegistryIsContractBound = Expect<
  Equal<
    ReturnType<ReturnType<typeof createRegistry<Ai>>['candidatesFor']>,
    readonly ProviderRecord[]
  >
>;
type P2_SatisfiesKeepsIds = Expect<
  Equal<ProviderId<typeof openai | typeof anthropic>, 'openai' | 'anthropic'>
>;
type P3_SatisfiesKeepsCapabilities = Expect<
  Equal<ImplementedBy<typeof openai | typeof anthropic>, 'chat' | 'embed'>
>;

const typeAssertions: [
  P1_RegistryIsContractBound,
  P2_SatisfiesKeepsIds,
  P3_SatisfiesKeepsCapabilities,
] = [true, true, true];

/* ---------- runtime ---------- */

describe('createRegistry() — identity', () => {
  it('enforces provider id uniqueness', () => {
    const registry = createRegistry({ contract: ai }).register(openai);
    expect(() => registry.register(makeOpenai())).toThrow(ConfigError);
    try {
      registry.register(makeOpenai());
      expect.unreachable('a duplicate id must be rejected');
    } catch (e) {
      expect(e).toBeInstanceOf(ConfigError);
      expect((e as ConfigError).code).toBe('CONFIG');
      expect((e as ConfigError).providerId).toBe('openai');
      expect((e as ConfigError).message).toContain('duplicate provider id');
    }
  });

  it('leaves the incumbent untouched when a duplicate is rejected', () => {
    const registry = createRegistry({ contract: ai }).register(openai, { priority: 5 });
    expect(() => registry.register(makeOpenai(), { priority: 1 })).toThrow(ConfigError);
    expect(registry.get('openai')?.priority).toBe(5);
    expect(registry.list()).toHaveLength(1);
  });

  it('allows the id back after an unregister', () => {
    const registry = createRegistry({ contract: ai }).register(openai);
    expect(registry.unregister('openai')).toBe(true);
    expect(() => registry.register(makeOpenai())).not.toThrow();
    expect(ids(registry.list())).toEqual(['openai']);
  });

  it('returns itself from register, so registration chains', () => {
    const registry = createRegistry({ contract: ai });
    expect(registry.register(openai).register(anthropic)).toBe(registry);
    expect(ids(registry.list())).toEqual(['openai', 'anthropic']);
  });

  it('rejects a non-object provider', () => {
    const registry = createRegistry({ contract: ai });
    expect(() => registry.register(null as unknown as Provider<Ai>)).toThrow(
      /expects a provider object/,
    );
  });
});

describe('createRegistry() — ordering', () => {
  it('defaults priority to the registration index, so array order is fallback order', () => {
    const registry = createRegistry({ contract: ai })
      .register(openai)
      .register(anthropic)
      .register(deepgram);
    expect(registry.get('openai')?.priority).toBe(0);
    expect(registry.get('anthropic')?.priority).toBe(1);
    expect(registry.get('deepgram')?.priority).toBe(2);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['openai', 'anthropic']);
  });

  it('sorts by priority ascending — lower runs first', () => {
    const registry = createRegistry({ contract: ai })
      .register(openai, { priority: 10 })
      .register(anthropic, { priority: -1 });
    expect(ids(registry.candidatesFor('chat'))).toEqual(['anthropic', 'openai']);
    expect(ids(registry.list())).toEqual(['anthropic', 'openai']);
  });

  it('breaks priority ties by registration order, stably', () => {
    const chatter = (id: string): Provider<Ai> =>
      defineProvider(ai, {
        id,
        capabilities: { chat: async () => ({ text: id, tokens: 0 }) },
      });
    const registry = createRegistry({ contract: ai })
      .register(chatter('a'), { priority: 1 })
      .register(chatter('b'), { priority: 1 })
      .register(chatter('c'), { priority: 0 });
    expect(ids(registry.candidatesFor('chat'))).toEqual(['c', 'a', 'b']);
  });

  it('accepts an already-erased ProviderRecord from the shared test harness', () => {
    // `Registry.register` is typed for a `Provider`; a pre-erased record needs
    // one cast at the boundary. The erasure re-reads it either way.
    const record = fakeProvider('fake', { capabilities: ['chat'] }).record;
    const registry = createRegistry<Ai>().register(record as unknown as Provider<Ai>);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['fake']);
    expect(registry.get('fake')?.capabilities.has('chat')).toBe(true);
  });

  it('produces the same order for the same registration sequence, every time', () => {
    const build = (): readonly string[] =>
      ids(
        createRegistry({ contract: ai })
          .register(openai, { priority: 2 })
          .register(anthropic, { priority: 2 })
          .register(deepgram, { priority: 1 })
          .list(),
      );
    expect(build()).toEqual(['deepgram', 'openai', 'anthropic']);
    expect(build()).toEqual(build());
  });
});

describe('createRegistry() — capability negotiation', () => {
  it('returns only providers implementing the capability', () => {
    const registry = createRegistry({ contract: ai })
      .register(openai)
      .register(anthropic)
      .register(deepgram);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['openai', 'anthropic']);
    expect(ids(registry.candidatesFor('embed'))).toEqual(['openai']);
    expect(ids(registry.candidatesFor('transcribe'))).toEqual(['deepgram']);
  });

  it('returns an empty list for a capability nobody implements', () => {
    const registry = createRegistry({ contract: ai }).register(anthropic);
    expect(registry.candidatesFor('embed')).toEqual([]);
    expect(registry.candidatesFor('nonsense')).toEqual([]);
  });

  it('hands back a fresh array the caller cannot use to mutate the registry', () => {
    const registry = createRegistry({ contract: ai }).register(openai).register(anthropic);
    const first = registry.candidatesFor('chat');
    expect(registry.candidatesFor('chat')).not.toBe(first);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['openai', 'anthropic']);
  });

  it('keeps the erased handlers callable end to end', async () => {
    const registry = createRegistry({ contract: ai }).register(openai);
    const record = registry.candidatesFor('chat')[0];
    expect(record).toBeDefined();
    const handler = record?.capabilities.get('chat');
    expect(handler).toBeDefined();
    const h = testContext({ capability: 'chat', provider: record, attempt: 3 });
    expect(await (handler as NonNullable<typeof handler>)({ prompt: 'hey' }, h.attempt)).toEqual({
      text: 'openai:hey',
      tokens: 3,
    });
  });

  it('drops a capability from the index when its last provider is unregistered', () => {
    const registry = createRegistry({ contract: ai }).register(openai).register(anthropic);
    expect(registry.supports('embed')).toBe(true);
    expect(registry.unregister('openai')).toBe(true);
    expect(registry.supports('embed')).toBe(false);
    expect(registry.supports('chat')).toBe(true);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['anthropic']);
  });

  it('returns false from unregister for an id it never held', () => {
    expect(createRegistry({ contract: ai }).unregister('ghost')).toBe(false);
  });
});

describe('createRegistry() — enable / disable', () => {
  it('removes a disabled provider from the candidate list', () => {
    const registry = createRegistry({ contract: ai }).register(openai).register(anthropic);
    expect(registry.setEnabled('openai', false)).toBe(true);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['anthropic']);
    expect(registry.setEnabled('openai', true)).toBe(true);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['openai', 'anthropic']);
  });

  it('keeps a disabled provider visible to get() and list()', () => {
    const registry = createRegistry({ contract: ai }).register(openai);
    registry.setEnabled('openai', false);
    expect(registry.get('openai')?.enabled).toBe(false);
    expect(ids(registry.list())).toEqual(['openai']);
  });

  it('honours enabled:false at registration time', () => {
    const registry = createRegistry({ contract: ai }).register(openai, { enabled: false });
    expect(registry.candidatesFor('chat')).toEqual([]);
    expect(registry.supports('chat')).toBe(true);
  });

  it('keeps supports() independent of enabled — the two drive different errors', () => {
    // "nobody declares it"          -> UNSUPPORTED_CAPABILITY
    // "declared, none available now" -> NO_PROVIDER
    // Folding `enabled` into supports() would collapse that distinction.
    const registry = createRegistry({ contract: ai }).register(openai);
    registry.setEnabled('openai', false);
    expect(registry.supports('chat')).toBe(true);
    expect(registry.candidatesFor('chat')).toEqual([]);
    expect(registry.supports('transcribe')).toBe(false);
  });

  it('returns false from setEnabled for an unknown id', () => {
    expect(createRegistry({ contract: ai }).setEnabled('ghost', false)).toBe(false);
  });

  it('reflects a record mutated directly, because enabled is read at call time', () => {
    const registry = createRegistry({ contract: ai }).register(openai).register(anthropic);
    const record = registry.get('openai');
    expect(record).toBeDefined();
    if (record !== undefined) record.enabled = false;
    expect(ids(registry.candidatesFor('chat'))).toEqual(['anthropic']);
  });
});

describe('createRegistry() — the runtime backstop (R6 risk R1)', () => {
  /**
   * `readonly Provider<Ai>[]` collapses ids to `string` and fills `H` with the
   * constraint, so the TYPE claims all three capabilities. `provider.test.ts`
   * pins that over-report as a type assertion; here is the consequence, and the
   * defence: the registry derives its capability map from the runtime value.
   */
  const annotated: readonly Provider<Ai>[] = [openai, anthropic];
  type OverReported = Expect<Equal<ImplementedBy<(typeof annotated)[number]>, keyof Ai>>;
  const overReportPinned: [OverReported] = [true];

  it('reports what a provider can actually serve, not what its type claims', () => {
    expect(overReportPinned[0]).toBe(true);
    const registry = createRegistry({ contract: ai });
    for (const p of annotated) registry.register(p);
    // The type said 'transcribe' was implemented. The values disagree.
    expect(registry.supports('transcribe')).toBe(false);
    expect(registry.candidatesFor('transcribe')).toEqual([]);
    expect(ids(registry.candidatesFor('chat'))).toEqual(['openai', 'anthropic']);
    expect(ids(registry.candidatesFor('embed'))).toEqual(['openai']);
  });

  it('rejects a provider that claims a capability it does not implement', () => {
    const liar = {
      id: 'liar',
      capabilities: {
        chat: async (): Promise<ChatOut> => ({ text: 'real', tokens: 1 }),
        // Claimed, present, and useless. Accepting it would fail OPEN: the
        // router would select this provider for 'embed' and then explode.
        embed: { almost: 'a handler' },
      },
    } as unknown as Provider<Ai>;
    const registry = createRegistry({ contract: ai });
    expect(() => registry.register(liar)).toThrow(ConfigError);
    try {
      registry.register(liar);
      expect.unreachable('the registry must reject a provider that cannot serve its claim');
    } catch (e) {
      expect(e).toBeInstanceOf(ConfigError);
      expect((e as ConfigError).providerId).toBe('liar');
      expect((e as ConfigError).capability).toBe('embed');
      expect((e as ConfigError).message).toContain('not a function');
    }
    // And nothing partial was left behind.
    expect(registry.get('liar')).toBeUndefined();
    expect(registry.supports('chat')).toBe(false);
  });

  it('rejects a capability the contract does not declare, when it knows the contract', () => {
    const rogue = {
      id: 'rogue',
      capabilities: { summarise: async (): Promise<unknown> => undefined },
    } as unknown as Provider<Ai>;
    expect(() => createRegistry({ contract: ai }).register(rogue)).toThrow(
      /the contract does not declare/,
    );
  });

  it('accepts any capability name when built without a contract', () => {
    const rogue = {
      id: 'rogue',
      capabilities: { summarise: async (): Promise<unknown> => undefined },
    } as unknown as Provider<Ai>;
    const registry = createRegistry<Ai>().register(rogue);
    expect(registry.supports('summarise')).toBe(true);
  });

  it('rejects a provider that implements nothing at all', () => {
    const empty = { id: 'empty', capabilities: {} } as unknown as Provider<Ai>;
    expect(() => createRegistry({ contract: ai }).register(empty)).toThrow(
      /implements no capabilities/,
    );
  });
});

describe('createRegistry() — traits', () => {
  it('takes traits from the provider by default', () => {
    const registry = createRegistry({ contract: ai }).register(openai);
    expect(registry.get('openai')?.traits).toEqual({ tags: ['paid'], costPerCall: 3 });
  });

  it('lets registration options override them', () => {
    const registry = createRegistry({ contract: ai }).register(openai, {
      traits: { region: 'eu' },
    });
    expect(registry.get('openai')?.traits).toEqual({ region: 'eu' });
  });

  it('defaults to an empty traits object, never undefined', () => {
    const registry = createRegistry({ contract: ai }).register(anthropic);
    expect(registry.get('anthropic')?.traits).toEqual({});
  });
});

describe('createRegistry() — disposal', () => {
  it('disposes every provider exactly once, in reverse registration order', async () => {
    const order: string[] = [];
    const make = (id: string): Provider<Ai> =>
      ({
        id,
        capabilities: { chat: async (): Promise<ChatOut> => ({ text: id, tokens: 0 }) },
        dispose: (): void => {
          order.push(id);
        },
      }) as unknown as Provider<Ai>;

    const registry = createRegistry({ contract: ai })
      .register(make('a'))
      .register(make('b'))
      .register(make('c'));
    await registry.dispose();
    expect(order).toEqual(['c', 'b', 'a']);
  });

  it('awaits an async dispose', async () => {
    const seen: string[] = [];
    const slow = {
      id: 'slow',
      capabilities: { chat: async (): Promise<ChatOut> => ({ text: '', tokens: 0 }) },
      dispose: async (): Promise<void> => {
        await Promise.resolve();
        seen.push('done');
      },
    } as unknown as Provider<Ai>;
    const registry = createRegistry({ contract: ai }).register(slow);
    await registry.dispose();
    expect(seen).toEqual(['done']);
  });

  it('empties the registry', async () => {
    const registry = createRegistry({ contract: ai }).register(openai).register(anthropic);
    await registry.dispose();
    expect(registry.list()).toEqual([]);
    expect(registry.get('openai')).toBeUndefined();
    expect(registry.supports('chat')).toBe(false);
    expect(registry.candidatesFor('chat')).toEqual([]);
  });

  it('is idempotent — a second dispose is a no-op', async () => {
    let disposals = 0;
    const counted = {
      id: 'counted',
      capabilities: { chat: async (): Promise<ChatOut> => ({ text: '', tokens: 0 }) },
      dispose: (): void => {
        disposals++;
      },
    } as unknown as Provider<Ai>;
    const registry = createRegistry({ contract: ai }).register(counted);
    await registry.dispose();
    await registry.dispose();
    expect(disposals).toBe(1);
  });

  it('never lets one broken teardown strand the others', async () => {
    const disposed: string[] = [];
    const make = (id: string, explode: boolean): Provider<Ai> =>
      ({
        id,
        capabilities: { chat: async (): Promise<ChatOut> => ({ text: id, tokens: 0 }) },
        dispose: (): void => {
          disposed.push(id);
          if (explode) throw new Error(`${id} teardown exploded`);
        },
      }) as unknown as Provider<Ai>;

    const registry = createRegistry({ contract: ai })
      .register(make('a', false))
      .register(make('b', true))
      .register(make('c', false));
    await expect(registry.dispose()).resolves.toBeUndefined();
    expect(disposed).toEqual(['c', 'b', 'a']);
  });

  it('tolerates providers with no dispose hook', async () => {
    const registry = createRegistry({ contract: ai }).register(anthropic);
    await expect(registry.dispose()).resolves.toBeUndefined();
  });

  it('refuses further registration once disposed', async () => {
    const registry = createRegistry({ contract: ai });
    await registry.dispose();
    expect(() => registry.register(openai)).toThrow(/disposed/);
  });
});

describe('compile-time guarantees', () => {
  it('holds all 3 positive type assertions', () => {
    expect(typeAssertions).toHaveLength(3);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the 4 negative cases compiled but unexecuted', () => {
    expect(typeof negativeCases).toBe('function');
  });
});
