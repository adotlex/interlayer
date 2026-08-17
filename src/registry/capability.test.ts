/**
 * Capability tokens and contract declaration.
 *
 * HOW TO READ A FAILURE HERE. The negative cases use `@ts-expect-error`. If one
 * stops firing, `tsc` reports `TS2578: Unused '@ts-expect-error' directive` and
 * `npm run typecheck` turns red. That is NOT cosmetic: an unused directive
 * means a bogus capability name or a wrong meta value is compiling clean, and
 * the value proposition of the library has silently evaporated.
 */

import { describe, expect, it } from 'vitest';
import { ConfigError } from '../core/errors.ts';
import type { AnyContract, Capability, CapabilityName, InputOf, OutputOf } from '../core/types.ts';
import {
  type CapabilityMeta,
  capability,
  capabilityMeta,
  capabilityNames,
  declaresCapability,
  defineContract,
} from './capability.ts';

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

const ai = defineContract({
  chat: capability<ChatIn, ChatOut>({ idempotent: false, description: 'one-shot completion' }),
  embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
  transcribe: capability<TranscribeIn, TranscribeOut>({ idempotent: true }),
});
type Ai = typeof ai;

/* ---------- P: positive type assertions ---------- */

type P1_KeysStayLiteral = Expect<Equal<CapabilityName<Ai>, 'chat' | 'embed' | 'transcribe'>>;
type P2_InputCarried = Expect<Equal<InputOf<Ai, 'chat'>, ChatIn>>;
type P3_OutputCarried = Expect<Equal<OutputOf<Ai, 'chat'>, ChatOut>>;
type P4_PairsAreIndependent = Expect<Equal<OutputOf<Ai, 'embed'>, EmbedOut>>;

/** An `interface` contract keeps its literal keys under the F-bounded form. */
interface InterfaceContract {
  readonly chat: Capability<ChatIn, ChatOut>;
}
type P5_InterfaceKeysStayLiteral = Expect<Equal<CapabilityName<InterfaceContract>, 'chat'>>;

/**
 * THE TRAP, as a type. The erased index-signature form collapses the key union
 * to `string`, which is why `Contract<C>` must stay F-bounded and why nothing
 * in this unit ever constrains on `AnyContract`.
 */
type P6_ErasedFormCollapses = Expect<Equal<CapabilityName<AnyContract>, string>>;

const names = capabilityNames(ai);
type P7_NamesKeepTheUnion = Expect<
  Equal<typeof names, readonly ('chat' | 'embed' | 'transcribe')[]>
>;

/* ---------- N: negative cases. Compiling IS the assertion. ---------- */

function negativeCases(): unknown[] {
  const out: unknown[] = [];

  // N1 — a meta field with the wrong value type.
  // @ts-expect-error 'yes' is not assignable to 'boolean | undefined'
  out.push(capability<ChatIn, ChatOut>({ idempotent: 'yes' }));

  // N2 — a meta field that does not exist.
  // @ts-expect-error 'retries' does not exist in type 'CapabilityMeta'
  out.push(capability<ChatIn, ChatOut>({ retries: 3 }));

  // N3 — indexing the contract with a name it does not declare.
  // @ts-expect-error '"chatt"' does not satisfy the constraint 'keyof Ai'
  type N3 = InputOf<Ai, 'chatt'>;
  out.push(null as unknown as N3);

  // N4 — a contract member that is not a capability descriptor.
  // @ts-expect-error 'string' is not assignable to 'Capability<unknown, unknown>'
  out.push(defineContract({ chat: 'not a descriptor' }));

  return out;
}

/* ---------- consume every assertion ---------- */

const typeAssertions: [
  P1_KeysStayLiteral,
  P2_InputCarried,
  P3_OutputCarried,
  P4_PairsAreIndependent,
  P5_InterfaceKeysStayLiteral,
  P6_ErasedFormCollapses,
  P7_NamesKeepTheUnion,
] = [true, true, true, true, true, true, true];

/* ---------- runtime ---------- */

describe('capability()', () => {
  it('keeps the phantom carrier type-only — it must never exist at runtime', () => {
    const c = capability<ChatIn, ChatOut>({ idempotent: true });
    expect(Object.getOwnPropertySymbols(c)).toHaveLength(0);
    expect(Object.keys(c)).toEqual(['idempotent']);
    expect(JSON.parse(JSON.stringify(c))).toEqual({ idempotent: true });
  });

  it('defaults to empty metadata', () => {
    const c = capability<ChatIn, ChatOut>();
    expect(Object.keys(c)).toHaveLength(0);
  });

  it('carries the metadata through unchanged', () => {
    const c = capability<ChatIn, ChatOut>({ idempotent: false, description: 'chat' });
    expect(c.idempotent).toBe(false);
    expect(c.description).toBe('chat');
  });

  it('copies the metadata, so a later mutation of the caller literal is inert', () => {
    const meta: { idempotent?: boolean | undefined } = { idempotent: true };
    const c = capability<ChatIn, ChatOut>(meta);
    meta.idempotent = false;
    expect(c.idempotent).toBe(true);
  });

  it('freezes the descriptor', () => {
    const c = capability<ChatIn, ChatOut>({ idempotent: true });
    expect(Object.isFrozen(c)).toBe(true);
    expect(() => {
      (c as { idempotent?: boolean }).idempotent = false;
    }).toThrow(TypeError);
  });
});

describe('defineContract()', () => {
  it('is identity at runtime — it exists to supply a contextual type', () => {
    const source = { chat: capability<ChatIn, ChatOut>() };
    expect(defineContract(source)).toBe(source);
  });

  it('rejects a member that cannot be a capability descriptor', () => {
    const bogus = { chat: 'not a descriptor' } as unknown as { chat: Capability<never, never> };
    expect(() => defineContract(bogus)).toThrow(ConfigError);
    try {
      defineContract(bogus);
      expect.unreachable('defineContract should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(ConfigError);
      expect((e as ConfigError).code).toBe('CONFIG');
      expect((e as ConfigError).message).toContain('chat');
    }
  });

  it('rejects a null member', () => {
    const bogus = { chat: null } as unknown as { chat: Capability<never, never> };
    expect(() => defineContract(bogus)).toThrow(/not a capability descriptor/);
  });

  it('rejects a non-object contract', () => {
    const bogus = 'nope' as unknown as { chat: Capability<never, never> };
    expect(() => defineContract(bogus)).toThrow(ConfigError);
  });

  it('accepts an empty contract — a contract with nothing in it is legal, if useless', () => {
    expect(capabilityNames(defineContract({}))).toEqual([]);
  });
});

describe('capabilityNames()', () => {
  it('returns the declared names in declaration order', () => {
    expect(names).toEqual(['chat', 'embed', 'transcribe']);
  });

  it('is deterministic across calls', () => {
    expect(capabilityNames(ai)).toEqual(capabilityNames(ai));
  });
});

describe('capabilityMeta()', () => {
  it('projects the advisory metadata of a declared capability', () => {
    expect(capabilityMeta(ai, 'chat')).toEqual({
      idempotent: false,
      description: 'one-shot completion',
    });
  });

  it('omits absent fields rather than reporting them as undefined', () => {
    const meta: CapabilityMeta | undefined = capabilityMeta(ai, 'embed');
    expect(meta).toEqual({ idempotent: true });
    expect(Object.hasOwn(meta ?? {}, 'description')).toBe(false);
  });

  it('returns undefined for a name the contract does not declare', () => {
    expect(capabilityMeta(ai, 'chatt')).toBeUndefined();
  });
});

describe('declaresCapability()', () => {
  it('narrows a runtime string to the contract key union', () => {
    const probe = 'chat';
    expect(declaresCapability(ai, probe)).toBe(true);
    if (declaresCapability(ai, probe)) {
      const narrowed: CapabilityName<Ai> = probe;
      expect(narrowed).toBe('chat');
    }
  });

  it('is false for an undeclared name', () => {
    expect(declaresCapability(ai, 'chatt')).toBe(false);
  });

  it('is false for an inherited property — own keys only', () => {
    expect(declaresCapability(ai, 'toString')).toBe(false);
    expect(declaresCapability(ai, 'constructor')).toBe(false);
  });
});

describe('compile-time guarantees', () => {
  it('holds all 7 positive type assertions', () => {
    expect(typeAssertions).toHaveLength(7);
    expect(typeAssertions.every(Boolean)).toBe(true);
  });

  it('keeps the 4 negative cases compiled but unexecuted', () => {
    // Their entire value is at compile time: every `@ts-expect-error` inside
    // must be consumed, or tsc reports TS2578 and typecheck fails.
    expect(typeof negativeCases).toBe('function');
  });
});
