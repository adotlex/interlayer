/**
 * OPEN QUESTION 2 — does R6's phantom-carrier inference still work on the
 * PINNED compiler, TypeScript 6.0.3?
 *
 * R6 verified the technique on tsc 5.9.3. The wave pins 6.0.3, so it is
 * re-verified here against the real `src/core/types.ts`.
 *
 * HOW TO READ A FAILURE. The negative cases below use `@ts-expect-error`. If
 * the technique breaks, the compiler stops raising the error and reports
 * `TS2578: Unused '@ts-expect-error' directive` — `npm run typecheck` turns
 * red. An unused directive is NOT a cosmetic warning: it means a bogus
 * capability name, or a wrong input type, is compiling clean and the entire
 * value proposition of the library has silently evaporated.
 */

import { describe, expect, it } from 'vitest';
import type {
  AnyContract,
  AttemptContext,
  Capability,
  CapabilityName,
  Contract,
  Handlers,
  ImplementedBy,
  InputOf,
  OutputOf,
  Provider,
  ProviderId,
} from '../../src/core/types.ts';
import { testContext } from './context.ts';

/* ---------- type-level assertion helpers ---------- */

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

/* ---------- an application's contract ---------- */

interface ChatIn {
  readonly prompt: string;
  readonly temperature?: number | undefined;
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

/**
 * The single cast in the whole design. `src/registry/capability.ts` (U1) owns
 * the shipped version — reproduced here so this file exercises the CORE TYPES
 * rather than a unit that does not exist yet.
 */
const capability = <In, Out>(
  meta: {
    readonly idempotent?: boolean | undefined;
    readonly description?: string | undefined;
  } = {},
): Capability<In, Out> => meta as Capability<In, Out>;

const defineContract = <C extends Contract<C>>(contract: C): C => contract;

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>({ idempotent: false }),
  embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
  transcribe: capability<TranscribeIn, TranscribeOut>({ idempotent: true }),
});
type Ai = typeof ai;

/* ---------- P: positive type assertions ---------- */

type P1_KeysStayLiteral = Expect<Equal<CapabilityName<Ai>, 'chat' | 'embed' | 'transcribe'>>;
type P2_InputCarried = Expect<Equal<InputOf<Ai, 'chat'>, ChatIn>>;
type P3_OutputCarried = Expect<Equal<OutputOf<Ai, 'chat'>, ChatOut>>;
type P4_SecondPairIndependent = Expect<Equal<OutputOf<Ai, 'embed'>, EmbedOut>>;

/**
 * The F-bounded form accepts an `interface` too — a bare `interface` does not
 * satisfy an index-signature constraint at all, which is why `Contract<C>` is
 * a mapped type rather than `Record<string, Capability>`.
 */
interface InterfaceContract {
  readonly chat: Capability<ChatIn, ChatOut>;
}
type P5_InterfaceKeysStayLiteral = Expect<Equal<CapabilityName<InterfaceContract>, 'chat'>>;
type P6_InterfaceInputCarried = Expect<Equal<InputOf<InterfaceContract, 'chat'>, ChatIn>>;

/**
 * THE TRAP, documented as a type. The erased index-signature form collapses to
 * `string`. This is what `interface X extends AnyContract` would give you, and
 * why the F-bounded constraint is mandatory.
 */
type P7_ErasedFormCollapses = Expect<Equal<CapabilityName<AnyContract>, string>>;

/* ---------- providers: handlers contextually typed, keys preserved ---------- */

function defineProvider<
  C extends Contract<C>,
  const Id extends string,
  H extends Partial<Handlers<C>>,
>(_contract: C, def: { readonly id: Id; readonly capabilities: H }): Provider<C, Id, H> {
  return def;
}

// No parameter annotations anywhere in here: `input` and the return type both
// come from the contract.
const openai = defineProvider(ai, {
  id: 'openai',
  capabilities: {
    chat: async (input, ctx) => ({ text: `openai:${input.prompt}`, tokens: ctx.attempt }),
    embed: async (input) => ({ vectors: input.texts.map(() => [0.1, 0.2]) }),
  },
});

const anthropic = defineProvider(ai, {
  id: 'anthropic',
  capabilities: {
    chat: async (input) => ({ text: `anthropic:${input.prompt}`, tokens: 1 }),
  },
});

type P8_ProviderIdStaysLiteral = Expect<Equal<ProviderId<typeof openai>, 'openai'>>;
type P9_ImplementedSetIsExact = Expect<
  Equal<ImplementedBy<typeof openai | typeof anthropic>, 'chat' | 'embed'>
>;
type P10_PartialProviderIsFine = Expect<Equal<ImplementedBy<typeof anthropic>, 'chat'>>;

/* ---------- a minimal layer, to check call-site inference end to end ---------- */

interface TestLayer<C extends Contract<C>, Ids extends string, Impl extends CapabilityName<C>> {
  readonly providerIds: readonly Ids[];
  call<K extends Impl>(
    capability: K,
    input: InputOf<C, K>,
    route?: readonly Ids[],
  ): Promise<OutputOf<C, K>>;
}

type ErasedHandlerMap = Record<
  string,
  ((input: unknown, ctx: AttemptContext) => Promise<unknown>) | undefined
>;

function createTestLayer<C extends Contract<C>, const P extends readonly Provider<C>[]>(
  _contract: C,
  providers: P,
): TestLayer<C, ProviderId<P[number]>, Extract<ImplementedBy<P[number]>, CapabilityName<C>>> {
  type Ids = ProviderId<P[number]>;
  type Impl = Extract<ImplementedBy<P[number]>, CapabilityName<C>>;
  return {
    providerIds: providers.map((p) => p.id) as unknown as readonly Ids[],
    async call<K extends Impl>(
      cap: K,
      input: InputOf<C, K>,
      route?: readonly Ids[],
    ): Promise<OutputOf<C, K>> {
      const pool =
        route === undefined
          ? providers
          : providers.filter((p) => (route as readonly string[]).includes(p.id));
      for (const p of pool) {
        const handlers = p.capabilities as unknown as ErasedHandlerMap;
        const handler = handlers[cap];
        if (handler === undefined) continue;
        const h = testContext({ capability: cap });
        return (await handler(input, h.attempt)) as OutputOf<C, K>;
      }
      throw new Error(`no provider implements "${cap}"`);
    },
  };
}

const layer = createTestLayer(ai, [openai, anthropic]);

type AiLayer = typeof layer;
type P11_LayerIdsAreLiteral = Expect<
  Equal<AiLayer['providerIds'], readonly ('openai' | 'anthropic')[]>
>;

/* ---------- N: negative cases. Compiling IS the assertion. ---------- */

/**
 * Never executed. Every `@ts-expect-error` below MUST be consumed; if any one
 * is not, `tsc` reports TS2578 and `npm run typecheck` fails.
 */
async function negativeCases(l: AiLayer): Promise<unknown[]> {
  const out: unknown[] = [];

  // N1 — a capability that is not in the contract at all.
  // @ts-expect-error 'chatt' is not assignable to '"chat" | "embed"'
  out.push(await l.call('chatt', { prompt: 'hi' }));

  // N2 — a capability that IS in the contract but that no provider implements.
  // @ts-expect-error 'transcribe' is not assignable to '"chat" | "embed"'
  out.push(await l.call('transcribe', { audio: 'x' }));

  // N3 — the wrong input shape for a real capability.
  // @ts-expect-error 'texts' does not exist in type 'ChatIn'
  out.push(await l.call('chat', { texts: ['a'] }));

  // N4 — the right shape with a wrong field type.
  // @ts-expect-error number is not assignable to string
  out.push(await l.call('chat', { prompt: 42 }));

  // N5 — an unregistered provider in the route.
  // @ts-expect-error '"cohere"' is not assignable to '"openai" | "anthropic"'
  out.push(await l.call('chat', { prompt: 'hi' }, ['cohere']));

  const reply = await l.call('chat', { prompt: 'hi' });
  // N6 — a field the output type does not have.
  // @ts-expect-error Property 'vectors' does not exist on type 'ChatOut'
  out.push(reply.vectors);

  const embedded = await l.call('embed', { texts: ['a'] });
  // N7 — the two capabilities really do carry different output types.
  // @ts-expect-error Property 'text' does not exist on type 'EmbedOut'
  out.push(embedded.text);

  return out;
}

/**
 * N8 — a handler whose return type disagrees with the contract.
 * `defineProvider` must reject it rather than widening.
 */
const badProvider = defineProvider(ai, {
  id: 'broken',
  capabilities: {
    // @ts-expect-error 'tokens' is missing in the returned object
    chat: async (input) => ({ text: input.prompt }),
  },
});

/* ---------- consume every assertion ---------- */

/**
 * Each slot is `true` ONLY IF its `Equal<>` held. If the phantom carrier stops
 * working, the corresponding alias becomes `false`, `true` is no longer
 * assignable to it, and this line fails to compile.
 *
 * (Kept local, and the file exports nothing: `isolatedDeclarations` demands an
 * explicit annotation on any EXPORTED const with an inferred type — which is
 * precisely the inference this file exists to test. See TS9010.)
 */
const typeAssertions: [
  P1_KeysStayLiteral,
  P2_InputCarried,
  P3_OutputCarried,
  P4_SecondPairIndependent,
  P5_InterfaceKeysStayLiteral,
  P6_InterfaceInputCarried,
  P7_ErasedFormCollapses,
  P8_ProviderIdStaysLiteral,
  P9_ImplementedSetIsExact,
  P10_PartialProviderIsFine,
  P11_LayerIdsAreLiteral,
] = [true, true, true, true, true, true, true, true, true, true, true];

/* ---------- runtime side: the types are not the only thing that works ---------- */

describe('phantom-carrier inference on TypeScript 6.0.3', () => {
  it('routes a call to the first provider implementing the capability', async () => {
    const reply = await layer.call('chat', { prompt: 'hello' });
    expect(reply.text).toBe('openai:hello');
    expect(reply.tokens).toBe(1);
  });

  it('honours a route restricted to a later provider', async () => {
    const reply = await layer.call('chat', { prompt: 'hello' }, ['anthropic']);
    expect(reply.text).toBe('anthropic:hello');
  });

  it('infers a different output type per capability', async () => {
    const embedded = await layer.call('embed', { texts: ['a', 'b'] });
    expect(embedded.vectors).toHaveLength(2);
  });

  it('keeps the phantom carrier type-only — it must never exist at runtime', () => {
    const c = capability<ChatIn, ChatOut>({ idempotent: true });
    expect(Object.getOwnPropertySymbols(c)).toHaveLength(0);
    expect(Object.keys(c)).toEqual(['idempotent']);
  });

  it('keeps provider ids as values, not just as types', () => {
    expect(layer.providerIds).toEqual(['openai', 'anthropic']);
  });

  it('holds all 11 positive type assertions', () => {
    expect(typeAssertions).toHaveLength(11);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the 8 negative cases compiled but unexecuted', () => {
    // Their value is entirely at compile time: every `@ts-expect-error` inside
    // must be consumed, or `tsc` reports TS2578 and typecheck fails.
    expect(typeof negativeCases).toBe('function');
    expect(badProvider.id).toBe('broken');
  });
});
