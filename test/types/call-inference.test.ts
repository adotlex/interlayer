/**
 * WAVE 3 / T5 — call-site inference against the REAL published surface.
 *
 * `test/support/type-inference.test.ts` re-verified R6's phantom-carrier
 * technique against a HAND-BUILT `TestLayer` and a hand-rolled `capability` /
 * `defineContract`. This file does the same job against the actual shipped
 * functions re-exported from `src/index.ts` — `capability`, `defineContract`,
 * `defineProvider`, `createLayer` — because a technique that works on a
 * transcription of the design and fails on the implementation is worth nothing.
 *
 * ── HOW TO READ A FAILURE ────────────────────────────────────────────────
 *
 * Positive cases are `Equal<>` aliases consumed by the `typeAssertions` tuple
 * at the bottom: if inference degrades, the alias becomes `false`, `true` stops
 * being assignable, and the file fails to compile.
 *
 * Negative cases are `@ts-expect-error`. **A directive that reports
 * `TS2578: Unused '@ts-expect-error' directive` is a FAILING test**: it means
 * the bogus call compiled clean and the assertion proved nothing. `tsc` reports
 * TS2578 as an error, so `npm run typecheck` goes red — but typecheck alone is
 * NOT sufficient evidence, because a directive can be consumed by an
 * *unintended* error on the same line. Every directive here was additionally
 * verified by stripping the suppressions into a throwaway tree and confirming a
 * real error lands on the very next line.
 *
 * ── ONE-LINE RULE ────────────────────────────────────────────────────────
 *
 * `@ts-expect-error` suppresses errors on the NEXT LINE ONLY. Every erroring
 * expression below is kept on a single line for that reason; do not let the
 * formatter wrap one.
 */

import { afterAll, describe, expect, it } from 'vitest';
import {
  type CapabilityName,
  capability,
  createLayer,
  defineContract,
  defineProvider,
  type ImplementedBy,
  type ImplementedCapabilities,
  type InputOf,
  type Layer,
  type OutputOf,
  type ProviderId,
  type ProviderIds,
} from '../../src/index.ts';
import { createFakeRuntime } from '../support/index.ts';

/* ---------- type-level assertion helpers ---------- */

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

/* ---------- the application's types ---------- */

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

/* ---------- the contract, minted by the SHIPPED helpers ---------- */

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>({ idempotent: false }),
  embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
  transcribe: capability<TranscribeIn, TranscribeOut>({ idempotent: true }),
});
type Ai = typeof ai;

type A01_ContractKeysStayLiteral = Expect<
  Equal<CapabilityName<Ai>, 'chat' | 'embed' | 'transcribe'>
>;
type A02_InputCarried = Expect<Equal<InputOf<Ai, 'chat'>, ChatIn>>;
type A03_OutputCarried = Expect<Equal<OutputOf<Ai, 'chat'>, ChatOut>>;
type A04_SecondPairIsIndependent = Expect<Equal<InputOf<Ai, 'embed'>, EmbedIn>>;

/* ---------- providers: zero handler annotations ---------- */

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

type A05_ProviderIdStaysLiteral = Expect<Equal<ProviderId<typeof openai>, 'openai'>>;
type A06_ImplementedSetIsExact = Expect<Equal<ImplementedBy<typeof openai>, 'chat' | 'embed'>>;
type A07_PartialProviderIsExact = Expect<Equal<ImplementedBy<typeof anthropic>, 'chat'>>;

/* ---------- the layer, built by the SHIPPED createLayer ---------- */

const layer = createLayer({
  contract: ai,
  providers: [openai, anthropic],
  runtime: createFakeRuntime(),
});
type AiLayer = typeof layer;

/**
 * `transcribe` is DECLARED by the contract but implemented by nobody, so the
 * layer's callable set must be `'chat' | 'embed'`. That is the difference
 * between the contract's surface and the *reachable* surface, and it is what
 * makes N02 below meaningful.
 */
type A08_LayerTypeIsExact = Expect<
  Equal<AiLayer, Layer<Ai, 'openai' | 'anthropic', 'chat' | 'embed'>>
>;
type A09_IdsHelperAgrees = Expect<
  Equal<ProviderIds<Ai, [typeof openai, typeof anthropic]>, 'openai' | 'anthropic'>
>;
type A10_ImplHelperExcludesUnimplemented = Expect<
  Equal<ImplementedCapabilities<Ai, [typeof openai, typeof anthropic]>, 'chat' | 'embed'>
>;

/**
 * R6 P13 — providers hoisted to a plain `const` array, WITHOUT `as const`,
 * still yield the full id union. The `const` type-parameter modifier on
 * `createLayer` is what does it; if someone drops it, this collapses.
 */
const hoisted = [openai, anthropic];
const hoistedLayer = createLayer({
  contract: ai,
  providers: hoisted,
  runtime: createFakeRuntime(),
});
type A11_HoistedArrayKeepsUnion = Expect<
  Equal<typeof hoistedLayer, Layer<Ai, 'openai' | 'anthropic', 'chat' | 'embed'>>
>;

/* ---------- POSITIVE: call-site inference, observed through a probe ---------- */

/**
 * Never invoked. Its INFERRED return type is the assertion: every binding below
 * is annotation-free, so `Probe['chat']` is literally whatever `layer.call`
 * decided to give back.
 */
async function callProbe() {
  const chat = await layer.call('chat', { prompt: 'hi' });
  const chatWithOptional = await layer.call('chat', { prompt: 'hi', temperature: 0.2 });
  const embed = await layer.call('embed', { texts: ['a'] });
  const pinned = await layer.only('anthropic').call('chat', { prompt: 'hi' });
  const withOptions = await layer.call('chat', { prompt: 'hi' }, { timeoutMs: 50 });
  return { chat, chatWithOptional, embed, pinned, withOptions };
}
type Probe = Awaited<ReturnType<typeof callProbe>>;

type A12_CallReturnsDeclaredOutput = Expect<Equal<Probe['chat'], ChatOut>>;
type A13_OptionalInputFieldAccepted = Expect<Equal<Probe['chatWithOptional'], ChatOut>>;
type A14_SecondCapabilityDiffers = Expect<Equal<Probe['embed'], EmbedOut>>;
type A15_OnlyKeepsCallInference = Expect<Equal<Probe['pinned'], ChatOut>>;
type A16_OptionsDoNotDisturbInference = Expect<Equal<Probe['withOptions'], ChatOut>>;

/**
 * The input parameter is genuinely `InputOf<C, K>` and not `unknown`. If it
 * ever widened, `build`'s parameter type would stop being checked here.
 */
function inputIsContextuallyTyped(): Promise<ChatOut> {
  const build = (p: ChatIn): ChatIn => ({ prompt: p.prompt.toUpperCase() });
  return layer.call('chat', build({ prompt: 'hi' }));
}

/* ---------- NEGATIVE: compiling IS the assertion ---------- */

/**
 * Never invoked; its whole value is at compile time. Each directive names the
 * error it expects, and each erroring expression is on ONE line.
 */
async function negatives(l: AiLayer): Promise<unknown[]> {
  const out: unknown[] = [];

  // N01 — a capability the contract does not declare at all.
  // @ts-expect-error TS2345: '"chatt"' is not assignable to '"chat" | "embed"'
  out.push(await l.call('chatt', { prompt: 'hi' }));

  // N02 — declared by the contract, implemented by NO registered provider.
  // @ts-expect-error TS2345: '"transcribe"' is not assignable to '"chat" | "embed"'
  out.push(await l.call('transcribe', { audio: 'x' }));

  // N03 — wrong input shape: a field belonging to a different capability.
  // @ts-expect-error TS2353/TS2345: 'texts' does not exist in type 'ChatIn'
  out.push(await l.call('chat', { texts: ['a'] }));

  // N04 — right field name, wrong field type.
  // @ts-expect-error TS2345: number is not assignable to string
  out.push(await l.call('chat', { prompt: 42 }));

  // N05 — a required input field omitted.
  // @ts-expect-error TS2345: 'prompt' is missing in type '{}'
  out.push(await l.call('chat', {}));

  // N06 — the input for the OTHER capability, in full.
  // @ts-expect-error TS2345: 'EmbedIn' is not assignable to 'ChatIn'
  out.push(await l.call('chat', embedInput));

  const reply = await l.call('chat', { prompt: 'hi' });
  // N07 — a field the declared output does not have.
  // @ts-expect-error TS2339: Property 'vectors' does not exist on type 'ChatOut'
  out.push(reply.vectors);

  const embedded = await l.call('embed', { texts: ['a'] });
  // N08 — the two capabilities really do carry different outputs.
  // @ts-expect-error TS2339: Property 'text' does not exist on type 'EmbedOut'
  out.push(embedded.text);

  // N09 — a provider id nobody registered, in `only()`.
  // @ts-expect-error TS2345: '"cohere"' is not assignable to '"openai" | "anthropic"'
  out.push(l.only('cohere'));

  // N10 — a bogus id alongside a real one is still rejected.
  // @ts-expect-error TS2345: '"gemini"' is not assignable to '"openai" | "anthropic"'
  out.push(l.only('openai', 'gemini'));

  // N11 — an unknown key on the per-call overrides.
  // @ts-expect-error TS2353: 'deadlineMs' does not exist in type 'CallOptions'
  out.push(await l.call('chat', { prompt: 'hi' }, { deadlineMs: 5 }));

  // N12 — a known option key with the wrong value type.
  // @ts-expect-error TS2322: string is not assignable to 'number | undefined'
  out.push(await l.call('chat', { prompt: 'hi' }, { timeoutMs: 'soon' }));

  return out;
}

/** A non-fresh value, so N06 tests assignability rather than excess properties. */
const embedInput: EmbedIn = { texts: ['a', 'b'] };

/**
 * N13 — a handler whose return type disagrees with the contract. `tokens` is
 * required by `ChatOut`; `defineProvider` must reject the object rather than
 * widening `H` to swallow it.
 */
const shortHandler = defineProvider(ai, {
  id: 'short',
  capabilities: {
    // @ts-expect-error TS2739/TS2322: 'tokens' is missing in the returned object
    chat: async (input) => ({ text: input.prompt }),
  },
});

/**
 * N14 — a provider implementing a capability the contract never declared.
 * NEVER INVOKED: `defineProvider` also throws a `ConfigError` at runtime, which
 * is the backstop for callers who never reach this signature. The static
 * rejection is what is under test here.
 */
function strayCapabilityRejected(): unknown {
  const stray = { summarise: async (): Promise<string> => 'x' };
  // @ts-expect-error TS2345: 'summarise' is not a capability declared by 'Ai'
  return defineProvider(ai, { id: 'stray', capabilities: stray });
}

/**
 * N15 — `createLayer` with no providers FAILS CLOSED: `Impl` resolves to
 * `never`, so the layer is uncallable rather than callable-with-anything.
 * Never invoked, so the empty registry is never exercised at runtime.
 */
function emptyLayerFailsClosed(): Promise<unknown> {
  const empty = createLayer({ contract: ai, providers: [], runtime: createFakeRuntime() });
  // @ts-expect-error TS2345: '"chat"' is not assignable to parameter of type 'never'
  return empty.call('chat', { prompt: 'hi' });
}

/* ---------- consume every positive assertion ---------- */

const typeAssertions: [
  A01_ContractKeysStayLiteral,
  A02_InputCarried,
  A03_OutputCarried,
  A04_SecondPairIsIndependent,
  A05_ProviderIdStaysLiteral,
  A06_ImplementedSetIsExact,
  A07_PartialProviderIsExact,
  A08_LayerTypeIsExact,
  A09_IdsHelperAgrees,
  A10_ImplHelperExcludesUnimplemented,
  A11_HoistedArrayKeepsUnion,
  A12_CallReturnsDeclaredOutput,
  A13_OptionalInputFieldAccepted,
  A14_SecondCapabilityDiffers,
  A15_OnlyKeepsCallInference,
  A16_OptionsDoNotDisturbInference,
] = [
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
  true,
];

/* ---------- runtime: the types are not the only thing that has to work ---------- */

describe('call-site inference on the shipped createLayer', () => {
  afterAll(async () => {
    await layer.close();
    await hoistedLayer.close();
  });

  it('holds every positive type assertion', () => {
    expect(typeAssertions).toHaveLength(16);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the negative cases compiled but unexecuted', () => {
    // Their value is entirely at compile time: every `@ts-expect-error` above
    // must be consumed, or `tsc` reports TS2578 and typecheck fails.
    expect(typeof negatives).toBe('function');
    expect(typeof strayCapabilityRejected).toBe('function');
    expect(typeof emptyLayerFailsClosed).toBe('function');
    expect(typeof callProbe).toBe('function');
    expect(embedInput.texts).toHaveLength(2);
  });

  it('runs the inferred call for real and returns the declared output', async () => {
    const reply = await layer.call('chat', { prompt: 'hello' });
    expect(reply.text).toBe('openai:hello');
    expect(reply.tokens).toBe(1);
  });

  it('keeps `input` contextually typed all the way to the handler', async () => {
    const reply = await inputIsContextuallyTyped();
    expect(reply.text).toBe('openai:HI');
  });

  it('mints capability descriptors with no runtime phantom', () => {
    expect(Object.getOwnPropertySymbols(ai.chat)).toHaveLength(0);
    expect(Object.keys(ai.chat)).toEqual(['idempotent']);
  });

  it('keeps the over-declared capability out of the reachable set at runtime too', () => {
    expect(layer.supports('transcribe')).toBe(false);
    expect(layer.supports('chat')).toBe(true);
    expect(shortHandler.id).toBe('short');
  });
});
