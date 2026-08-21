/**
 * WAVE 3 / T5 — ERROR-MESSAGE READABILITY, and the aliases that buy it.
 *
 * R6 §5.1 closes with a rule earned by before/after measurement:
 *
 * > That last one is only readable because `LayerEventName` is a **named
 * > alias**. Declared inline as `E extends keyof LayerEvents<C, Names>` the
 * > message printed the raw generic — verified before/after.
 * > **Rule: never expose `keyof SomeGeneric<…>` in a public signature; alias it.**
 *
 * A compiler cannot be asked "is this message readable?", so this file does the
 * two things that can be checked mechanically:
 *
 *  1. It asserts that every union a user meets in an error message is a NAMED,
 *     EXPORTED alias resolving to a small literal union — `ErrorCode`,
 *     `InterlayerEventName`, `BreakerState`, `CapabilityName<C>` — rather than
 *     an anonymous `keyof …` expression.
 *
 *  2. It parks one negative case per user-facing union so the actual text can
 *     be captured. Stripping the suppressions and reading `tsc`'s output is how
 *     the T5 report quotes the message a user really sees; the expected text is
 *     written above each directive.
 *
 * The one place the rule is NOT honoured is recorded at the bottom.
 */

import { describe, expect, it } from 'vitest';
import {
  type BreakerState,
  type CapabilityName,
  capability,
  createLayer,
  defineContract,
  defineProvider,
  type ErrorCode,
  hasCode,
  type Infer,
  type InterlayerEventName,
  type PerCapabilityResilience,
  TimeoutError,
  v,
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
  capabilities: { chat: async (input) => ({ text: `openai:${input.prompt}` }) },
});

const layer = createLayer({ contract: ai, providers: [openai], runtime: createFakeRuntime() });

/* ================================================================== *
 * 1. Every user-facing union is a named alias over a small literal set
 * ================================================================== */

type R01_ErrorCodeIsALiteralUnion = Expect<
  Equal<
    ErrorCode,
    | 'VALIDATION'
    | 'CONFIG'
    | 'UNSUPPORTED_CAPABILITY'
    | 'NO_PROVIDER'
    | 'TRANSPORT'
    | 'TIMEOUT'
    | 'CANCELLED'
    | 'RATE_LIMITED'
    | 'CIRCUIT_OPEN'
    | 'BULKHEAD_FULL'
    | 'PROVIDER_ERROR'
    | 'RETRY_EXHAUSTED'
    | 'ALL_FAILED'
  >
>;
type R02_BreakerStateIsALiteralUnion = Expect<Equal<BreakerState, 'closed' | 'open' | 'half-open'>>;
type R03_EventNameIsAliased = Expect<Equal<InterlayerEventName, keyof InterlayerEventsShape>>;
type R04_CapabilityNameIsALiteralUnion = Expect<Equal<CapabilityName<Ai>, 'chat' | 'embed'>>;
type R05_PerCapabilityKeysAreTheContract = Expect<
  Equal<keyof PerCapabilityResilience<Ai>, 'chat' | 'embed'>
>;

/** Local restatement, so R03 compares the alias against a spelled-out set. */
interface InterlayerEventsShape {
  'call:start': unknown;
  'call:success': unknown;
  'call:failure': unknown;
  'provider:selected': unknown;
  'attempt:start': unknown;
  'attempt:success': unknown;
  'attempt:failure': unknown;
  'retry:scheduled': unknown;
  'breaker:transition': unknown;
  'ratelimit:throttled': unknown;
  'fallback:advance': unknown;
  'listener:error': unknown;
}

/* ================================================================== *
 * 2. `hasCode` — the alias is what makes the guard usable
 * ================================================================== */

function narrowByCode(e: unknown) {
  if (hasCode(e, 'TIMEOUT')) return e;
  return undefined;
}
type R06_HasCodeNarrowsToTheClass = Expect<
  Equal<NonNullable<ReturnType<typeof narrowByCode>>, TimeoutError>
>;
type R07_HasCodeTakesTheAlias = Expect<Equal<Parameters<typeof hasCode>[1], ErrorCode>>;

function codeNegatives(e: unknown): unknown[] {
  const out: unknown[] = [];

  // R-N01 — expected text: `Argument of type '"NOT_A_CODE"' is not assignable
  //         to parameter of type 'ErrorCode'.` (the ALIAS name, not the union)
  // @ts-expect-error TS2345: '"NOT_A_CODE"' is not assignable to 'ErrorCode'
  out.push(hasCode(e, 'NOT_A_CODE'));

  // R-N02 — a lowercase code is a different literal, not a near-miss.
  // @ts-expect-error TS2345: '"timeout"' is not assignable to 'ErrorCode'
  out.push(hasCode(e, 'timeout'));

  if (hasCode(e, 'TIMEOUT')) {
    // R-N03 — the narrowed class really is `TimeoutError`.
    // @ts-expect-error TS2339: Property 'failures' does not exist on 'TimeoutError'
    out.push(e.failures);
  }

  return out;
}

/* ================================================================== *
 * 3. Configuration keys — the `Did you mean` cases R6 measured
 * ================================================================== */

function configNegatives(): unknown[] {
  const out: unknown[] = [];
  const runtime = createFakeRuntime();

  // R-N04 — R6's example verbatim: a typo'd capability in `perCapability`.
  // Expected: TS2561 `… but 'chats' does not exist in type
  // 'PerCapabilityResilience<…>'. Did you mean to write 'chat'?`
  out.push(
    createLayer({
      contract: ai,
      providers: [openai],
      runtime,
      // @ts-expect-error TS2561: 'chats' does not exist; did you mean 'chat'?
      perCapability: { chats: {} },
    }),
  );

  // R-N05 — an unknown top-level key on the layer config.
  out.push(
    createLayer({
      contract: ai,
      providers: [openai],
      runtime,
      // @ts-expect-error TS2353: 'retries' does not exist in type 'LayerConfig<…>'
      retries: 3,
    }),
  );

  // R-N06 — a typo inside the resilience options.
  out.push(
    createLayer({
      contract: ai,
      providers: [openai],
      runtime,
      // @ts-expect-error TS2353: 'maxAttempt' does not exist in type 'RetryOptions'
      resilience: { retry: { maxAttempt: 2 } },
    }),
  );

  // R-N07 — a typo in the capability descriptor's advisory metadata.
  // @ts-expect-error TS2353: 'idempotant' does not exist in type 'CapabilityMeta'
  out.push(capability<ChatIn, ChatOut>({ idempotant: true }));

  return out;
}

/* ================================================================== *
 * 4. `Infer` — the validator combinators carry their own types
 * ================================================================== */

const chatSchema = v.object({ prompt: v.string(), retries: v.number() });
type R08_InferReadsTheShape = Expect<
  Equal<Infer<typeof chatSchema>, { prompt: string; retries: number }>
>;

const optionalSchema = v.object({ prompt: v.string(), tag: v.optional(v.string()) });
type R09_InferKeepsOptionalAsUndefinedUnion = Expect<
  Equal<Infer<typeof optionalSchema>, { prompt: string; tag: string | undefined }>
>;

const nestedSchema = v.array(v.object({ id: v.integer() }));
type R10_InferNests = Expect<Equal<Infer<typeof nestedSchema>, { id: number }[]>>;

const literalSchema = v.literal('fast');
type R11_InferKeepsLiterals = Expect<Equal<Infer<typeof literalSchema>, 'fast'>>;

function inferNegatives(): unknown[] {
  const out: unknown[] = [];
  const parsed = v.parse({ prompt: 'x', retries: 1 }, chatSchema);
  // R-N08 — the parsed value is the shape, not `unknown`.
  // @ts-expect-error TS2339: Property 'nope' does not exist on the parsed shape
  out.push(parsed.nope);

  // R-N09 — `min` only accepts a numeric validator.
  // @ts-expect-error TS2345: 'Validator<string>' is not assignable to 'Validator<number>'
  out.push(v.min(v.string(), 1));

  return out;
}

/* ================================================================== *
 * 5. Middleware — `use()` is typed against `CallContext`
 * ================================================================== */

function middlewareNegatives(): unknown[] {
  const out: unknown[] = [];

  // R-N10 — a field `CallContext` does not carry.
  // @ts-expect-error TS2339: Property 'provider' does not exist on type 'CallContext'
  out.push(layer.use(async (ctx, next) => next({ ...ctx, provider: undefined })));

  // R-N11 — `next` returns a promise; a middleware must return one too.
  // @ts-expect-error TS2345: 'number' is not assignable to 'Promise<unknown>'
  out.push(layer.use(() => 1));

  return out;
}

/* ================================================================== *
 * 6. FINDING — where the alias rule is NOT honoured
 * ================================================================== */

/**
 * ── FINDING 4 (recorded, not fixed) ──────────────────────────────────────
 *
 * `InterlayerEventName` and `ErrorCode` print as NAMES; captured verbatim from
 * a strip-and-verify run:
 *
 *   Argument of type '"exploded"' is not assignable to parameter of type
 *   'InterlayerEventName'.
 *   Argument of type '"NOT_A_CODE"' is not assignable to parameter of type
 *   'ErrorCode'.
 *
 * The CONTRACT type parameter does not. Because `defineContract({…})` infers an
 * anonymous object type, every message that mentions `C` dumps the whole
 * contract structurally, and it gets worse the bigger the contract is:
 *
 *   Object literal may only specify known properties, and 'retries' does not
 *   exist in type 'LayerConfig<{ chat: Capability<ChatIn, ChatOut>; embed:
 *   Capability<EmbedIn, EmbedOut>; }, readonly [Provider<{ chat:
 *   Capability<ChatIn, ChatOut>; embed: Capability<...>; }, "openai",
 *   { ...; }>]>'.
 *
 * This is NOT the mistake R6 warned about — no public signature exposes a bare
 * `keyof SomeGeneric<…>` — but it produces the same symptom. The user-side fix
 * is to give the contract a name, at which point the alias prints instead:
 *
 *   Argument of type 'InterfaceContract' is not assignable to parameter of type
 *   'Readonly<Record<string, Capability<unknown, unknown>>>'.
 *
 * The contrast below is kept compiling so any future strip-and-verify run
 * reproduces both messages side by side.
 */
const inlineContract = defineContract({ chat: capability<ChatIn, ChatOut>() });

interface NamedContract {
  readonly chat: ReturnType<typeof capability<ChatIn, ChatOut>>;
}
const namedContract: NamedContract = { chat: capability<ChatIn, ChatOut>() };

function readabilityContrast(): unknown[] {
  const out: unknown[] = [];

  // R-N12 — inline-inferred contract: the message dumps `C` STRUCTURALLY.
  out.push(
    defineProvider(inlineContract, {
      id: 'inline',
      capabilities: {
        // @ts-expect-error TS2322: 'object' is not assignable to 'ChatOut'
        chat: async (): Promise<object> => ({}),
      },
    }),
  );

  // R-N13 — the identical mistake against a NAMED contract, for comparison.
  out.push(
    defineProvider(namedContract, {
      id: 'named',
      capabilities: {
        // @ts-expect-error TS2322: 'object' is not assignable to 'ChatOut'
        chat: async (): Promise<object> => ({}),
      },
    }),
  );

  return out;
}

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  R01_ErrorCodeIsALiteralUnion,
  R02_BreakerStateIsALiteralUnion,
  R03_EventNameIsAliased,
  R04_CapabilityNameIsALiteralUnion,
  R05_PerCapabilityKeysAreTheContract,
  R06_HasCodeNarrowsToTheClass,
  R07_HasCodeTakesTheAlias,
  R08_InferReadsTheShape,
  R09_InferKeepsOptionalAsUndefinedUnion,
  R10_InferNests,
  R11_InferKeepsLiterals,
] = [true, true, true, true, true, true, true, true, true, true, true];

describe('public aliases and the error text they produce', () => {
  it('holds all 11 alias assertions', () => {
    expect(typeAssertions).toHaveLength(11);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the compile-time-only probes unexecuted', () => {
    expect(typeof codeNegatives).toBe('function');
    expect(typeof configNegatives).toBe('function');
    expect(typeof inferNegatives).toBe('function');
    expect(typeof middlewareNegatives).toBe('function');
    expect(typeof narrowByCode).toBe('function');
    expect(typeof readabilityContrast).toBe('function');
    expect(Object.keys(inlineContract)).toEqual(['chat']);
    expect(Object.keys(namedContract)).toEqual(['chat']);
  });

  it('names the validator shapes readably, which is the runtime half of the rule', () => {
    // `typeName` is what a VALIDATION error prints. Same principle as the
    // aliases above: a user reads a name, never a structural dump.
    expect(chatSchema.typeName).toBe('{ prompt: string; retries: number }');
    expect(literalSchema.typeName).toBe('"fast"');
  });

  it('parses through the public `v` namespace', () => {
    const parsed = v.parse({ prompt: 'hi', retries: 2 }, chatSchema);
    expect(parsed).toEqual({ prompt: 'hi', retries: 2 });
  });

  it('narrows a real error by code', () => {
    const timeout: unknown = new TimeoutError(50, 'call');
    expect(hasCode(timeout, 'TIMEOUT')).toBe(true);
    expect(hasCode(timeout, 'ALL_FAILED')).toBe(false);
    expect(narrowByCode(timeout)?.timeoutMs).toBe(50);
    expect(narrowByCode(new Error('plain'))).toBeUndefined();
  });

  it('runs a real call through the layer these aliases describe', async () => {
    const reply = await layer.call('chat', { prompt: 'hi' });
    expect(reply.text).toBe('openai:hi');
    await layer.close();
  });
});
