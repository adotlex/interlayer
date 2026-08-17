/**
 * Provider definition, introspection, and the single type-erasure site.
 *
 * The negative cases use `@ts-expect-error`; an UNUSED directive (TS2578) means
 * the case is not firing and the test is worthless. Treat it as a failure.
 */

import { describe, expect, it } from 'vitest';
import { testContext } from '../../test/support/index.ts';
import { ConfigError } from '../core/errors.ts';
import type { ImplementedBy, Provider, ProviderId, ProviderRecord } from '../core/types.ts';
import { capability, defineContract } from './capability.ts';
import {
  defineProvider,
  implementedCapabilities,
  type ProviderLike,
  supports,
  toProviderRecord,
} from './provider.ts';

/* ---------- type-level assertion helpers ---------- */

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

/* ---------- contract ---------- */

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

/**
 * Zero parameter annotations anywhere below: `input` and the return type both
 * come from the contract. That is the whole point of `defineProvider`.
 */
const openai = defineProvider(ai, {
  id: 'openai',
  capabilities: {
    chat: async (input, ctx) => ({ text: `openai:${input.prompt}`, tokens: ctx.attempt }),
    embed: async (input) => ({ vectors: input.texts.map(() => [0.1, 0.2]) }),
  },
  traits: { tags: ['paid'], costPerCall: 3 },
});

const anthropic = defineProvider(ai, {
  id: 'anthropic',
  capabilities: {
    chat: async (input) => ({ text: `anthropic:${input.prompt}`, tokens: 1 }),
  },
});

/* ---------- P: positive type assertions ---------- */

type P1_IdStaysLiteral = Expect<Equal<ProviderId<typeof openai>, 'openai'>>;
type P2_ImplementedSetIsExact = Expect<Equal<ImplementedBy<typeof openai>, 'chat' | 'embed'>>;
type P3_PartialProviderIsFine = Expect<Equal<ImplementedBy<typeof anthropic>, 'chat'>>;

/**
 * `satisfies` checks the shape WITHOUT collapsing anything. This is the form
 * every caller should use when hoisting providers into an array.
 */
const preserved = [openai, anthropic] satisfies readonly Provider<Ai>[];
type P4_SatisfiesKeepsIds = Expect<
  Equal<ProviderId<(typeof preserved)[number]>, 'openai' | 'anthropic'>
>;
type P5_SatisfiesKeepsCapabilities = Expect<
  Equal<ImplementedBy<(typeof preserved)[number]>, 'chat' | 'embed'>
>;

/**
 * THE TRAP (R6 risk R1), pinned as a type so it can never be "fixed" by
 * accident. Annotating collapses `Id` to `string` and fills `H` with the
 * constraint, so the type claims EVERY capability in the contract — including
 * `transcribe`, which neither provider implements. It fails OPEN.
 */
const annotated: readonly Provider<Ai>[] = [openai, anthropic];
type P6_AnnotationCollapsesIds = Expect<Equal<ProviderId<(typeof annotated)[number]>, string>>;
type P7_AnnotationOverReports = Expect<
  Equal<ImplementedBy<(typeof annotated)[number]>, 'chat' | 'embed' | 'transcribe'>
>;

/* ---------- N: negative cases. Compiling IS the assertion. ---------- */

function negativeCases(): unknown[] {
  const out: unknown[] = [];

  // N1 — a handler whose return type disagrees with the contract.
  out.push(
    defineProvider(ai, {
      id: 'broken-output',
      capabilities: {
        // @ts-expect-error 'tokens' is missing in the returned object
        chat: async (input) => ({ text: input.prompt }),
      },
    }),
  );

  // N2 — a handler reading a field its input type does not have.
  out.push(
    defineProvider(ai, {
      id: 'broken-input',
      capabilities: {
        // @ts-expect-error Property 'texts' does not exist on type 'ChatIn'
        chat: async (input) => ({ text: input.texts[0] ?? '', tokens: 0 }),
      },
    }),
  );

  // N3 — a capability name the contract does not declare.
  out.push(
    defineProvider(ai, {
      id: 'typo',
      // @ts-expect-error 'chatt' does not exist in 'Partial<Handlers<Ai>>'
      capabilities: { chatt: async () => ({ text: 'x', tokens: 0 }) },
    }),
  );

  // N4 — identity is `id`, not `name`. This is load-bearing: `providerId`
  //      appears in every error context and every event payload.
  out.push(
    defineProvider(ai, {
      // @ts-expect-error 'name' does not exist in type 'ProviderDefinition'
      name: 'openai',
      capabilities: { chat: async (input: ChatIn) => ({ text: input.prompt, tokens: 0 }) },
    }),
  );

  return out;
}

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  P1_IdStaysLiteral,
  P2_ImplementedSetIsExact,
  P3_PartialProviderIsFine,
  P4_SatisfiesKeepsIds,
  P5_SatisfiesKeepsCapabilities,
  P6_AnnotationCollapsesIds,
  P7_AnnotationOverReports,
] = [true, true, true, true, true, true, true];

/* ---------- helpers ---------- */

const registration = { priority: 0, enabled: true, traits: {} } as const;

/* ---------- runtime ---------- */

describe('defineProvider()', () => {
  it('is identity at runtime — it exists to supply contextual types', () => {
    const definition = {
      id: 'x',
      capabilities: { chat: async (input: ChatIn) => ({ text: input.prompt, tokens: 0 }) },
    };
    expect(defineProvider(ai, definition)).toBe(definition);
  });

  it('rejects an empty capability set — such a provider could never be selected', () => {
    expect(() => defineProvider(ai, { id: 'empty', capabilities: {} })).toThrow(
      /implements no capabilities/,
    );
  });

  it('rejects a blank id', () => {
    const def = { id: '   ', capabilities: { chat: async () => ({ text: '', tokens: 0 }) } };
    expect(() => defineProvider(ai, def)).toThrow(/non-empty string/);
  });

  it('rejects a capability the contract does not declare', () => {
    const rogue = {
      id: 'rogue',
      capabilities: { summarise: async () => ({ text: '', tokens: 0 }) },
    } as unknown as Parameters<typeof defineProvider<Ai, 'rogue', Record<never, never>>>[1];
    expect(() => defineProvider(ai, rogue)).toThrow(/the contract does not declare/);
  });
});

describe('implementedCapabilities() / supports()', () => {
  it('reads the runtime value of a typed provider', () => {
    expect(implementedCapabilities(openai)).toEqual(['chat', 'embed']);
    expect(supports(openai, 'chat')).toBe(true);
    expect(supports(openai, 'transcribe')).toBe(false);
  });

  it('reads a type-erased ProviderRecord just as well', () => {
    const record = toProviderRecord(openai, registration);
    expect(implementedCapabilities(record)).toEqual(['chat', 'embed']);
    expect(supports(record, 'embed')).toBe(true);
    expect(supports(record, 'transcribe')).toBe(false);
  });

  it('reports the TRUTH even when the static type over-reports', () => {
    // `annotated` claims 'transcribe' at the type level (P7). The value does
    // not have it, and introspection reads the value.
    const first = annotated[0];
    expect(first).toBeDefined();
    expect(implementedCapabilities(first as ProviderLike)).toEqual(['chat', 'embed']);
    expect(supports(first as ProviderLike, 'transcribe')).toBe(false);
  });

  it('does not count a key whose handler is missing', () => {
    const sparse = { id: 'sparse', capabilities: { chat: undefined, embed: async () => ({}) } };
    expect(implementedCapabilities(sparse)).toEqual(['embed']);
  });

  it('does not count a key whose handler is not callable', () => {
    const liar = { id: 'liar', capabilities: { chat: 'definitely a handler' } };
    expect(implementedCapabilities(liar)).toEqual([]);
    expect(supports(liar, 'chat')).toBe(false);
  });

  it('returns nothing for a malformed capability table', () => {
    expect(implementedCapabilities({ capabilities: null })).toEqual([]);
    expect(implementedCapabilities({ capabilities: 42 })).toEqual([]);
  });
});

describe('toProviderRecord() — the single type-erasure site', () => {
  it('builds the capability map from the VALUE, not from the type', () => {
    const record = toProviderRecord(openai, registration);
    expect([...record.capabilities.keys()]).toEqual(['chat', 'embed']);
    expect(record.capabilities.has('transcribe')).toBe(false);
  });

  it('produces handlers callable through the erased signature', async () => {
    const record = toProviderRecord(openai, registration);
    const handler = record.capabilities.get('chat');
    expect(handler).toBeDefined();
    const h = testContext({ capability: 'chat', provider: record, attempt: 2 });
    const result = await (handler as NonNullable<typeof handler>)({ prompt: 'hi' }, h.attempt);
    expect(result).toEqual({ text: 'openai:hi', tokens: 2 });
  });

  it('carries the resolved registration facts onto the record', () => {
    const record: ProviderRecord = toProviderRecord(openai, {
      priority: 7,
      enabled: false,
      traits: { region: 'eu' },
    });
    expect(record.id).toBe('openai');
    expect(record.priority).toBe(7);
    expect(record.enabled).toBe(false);
    expect(record.traits).toEqual({ region: 'eu' });
  });

  it('forwards health and dispose only when the provider supplies them', () => {
    const bare = toProviderRecord(anthropic, registration);
    expect(Object.hasOwn(bare, 'dispose')).toBe(false);
    expect(Object.hasOwn(bare, 'health')).toBe(false);

    const withHooks = toProviderRecord(
      {
        id: 'hooked',
        capabilities: { chat: async () => ({ text: '', tokens: 0 }) },
        dispose: () => undefined,
        health: async () => ({ healthy: true }),
      },
      registration,
    );
    expect(typeof withHooks.dispose).toBe('function');
    expect(typeof withHooks.health).toBe('function');
  });

  it('REJECTS a provider that claims a capability it does not implement', () => {
    // The runtime backstop for R6 risk R1. A non-callable handler under a real
    // capability name is the fail-open shape: the type says "yes", the value
    // cannot serve it. It must not reach the registry.
    const liar: ProviderLike = {
      id: 'liar',
      capabilities: {
        chat: async () => ({ text: 'real', tokens: 1 }),
        embed: 'I promise I can embed',
      },
    };
    expect(() => toProviderRecord(liar, registration)).toThrow(ConfigError);
    try {
      toProviderRecord(liar, registration);
      expect.unreachable('the erasure should have rejected the liar');
    } catch (e) {
      expect(e).toBeInstanceOf(ConfigError);
      expect((e as ConfigError).code).toBe('CONFIG');
      expect((e as ConfigError).providerId).toBe('liar');
      expect((e as ConfigError).capability).toBe('embed');
      expect((e as ConfigError).message).toContain('not a function');
    }
  });

  it('skips a capability explicitly set to undefined rather than rejecting it', () => {
    const partial: ProviderLike = {
      id: 'partial',
      capabilities: { chat: async () => ({ text: '', tokens: 0 }), embed: undefined },
    };
    expect([...toProviderRecord(partial, registration).capabilities.keys()]).toEqual(['chat']);
  });

  it('rejects a capability outside the allowed set when one is supplied', () => {
    const rogue: ProviderLike = {
      id: 'rogue',
      capabilities: { summarise: async () => undefined },
    };
    expect(() =>
      toProviderRecord(rogue, { ...registration, allowedCapabilities: ['chat', 'embed'] }),
    ).toThrow(/the contract does not declare/);
    expect(() => toProviderRecord(rogue, registration)).not.toThrow();
  });

  it('rejects an empty, absent or non-object capability table', () => {
    expect(() => toProviderRecord({ id: 'a', capabilities: {} }, registration)).toThrow(
      /implements no capabilities/,
    );
    expect(() => toProviderRecord({ id: 'b', capabilities: null }, registration)).toThrow(
      /no capabilities object/,
    );
    expect(() => toProviderRecord({ id: 'c', capabilities: 'chat' }, registration)).toThrow(
      /no capabilities object/,
    );
  });

  it('rejects a blank id and a non-finite priority', () => {
    const ok = { id: 'ok', capabilities: { chat: async () => undefined } };
    expect(() => toProviderRecord({ ...ok, id: '' }, registration)).toThrow(/non-empty string/);
    expect(() => toProviderRecord(ok, { ...registration, priority: Number.NaN })).toThrow(
      /non-finite priority/,
    );
  });

  it('accepts a Map-shaped capability table, so a record can be re-erased', () => {
    const record = toProviderRecord(openai, registration);
    const again = toProviderRecord(record, { ...registration, priority: 1 });
    expect([...again.capabilities.keys()]).toEqual(['chat', 'embed']);
  });
});

describe('compile-time guarantees', () => {
  it('holds all 7 positive type assertions', () => {
    expect(typeAssertions).toHaveLength(7);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the 4 negative cases compiled but unexecuted', () => {
    expect(typeof negativeCases).toBe('function');
  });
});
