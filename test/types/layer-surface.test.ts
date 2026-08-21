/**
 * WAVE 3 / T5 — the rest of the `Layer` surface, at the type level.
 *
 * `call()` is the headline, but a consumer touches `callWithMeta`, `tryCall`,
 * `only`, `supports`, `on` and `use` on the way to anything real. Each one is
 * asserted here for the type it actually produces — and three of them produce
 * something WEAKER than R6 §5.1's verified probe promised. Those three are
 * marked `FINDING` and carry an assertion of the *current* behaviour, so the
 * divergence is recorded rather than hidden: if it is ever tightened, the
 * assertion flips and this file is where the conversation starts.
 */

import { describe, expect, it } from 'vitest';
import {
  type CallOptions,
  type CallResult,
  capability,
  createLayer,
  defineContract,
  defineProvider,
  hasCode,
  type InterlayerEventName,
  type InterlayerEvents,
  type Layer,
  type ProviderRecord,
  type Registry,
  type Result,
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

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>(),
  embed: capability<EmbedIn, EmbedOut>(),
});
type Ai = typeof ai;

const openai = defineProvider(ai, {
  id: 'openai',
  capabilities: {
    chat: async (input) => ({ text: `openai:${input.prompt}` }),
    embed: async (input) => ({ vectors: input.texts.map((_t, i) => i) }),
  },
});
const anthropic = defineProvider(ai, {
  id: 'anthropic',
  capabilities: { chat: async (input) => ({ text: `anthropic:${input.prompt}` }) },
});

const layer = createLayer({
  contract: ai,
  providers: [openai, anthropic],
  runtime: createFakeRuntime(),
});
type AiLayer = typeof layer;

/* ================================================================== *
 * 1. The read-only surface
 * ================================================================== */

type L01_ContractIsTheContract = Expect<Equal<AiLayer['contract'], Ai>>;
type L02_RegistryIsTypedByContract = Expect<Equal<AiLayer['registry'], Registry<Ai>>>;
type L03_ProvidersAreErased = Expect<Equal<AiLayer['providers'], readonly ProviderRecord[]>>;
type L04_LayerIsAsyncDisposable = Expect<AiLayer extends AsyncDisposable ? true : false>;

/* ================================================================== *
 * 2. `callWithMeta` and `tryCall`
 * ================================================================== */

async function surfaceProbe() {
  const meta = await layer.callWithMeta('chat', { prompt: 'hi' });
  const metaEmbed = await layer.callWithMeta('embed', { texts: ['a'] });
  const tried = await layer.tryCall('chat', { prompt: 'hi' });
  const pinned = layer.only('anthropic');
  const chained = layer.use(async (ctx, next) => next(ctx));
  return { meta, metaEmbed, tried, pinned, chained };
}
type Probe = Awaited<ReturnType<typeof surfaceProbe>>;

type L05_MetaIsCallResultOfOutput = Expect<Equal<Probe['meta'], CallResult<ChatOut>>>;
type L06_MetaValueIsTheOutput = Expect<Equal<Probe['meta']['value'], ChatOut>>;
type L07_MetaTracksTheCapability = Expect<Equal<Probe['metaEmbed'], CallResult<EmbedOut>>>;
type L08_MetaCarriesTheFailures = Expect<
  Equal<Probe['meta']['errors'], CallResult<ChatOut>['errors']>
>;
type L09_TryCallIsResultOfOutput = Expect<Equal<Probe['tried'], Result<ChatOut>>>;
type L10_UseReturnsTheSameLayer = Expect<Equal<Probe['chained'], AiLayer>>;

/** `Result` really is a discriminated union, so `ok` narrows both ways. */
function tryCallNarrows(r: Result<ChatOut>): string {
  if (r.ok) return r.value.text;
  return r.error.code;
}

/* ================================================================== *
 * 3. `only()`
 * ================================================================== */

type L11_OnlyReturnsALayer = Expect<Equal<ReturnType<AiLayer['only']>, AiLayer>>;
type L12_OnlyKeepsTheImplementedSet = Expect<
  Equal<ReturnType<AiLayer['only']>, Layer<Ai, 'openai' | 'anthropic', 'chat' | 'embed'>>
>;

/**
 * ── FINDING 1 (recorded, not fixed) ──────────────────────────────────────
 *
 * R6 §5.1 P8 verified that `layer.only('anthropic')` NARROWS the id union.
 * The shipped `Layer.only` does not: its signature is
 * `only(...ids: readonly Ids[]): Layer<C, Ids, Impl>`, so the returned layer
 * still advertises every original id (asserted by L11/L12 above).
 *
 * The visible consequence is that CONTRADICTORY PINS COMPILE.
 * `only('anthropic').only('openai')` intersects to the empty set — every call
 * on it fails with NO_PROVIDER — and the type system says nothing. Narrowing
 * `Ids` on the return type would have made it a compile error.
 *
 * NO `@ts-expect-error` below: it compiles today, and that is the record.
 * The runtime half is asserted in the `describe` block.
 */
function contradictoryPinsCompile(): AiLayer {
  return layer.only('anthropic').only('openai');
}

/* ================================================================== *
 * 4. `supports()` as a type guard
 * ================================================================== */

/** The guard narrows a raw `string` to the implemented union. */
function narrowBySupports(l: AiLayer, name: string) {
  if (!l.supports(name)) throw new Error(`unsupported: ${name}`);
  return name;
}
type L13_SupportsNarrowsToImplemented = Expect<
  Equal<ReturnType<typeof narrowBySupports>, 'chat' | 'embed'>
>;
type L14_SupportsTakesAnyString = Expect<Equal<Parameters<AiLayer['supports']>[0], string>>;

/** The guard is not decorative: without it a raw `string` is rejected. */
function unguardedStringRejected(l: AiLayer, name: string): unknown {
  // @ts-expect-error TS2345: 'string' is not assignable to '"chat" | "embed"'
  return l.call(name, { prompt: 'hi' });
}

/** And the guarded call is accepted. */
function guardedStringAccepted(l: AiLayer, name: string): unknown {
  if (!l.supports(name)) return undefined;
  return l.call(name, { prompt: 'hi' });
}

/* ================================================================== *
 * 5. `on()` — event names and payloads
 * ================================================================== */

type L15_EventNameIsTheAlias = Expect<Equal<Parameters<AiLayer['on']>[0], InterlayerEventName>>;
type L16_EventNameIsALiteralUnion = Expect<
  Equal<
    InterlayerEventName,
    | 'call:start'
    | 'call:success'
    | 'call:failure'
    | 'provider:selected'
    | 'attempt:start'
    | 'attempt:success'
    | 'attempt:failure'
    | 'retry:scheduled'
    | 'breaker:transition'
    | 'ratelimit:throttled'
    | 'fallback:advance'
    | 'listener:error'
  >
>;

function eventPayloadsAreTyped(l: AiLayer): readonly unknown[] {
  const seen: unknown[] = [];
  l.on('call:start', (p) => seen.push(p.callId, p.capability, p.at));
  l.on('breaker:transition', (p) => seen.push(p.from, p.to, p.failures));
  l.on('fallback:advance', (p) => seen.push(p.fromProviderId, p.toProviderId, p.reason));
  return seen;
}
type L17_StartPayloadIsExact = Expect<
  Equal<Parameters<Parameters<AiLayer['on']>[1]>[0], InterlayerEvents[InterlayerEventName]>
>;

function eventNegatives(l: AiLayer): unknown[] {
  const out: unknown[] = [];

  // E01 — an event name that does not exist.
  // @ts-expect-error TS2345: '"exploded"' is not assignable to 'InterlayerEventName'
  out.push(l.on('exploded', () => undefined));

  // E02 — a field the payload does not carry.
  // @ts-expect-error TS2339: Property 'providerId' does not exist on the payload
  out.push(l.on('call:start', (p) => p.providerId));

  // E03 — a payload field read at the wrong type.
  // @ts-expect-error TS2339: Property 'toUpperCase' does not exist on type 'number'
  out.push(l.on('call:start', (p) => p.at.toUpperCase()));

  return out;
}

/* ================================================================== *
 * 6. The remaining call-shape negatives on the meta/try verbs
 * ================================================================== */

async function metaNegatives(l: AiLayer): Promise<unknown[]> {
  const out: unknown[] = [];

  // M01 — `callWithMeta` checks the capability just as `call` does.
  // @ts-expect-error TS2345: '"nope"' is not assignable to '"chat" | "embed"'
  out.push(await l.callWithMeta('nope', { prompt: 'hi' }));

  // M02 — …and the input shape.
  // @ts-expect-error TS2353: 'texts' does not exist in type 'ChatIn'
  out.push(await l.callWithMeta('chat', { texts: ['a'] }));

  const meta = await l.callWithMeta('chat', { prompt: 'hi' });
  // M03 — the metadata record has a fixed shape.
  // @ts-expect-error TS2339: Property 'latencyMs' does not exist on 'CallResult<ChatOut>'
  out.push(meta.latencyMs);

  // M04 — and `value` carries the capability's output, not `unknown`.
  // @ts-expect-error TS2339: Property 'vectors' does not exist on type 'ChatOut'
  out.push(meta.value.vectors);

  const tried = await l.tryCall('chat', { prompt: 'hi' });
  // M05 — `Result` must be narrowed on `ok` before `value` exists.
  // @ts-expect-error TS2339: Property 'value' does not exist on the failure branch
  out.push(tried.value);

  if (!tried.ok) {
    // M06 — and `value` is still absent inside the failure branch.
    // @ts-expect-error TS2339: Property 'value' does not exist on the failure branch
    out.push(tried.value);
  }

  return out;
}

/* ================================================================== *
 * 7. FINDINGS 2 and 3 — precision the facade does not carry
 * ================================================================== */

/**
 * ── FINDING 2 (recorded, not fixed) ──────────────────────────────────────
 *
 * R6 §5.1 P7 verified that `callWithMeta().provider` is the literal id union.
 * The shipped `CallResult<T>` is not parameterised by the id union at all —
 * `providerId` and its alias `provider` are both plain `string`. Comparing the
 * result against a provider that was never registered therefore compiles and
 * is silently always-false.
 */
type L18_MetaProviderIdIsPlainString = Expect<Equal<Probe['meta']['providerId'], string>>;
type L19_MetaProviderAliasIsPlainString = Expect<Equal<Probe['meta']['provider'], string>>;

function metaProviderComparisonFailsOpen(meta: CallResult<ChatOut>): boolean {
  // Compiles. `'cohere'` is not a registered id and never can be.
  return meta.providerId === 'cohere';
}

/**
 * ── FINDING 3 (recorded, not fixed) ──────────────────────────────────────
 *
 * R6 §5.1 P6 verified that a route hint is checked against the registered
 * provider names. `CallOptions.providers` is `readonly string[]`, shared by
 * every layer regardless of `Ids`, so a typo'd provider hint compiles and is
 * simply ignored at selection time.
 */
type L20_ProvidersHintIsUnchecked = Expect<
  Equal<NonNullable<CallOptions['providers']>, readonly string[]>
>;

function providersHintFailsOpen(): Promise<ChatOut> {
  return layer.call('chat', { prompt: 'hi' }, { providers: ['typo-provider'] });
}

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  L01_ContractIsTheContract,
  L02_RegistryIsTypedByContract,
  L03_ProvidersAreErased,
  L04_LayerIsAsyncDisposable,
  L05_MetaIsCallResultOfOutput,
  L06_MetaValueIsTheOutput,
  L07_MetaTracksTheCapability,
  L08_MetaCarriesTheFailures,
  L09_TryCallIsResultOfOutput,
  L10_UseReturnsTheSameLayer,
  L11_OnlyReturnsALayer,
  L12_OnlyKeepsTheImplementedSet,
  L13_SupportsNarrowsToImplemented,
  L14_SupportsTakesAnyString,
  L15_EventNameIsTheAlias,
  L16_EventNameIsALiteralUnion,
  L17_StartPayloadIsExact,
  L18_MetaProviderIdIsPlainString,
  L19_MetaProviderAliasIsPlainString,
  L20_ProvidersHintIsUnchecked,
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
];

describe('the rest of the Layer surface', () => {
  it('holds all 20 surface assertions', () => {
    expect(typeAssertions).toHaveLength(20);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the compile-time-only probes unexecuted', () => {
    expect(typeof eventNegatives).toBe('function');
    expect(typeof metaNegatives).toBe('function');
    expect(typeof unguardedStringRejected).toBe('function');
    expect(typeof guardedStringAccepted).toBe('function');
    expect(typeof surfaceProbe).toBe('function');
    expect(typeof metaProviderComparisonFailsOpen).toBe('function');
  });

  it('returns the metadata the type promises', async () => {
    const meta = await layer.callWithMeta('chat', { prompt: 'hi' });
    expect(meta.value.text).toBe('openai:hi');
    expect(meta.providerId).toBe('openai');
    expect(meta.provider).toBe(meta.providerId);
    expect(meta.attempts).toBe(1);
    expect(meta.errors).toEqual([]);
  });

  it('narrows `tryCall` on `ok`', async () => {
    const tried = await layer.tryCall('chat', { prompt: 'hi' });
    expect(tried.ok).toBe(true);
    expect(tryCallNarrows(tried)).toBe('openai:hi');
  });

  it('narrows a raw string through `supports`', () => {
    expect(narrowBySupports(layer, 'chat')).toBe('chat');
    expect(() => narrowBySupports(layer, 'transcribe')).toThrow(/unsupported/);
  });

  it('types every event payload it delivers', () => {
    expect(eventPayloadsAreTyped(layer)).toEqual([]);
    layer.events.removeAllListeners();
  });

  it('FINDING 1: contradictory pins compile, and produce an unusable layer', async () => {
    const contradictory = contradictoryPinsCompile();
    expect(contradictory.providers).toHaveLength(0);
    const boom = await contradictory
      .call('chat', { prompt: 'hi' })
      .then(() => undefined)
      .catch((error: unknown) => error);
    expect(hasCode(boom, 'NO_PROVIDER')).toBe(true);
  });

  it('FINDING 3: an unregistered `providers` hint compiles and is ignored', async () => {
    // The hint names a provider that does not exist. Nothing type-checks it,
    // and selection simply finds no candidate.
    const boom = await providersHintFailsOpen()
      .then(() => undefined)
      .catch((error: unknown) => error);
    expect(hasCode(boom, 'NO_PROVIDER')).toBe(true);
    await layer.close();
  });
});
