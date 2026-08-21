/**
 * WAVE 3 / T5 — THE `interface` TRAP.
 *
 * R3 and R6 found this independently, and `src/core/types.ts` documents it in
 * the comment above `Contract<C>`. It is the highest-value type test in the
 * suite because it fails in BOTH directions and both are silent:
 *
 *  1. `C extends Record<string, Capability>` (the "obvious" constraint)
 *     REJECTS a contract annotated with an `interface`. An interface has no
 *     implicit index signature, so a perfectly good contract stops compiling —
 *     noisy, but at least loud.
 *
 *  2. The fix people reach for — `interface MyContract extends AnyContract` —
 *     INHERITS the string index signature, `CapabilityName<C>` widens from
 *     `'chat' | 'embed'` to `string`, and every call site silently loses type
 *     checking. That one FAILS OPEN: `call('typo', {})` compiles.
 *
 * The F-bounded `Contract<C> = { readonly [K in keyof C]: Capability }` imposes
 * the same shape, keeps the literal key union, and accepts `type` and
 * `interface` declarations alike. This file proves all four claims.
 */

import { describe, expect, it } from 'vitest';
import type { AnyContract, Handlers } from '../../src/core/types.ts';
import {
  type Capability,
  type CapabilityName,
  type Contract,
  capability,
  createLayer,
  defineContract,
  defineProvider,
  type InputOf,
  type Layer,
  type OutputOf,
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
}
interface EmbedIn {
  readonly texts: readonly string[];
}
interface EmbedOut {
  readonly vectors: readonly number[];
}

/* ================================================================== *
 * 1. The same contract, declared three ways
 * ================================================================== */

/** (a) A `type` alias. Type aliases DO get an implicit index signature. */
type TypeContract = {
  readonly chat: Capability<ChatIn, ChatOut>;
  readonly embed: Capability<EmbedIn, EmbedOut>;
};

/** (b) An `interface`. Interfaces do NOT get an implicit index signature. */
interface InterfaceContract {
  readonly chat: Capability<ChatIn, ChatOut>;
  readonly embed: Capability<EmbedIn, EmbedOut>;
}

/** (c) An `interface` built by EXTENSION — the shape real codebases grow into. */
interface BaseContract {
  readonly chat: Capability<ChatIn, ChatOut>;
}
interface ExtendedContract extends BaseContract {
  readonly embed: Capability<EmbedIn, EmbedOut>;
}

/** (d) An `interface` with MUTABLE members — `Contract<C>` maps `readonly` on. */
interface MutableContract {
  chat: Capability<ChatIn, ChatOut>;
  embed: Capability<EmbedIn, EmbedOut>;
}

const members = {
  chat: capability<ChatIn, ChatOut>({ idempotent: false }),
  embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
};

const asType: TypeContract = members;
const asInterface: InterfaceContract = members;
const asExtended: ExtendedContract = members;
const asMutable: MutableContract = { ...members };

/* ================================================================== *
 * 2. `defineContract` accepts every one of them
 * ================================================================== */

const typeDeclared = defineContract(asType);
const interfaceDeclared = defineContract(asInterface);
const extendedDeclared = defineContract(asExtended);
const mutableDeclared = defineContract(asMutable);

type C01_TypeRoundTrips = Expect<Equal<typeof typeDeclared, TypeContract>>;
type C02_InterfaceRoundTrips = Expect<Equal<typeof interfaceDeclared, InterfaceContract>>;
type C03_ExtendedRoundTrips = Expect<Equal<typeof extendedDeclared, ExtendedContract>>;
type C04_MutableRoundTrips = Expect<Equal<typeof mutableDeclared, MutableContract>>;

/* ================================================================== *
 * 3. Inference survives in ALL of them
 * ================================================================== */

type C05_TypeKeysLiteral = Expect<Equal<CapabilityName<TypeContract>, 'chat' | 'embed'>>;
type C06_InterfaceKeysLiteral = Expect<Equal<CapabilityName<InterfaceContract>, 'chat' | 'embed'>>;
type C07_ExtendedKeysLiteral = Expect<Equal<CapabilityName<ExtendedContract>, 'chat' | 'embed'>>;
type C08_MutableKeysLiteral = Expect<Equal<CapabilityName<MutableContract>, 'chat' | 'embed'>>;

type C09_InterfaceInputCarried = Expect<Equal<InputOf<InterfaceContract, 'chat'>, ChatIn>>;
type C10_InterfaceOutputCarried = Expect<Equal<OutputOf<InterfaceContract, 'embed'>, EmbedOut>>;
type C11_ExtendedInheritedCarried = Expect<Equal<InputOf<ExtendedContract, 'chat'>, ChatIn>>;
type C12_MutableOutputCarried = Expect<Equal<OutputOf<MutableContract, 'chat'>, ChatOut>>;

/* ================================================================== *
 * 4. THE ASYMMETRY: the naive index-signature constraint
 * ================================================================== */

/**
 * The constraint `src/core/types.ts` warns against, written out so the
 * difference is executable rather than folklore. `AnyContract` IS
 * `Readonly<Record<string, Capability>>`, the exact erased form.
 */
declare function defineContractNaive<C extends AnyContract>(contract: C): C;

/** The F-bounded constraint, for side-by-side comparison. This is the fix. */
declare function defineContractFBounded<C extends Contract<C>>(contract: C): C;

/**
 * Both helpers are AMBIENT (`declare function`) — they have no runtime body, so
 * this probe is never invoked. Its inferred return type is the assertion.
 */
function constraintProbe() {
  const naiveWithTypeAlias = defineContractNaive(asType);
  const fBoundedWithTypeAlias = defineContractFBounded(asType);
  const fBoundedWithInterface = defineContractFBounded(asInterface);
  return { naiveWithTypeAlias, fBoundedWithTypeAlias, fBoundedWithInterface };
}
type ConstraintProbe = ReturnType<typeof constraintProbe>;

/** The naive constraint is perfectly happy with a `type` alias… */
type C13_NaiveAcceptsTypeAlias = Expect<Equal<ConstraintProbe['naiveWithTypeAlias'], TypeContract>>;
/** …and the F-bounded form takes the `type` alias AND the `interface`. */
type C14_FBoundedKeepsType = Expect<Equal<ConstraintProbe['fBoundedWithTypeAlias'], TypeContract>>;
type C15_FBoundedKeepsInterface = Expect<
  Equal<ConstraintProbe['fBoundedWithInterface'], InterfaceContract>
>;

/** The naive constraint rejects the identical contract declared as an `interface`. */
function naiveRejectsInterface(): unknown {
  // @ts-expect-error TS2345: an `interface` has no implicit index signature
  return defineContractNaive(asInterface);
}

/* ================================================================== *
 * 5. THE FAIL-OPEN FORM: `interface X extends AnyContract`
 * ================================================================== */

/**
 * The "fix" that makes the naive constraint compile again. It also destroys
 * every guarantee the library sells. Asserted here so that if anyone ever
 * writes it in an example, this file explains what they bought.
 */
interface InheritsIndexSignature extends AnyContract {
  readonly chat: Capability<ChatIn, ChatOut>;
  readonly embed: Capability<EmbedIn, EmbedOut>;
}

/** The literal union is GONE — collapsed to `string`. */
type C16_IndexSignatureCollapsesKeys = Expect<
  Equal<CapabilityName<InheritsIndexSignature>, string>
>;
/** The declared pairs still resolve; it is the KEY SET that stopped checking. */
type C17_DeclaredPairsStillResolve = Expect<Equal<InputOf<InheritsIndexSignature, 'chat'>, ChatIn>>;
/** A key nobody declared resolves to `unknown` instead of erroring. */
type C18_UndeclaredKeyResolvesToUnknown = Expect<
  Equal<InputOf<InheritsIndexSignature, 'never-declared'>, unknown>
>;

/**
 * THE FAIL-OPEN, made concrete. NOTE THE ABSENCE OF `@ts-expect-error`: these
 * lines COMPILE, and that is the defect being recorded. If a future change to
 * `Contract<C>` ever made them stop compiling, this function would fail to
 * type-check and this comment is how you will know why.
 */
function indexSignatureFailsOpen(l: Layer<InheritsIndexSignature>): unknown[] {
  return [
    l.call('chat', { prompt: 'ok' }),
    l.call('chatt', { prompt: 'typo — and it compiles' }),
    l.call('utterly-made-up', { anything: 'at all' }),
    l.only('a-provider-that-was-never-registered'),
  ];
}

/* ================================================================== *
 * 6. End to end on the INTERFACE-declared contract
 * ================================================================== */

const openai = defineProvider(interfaceDeclared, {
  id: 'openai',
  capabilities: {
    chat: async (input) => ({ text: `openai:${input.prompt}` }),
    embed: async (input) => ({ vectors: input.texts.map((_t, i) => i) }),
  },
});

const anthropic = defineProvider(interfaceDeclared, {
  id: 'anthropic',
  capabilities: { chat: async (input) => ({ text: `anthropic:${input.prompt}` }) },
});

const interfaceLayer = createLayer({
  contract: interfaceDeclared,
  providers: [openai, anthropic],
  runtime: createFakeRuntime(),
});

type C19_InterfaceLayerIsFullyTyped = Expect<
  Equal<typeof interfaceLayer, Layer<InterfaceContract, 'openai' | 'anthropic', 'chat' | 'embed'>>
>;

/** Handler parameters are contextually typed off an INTERFACE contract too. */
type C20_InterfaceHandlersTyped = Expect<
  Equal<Parameters<Handlers<InterfaceContract>['chat']>[0], ChatIn>
>;

async function interfaceProbe() {
  const chat = await interfaceLayer.call('chat', { prompt: 'hi' });
  const embed = await interfaceLayer.call('embed', { texts: ['a'] });
  return { chat, embed };
}
type InterfaceProbe = Awaited<ReturnType<typeof interfaceProbe>>;
type C21_InterfaceCallInfersOutput = Expect<Equal<InterfaceProbe['chat'], ChatOut>>;
type C22_InterfaceSecondCapability = Expect<Equal<InterfaceProbe['embed'], EmbedOut>>;

/** And the negatives fire exactly as they do for the `type`-declared form. */
async function interfaceNegatives(): Promise<unknown[]> {
  const l = interfaceLayer;
  const out: unknown[] = [];

  // I01 — unknown capability, on an interface-declared contract.
  // @ts-expect-error TS2345: '"chatt"' is not assignable to '"chat" | "embed"'
  out.push(await l.call('chatt', { prompt: 'hi' }));

  // I02 — wrong input shape, on an interface-declared contract.
  // @ts-expect-error TS2353: 'texts' does not exist in type 'ChatIn'
  out.push(await l.call('chat', { texts: ['a'] }));

  // I03 — wrong output assumption, on an interface-declared contract.
  const reply = await l.call('chat', { prompt: 'hi' });
  // @ts-expect-error TS2339: Property 'vectors' does not exist on type 'ChatOut'
  out.push(reply.vectors);

  // I04 — unregistered provider id, on an interface-declared contract.
  // @ts-expect-error TS2345: '"cohere"' is not assignable to '"openai" | "anthropic"'
  out.push(l.only('cohere'));

  return out;
}

/* ================================================================== *
 * 7. `defineContract` rejects a member that is not a capability
 * ================================================================== */

function notACapability(): unknown {
  // @ts-expect-error TS2345: number is not a 'Capability'
  return defineContract({ chat: 42 });
}

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  C01_TypeRoundTrips,
  C02_InterfaceRoundTrips,
  C03_ExtendedRoundTrips,
  C04_MutableRoundTrips,
  C05_TypeKeysLiteral,
  C06_InterfaceKeysLiteral,
  C07_ExtendedKeysLiteral,
  C08_MutableKeysLiteral,
  C09_InterfaceInputCarried,
  C10_InterfaceOutputCarried,
  C11_ExtendedInheritedCarried,
  C12_MutableOutputCarried,
  C13_NaiveAcceptsTypeAlias,
  C14_FBoundedKeepsType,
  C15_FBoundedKeepsInterface,
  C16_IndexSignatureCollapsesKeys,
  C17_DeclaredPairsStillResolve,
  C18_UndeclaredKeyResolvesToUnknown,
  C19_InterfaceLayerIsFullyTyped,
  C20_InterfaceHandlersTyped,
  C21_InterfaceCallInfersOutput,
  C22_InterfaceSecondCapability,
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
  true,
  true,
  true,
  true,
  true,
  true,
];

describe('contract declaration shape: `type` and `interface` must both infer', () => {
  it('holds all 22 shape assertions', () => {
    expect(typeAssertions).toHaveLength(22);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the compile-time-only probes unexecuted', () => {
    expect(typeof naiveRejectsInterface).toBe('function');
    expect(typeof indexSignatureFailsOpen).toBe('function');
    expect(typeof interfaceNegatives).toBe('function');
    expect(typeof notACapability).toBe('function');
    expect(typeof interfaceProbe).toBe('function');
    expect(typeof constraintProbe).toBe('function');
  });

  it('returns the same object from every declaration form', () => {
    expect(typeDeclared).toBe(asType);
    expect(interfaceDeclared).toBe(asInterface);
    expect(extendedDeclared).toBe(asExtended);
    expect(mutableDeclared).toBe(asMutable);
  });

  it('runs a real call through an interface-declared contract', async () => {
    const reply = await interfaceLayer.call('chat', { prompt: 'hello' });
    expect(reply.text).toBe('openai:hello');
    const embedded = await interfaceLayer.call('embed', { texts: ['a', 'b'] });
    expect(embedded.vectors).toEqual([0, 1]);
    await interfaceLayer.close();
  });
});
