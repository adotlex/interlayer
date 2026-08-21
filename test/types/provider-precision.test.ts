/**
 * WAVE 3 / T5 — THE `satisfies` TRAP (R6 risk R1).
 *
 * ```ts
 * const providers: Provider<Ai>[] = [openai];          // ← WRONG, fails OPEN
 * const providers = [openai] satisfies readonly Provider<Ai>[];   // ← RIGHT
 * ```
 *
 * The annotation does two things at once, and the second one is the dangerous
 * half:
 *
 *  1. `Id` collapses from `'openai'` to `string`, so `only()` stops checking
 *     provider ids. Annoying, visible, survivable.
 *
 *  2. `H` is filled with the CONSTRAINT `Partial<Handlers<C>>`, whose `keyof`
 *     is EVERY capability in the contract. The layer then claims to implement
 *     capabilities that no provider has a handler for. **It fails OPEN** —
 *     `call('transcribe', …)` compiles clean and blows up at runtime.
 *
 * A library cannot force its callers to write `satisfies`, which is why
 * `src/registry/provider.ts` derives the capability map from the RUNTIME VALUE
 * and the registry rejects what does not add up. Both halves are tested here:
 * the type-level precision loss, and the runtime backstop that catches it.
 */

import { describe, expect, it } from 'vitest';
import {
  capability,
  createLayer,
  defineContract,
  defineProvider,
  type Handlers,
  hasCode,
  type ImplementedBy,
  implementedCapabilities,
  type Layer,
  type Provider,
  type ProviderId,
} from '../../src/index.ts';
import { createFakeRuntime } from '../support/index.ts';

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Expect<T extends true> = T;

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
  readonly vectors: readonly number[];
}
interface TranscribeIn {
  readonly audio: string;
}
interface TranscribeOut {
  readonly transcript: string;
}

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>(),
  embed: capability<EmbedIn, EmbedOut>(),
  transcribe: capability<TranscribeIn, TranscribeOut>(),
});
type Ai = typeof ai;

/** Implements two of the three capabilities. `transcribe` has NO handler. */
const openai = defineProvider(ai, {
  id: 'openai',
  capabilities: {
    chat: async (input) => ({ text: `openai:${input.prompt}`, tokens: 1 }),
    embed: async (input) => ({ vectors: input.texts.map((_t, i) => i) }),
  },
});

/* ================================================================== *
 * 1. The trap, stated as two type assertions
 * ================================================================== */

/** `Provider<Ai>` defaults `Id` to `string`: the literal id is gone. */
type S01_AnnotationCollapsesId = Expect<Equal<ProviderId<Provider<Ai>>, string>>;

/**
 * …and defaults `H` to `Partial<Handlers<Ai>>`, whose `keyof` is the WHOLE
 * contract. This is the over-report: the type says `transcribe` is implemented.
 */
type S02_AnnotationOverReportsCapabilities = Expect<
  Equal<ImplementedBy<Provider<Ai>>, 'chat' | 'embed' | 'transcribe'>
>;

/** The inferred provider tells the truth about both. */
type S03_InferredKeepsId = Expect<Equal<ProviderId<typeof openai>, 'openai'>>;
type S04_InferredKeepsCapabilities = Expect<Equal<ImplementedBy<typeof openai>, 'chat' | 'embed'>>;

/* ================================================================== *
 * 2. The trap, carried through `createLayer`
 * ================================================================== */

/** THE WRONG FORM. Written deliberately; this is the defect under test. */
const annotated: Provider<Ai>[] = [openai];
const annotatedLayer = createLayer({
  contract: ai,
  providers: annotated,
  runtime: createFakeRuntime(),
});

/**
 * The layer type now claims all three capabilities and any provider id at all.
 * NO `@ts-expect-error` here — that is the point: the loss is silent.
 */
type S05_AnnotatedLayerFailsOpen = Expect<
  Equal<typeof annotatedLayer, Layer<Ai, string, 'chat' | 'embed' | 'transcribe'>>
>;

/**
 * Both of these COMPILE, and both are wrong. Never invoked — the first would
 * reject at runtime, which is exactly the backstop asserted below.
 */
function annotatedLayerAcceptsNonsense(): unknown[] {
  return [
    annotatedLayer.call('transcribe', { audio: 'nobody implements this' }),
    annotatedLayer.only('a-provider-id-that-does-not-exist'),
  ];
}

/* ================================================================== *
 * 3. `satisfies` — the same shape check, full precision
 * ================================================================== */

/** THE RIGHT FORM. */
const checked = [openai] satisfies readonly Provider<Ai>[];
const satisfiesLayer = createLayer({
  contract: ai,
  providers: checked,
  runtime: createFakeRuntime(),
});

type S06_SatisfiesKeepsElementPrecision = Expect<
  Equal<(typeof checked)[number], typeof openai>
>;
type S07_SatisfiesLayerIsExact = Expect<
  Equal<typeof satisfiesLayer, Layer<Ai, 'openai', 'chat' | 'embed'>>
>;

/** `as const` + `satisfies` is equally precise — it just also freezes order. */
const frozen = [openai] as const satisfies readonly Provider<Ai>[];
const frozenLayer = createLayer({
  contract: ai,
  providers: frozen,
  runtime: createFakeRuntime(),
});
type S08_AsConstSatisfiesIsExact = Expect<
  Equal<typeof frozenLayer, Layer<Ai, 'openai', 'chat' | 'embed'>>
>;

/** The same two nonsense calls, now REJECTED. */
function satisfiesLayerRejectsNonsense(): unknown[] {
  const out: unknown[] = [];

  // S-N01 — no provider implements `transcribe`; the annotation form allowed it.
  // @ts-expect-error TS2345: '"transcribe"' is not assignable to '"chat" | "embed"'
  out.push(satisfiesLayer.call('transcribe', { audio: 'x' }));

  // S-N02 — an unregistered provider id; the annotation form allowed it.
  // @ts-expect-error TS2345: '"nope"' is not assignable to parameter of type '"openai"'
  out.push(satisfiesLayer.only('nope'));

  // S-N03 — and the ordinary call-site checks still apply.
  // @ts-expect-error TS2353: 'texts' does not exist in type 'ChatIn'
  out.push(satisfiesLayer.call('chat', { texts: ['a'] }));

  return out;
}

/* ================================================================== *
 * 4. `satisfies` still rejects a genuinely broken provider
 * ================================================================== */

function satisfiesStillChecksShape(): unknown[] {
  const out: unknown[] = [];
  const wrongReturn = { id: 'x', capabilities: { chat: async (): Promise<object> => ({}) } };
  // S-N04 — the handler's return type does not satisfy `ChatOut`.
  // @ts-expect-error TS2322: '{}' is missing 'text' and 'tokens' from 'ChatOut'
  out.push([wrongReturn] satisfies readonly Provider<Ai>[]);

  const strayKey = { id: 'y', capabilities: { summarise: async (): Promise<string> => 's' } };
  // S-N05 — `summarise` is not a capability of the contract.
  // @ts-expect-error TS2559: no properties in common with 'Partial<Handlers<Ai>>'
  out.push([strayKey] satisfies readonly Provider<Ai>[]);

  return out;
}

/* ================================================================== *
 * 5. FINDING (T5): `satisfies Provider<C>` on a FRESH OBJECT LITERAL
 *    preserves the capability set but WIDENS THE ID.
 * ================================================================== */

/**
 * ── RECORDED DEFECT, not a passing feature ───────────────────────────────
 *
 * The guidance in `src/registry/provider.ts` — "use `satisfies`" — is stated
 * for an ARRAY OF ALREADY-DEFINED PROVIDERS (`[openai] satisfies …`), where it
 * is exactly right: §3 above proves the element keeps `'openai'`.
 *
 * It does NOT hold for a provider written INLINE under `satisfies`. `Provider`
 * declares `Id extends string = string`, so the contextual type of the `id`
 * property is plain `string`, and a fresh string literal contextually typed by
 * `string` WIDENS. The implemented capability set survives; the id does not.
 *
 * Consequence: a layer built from such a provider has `Ids = string`, so
 * `only()` accepts any string at all — the same fail-open as §2, arriving by a
 * different door and from code that looks like it took the advice.
 *
 * Two forms keep the literal, and both are asserted below: `defineProvider`
 * (whose `const Id extends string` exists for precisely this) and
 * `as const satisfies`.
 */
const bareSatisfies = {
  id: 'handrolled',
  capabilities: { chat: async (): Promise<ChatOut> => ({ text: 'h', tokens: 0 }) },
} satisfies Provider<Ai>;

/** The capability set is exact — this half of `satisfies` works. */
type S09_BareSatisfiesKeepsCapabilities = Expect<Equal<ImplementedBy<typeof bareSatisfies>, 'chat'>>;
/** The id is NOT. `'handrolled'` widened to `string`. THIS IS THE DEFECT. */
type S10_BareSatisfiesWidensId = Expect<Equal<ProviderId<typeof bareSatisfies>, string>>;

/** `as const satisfies` keeps it. */
const constSatisfies = {
  id: 'handrolled',
  capabilities: { chat: async (): Promise<ChatOut> => ({ text: 'h', tokens: 0 }) },
} as const satisfies Provider<Ai>;
type S11_AsConstSatisfiesKeepsId = Expect<Equal<ProviderId<typeof constSatisfies>, 'handrolled'>>;

/** And so does `defineProvider`, which is why it declares `const Id`. */
const viaDefineProvider = defineProvider(ai, {
  id: 'handrolled',
  capabilities: { chat: async (): Promise<ChatOut> => ({ text: 'h', tokens: 0 }) },
});
type S12_DefineProviderKeepsId = Expect<Equal<ProviderId<typeof viaDefineProvider>, 'handrolled'>>;

/**
 * The consequence, carried into a layer. NO `@ts-expect-error`: this compiles,
 * and that is the recorded defect. Never invoked.
 */
function bareSatisfiesLayerFailsOpenOnIds(): unknown {
  const l = createLayer({
    contract: ai,
    providers: [bareSatisfies],
    runtime: createFakeRuntime(),
  });
  return l.only('an-id-that-was-never-registered');
}

/** With `as const satisfies`, the same mistake is caught. */
function constSatisfiesLayerChecksIds(): unknown {
  const l = createLayer({ contract: ai, providers: [constSatisfies], runtime: createFakeRuntime() });
  // S-N06 — the id union is real again.
  // @ts-expect-error TS2345: '"nope"' is not assignable to parameter of type '"handrolled"'
  return l.only('nope');
}

/** `Handlers<C>` is the total map; providers are constrained to a Partial of it. */
type S13_HandlersIsTotal = Expect<
  Equal<Extract<keyof Handlers<Ai>, string>, 'chat' | 'embed' | 'transcribe'>
>;

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  S01_AnnotationCollapsesId,
  S02_AnnotationOverReportsCapabilities,
  S03_InferredKeepsId,
  S04_InferredKeepsCapabilities,
  S05_AnnotatedLayerFailsOpen,
  S06_SatisfiesKeepsElementPrecision,
  S07_SatisfiesLayerIsExact,
  S08_AsConstSatisfiesIsExact,
  S09_BareSatisfiesKeepsCapabilities,
  S10_BareSatisfiesWidensId,
  S11_AsConstSatisfiesKeepsId,
  S12_DefineProviderKeepsId,
  S13_HandlersIsTotal,
] = [true, true, true, true, true, true, true, true, true, true, true, true, true];

describe('provider precision: `satisfies` keeps it, an annotation loses it', () => {
  it('holds all 13 precision assertions', () => {
    expect(typeAssertions).toHaveLength(13);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the compile-time-only probes unexecuted', () => {
    expect(typeof annotatedLayerAcceptsNonsense).toBe('function');
    expect(typeof satisfiesLayerRejectsNonsense).toBe('function');
    expect(typeof satisfiesStillChecksShape).toBe('function');
    expect(typeof bareSatisfiesLayerFailsOpenOnIds).toBe('function');
    expect(typeof constSatisfiesLayerChecksIds).toBe('function');
  });

  it('reads the TRUE capability set off the runtime value, not the type', () => {
    // The annotated array's static type claims three capabilities; the value
    // has two, and the value is what the registry believes.
    expect(implementedCapabilities(openai)).toEqual(['chat', 'embed']);
    expect(annotatedLayer.supports('transcribe')).toBe(false);
    expect(satisfiesLayer.supports('transcribe')).toBe(false);
  });

  it('THE RUNTIME BACKSTOP: the fail-open call is rejected at call time', async () => {
    // The type said this was fine. `src/registry` is why it is not.
    const boom = await annotatedLayer
      .call('transcribe', { audio: 'x' })
      .then(() => undefined)
      .catch((error: unknown) => error);
    expect(hasCode(boom, 'UNSUPPORTED_CAPABILITY')).toBe(true);
  });

  it('routes a real call through the `satisfies` layer', async () => {
    const reply = await satisfiesLayer.call('chat', { prompt: 'hi' });
    expect(reply.text).toBe('openai:hi');
  });

  it('carries the same id at RUNTIME whether or not the type kept the literal', () => {
    // The id widening in §5 is purely static: every form produces 'handrolled'.
    // That is what makes it dangerous — nothing observable at runtime changes.
    expect(bareSatisfies.id).toBe('handrolled');
    expect(constSatisfies.id).toBe('handrolled');
    expect(viaDefineProvider.id).toBe('handrolled');
  });

  it('closes every layer it opened', async () => {
    await annotatedLayer.close();
    await satisfiesLayer.close();
    await frozenLayer.close();
    expect(annotatedLayer.providers).toHaveLength(0);
  });
});
