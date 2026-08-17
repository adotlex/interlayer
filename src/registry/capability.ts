/**
 * Capability tokens and contract declaration — U1.
 *
 * A contract is a plain record of capability descriptors. Each descriptor is a
 * `Capability<In, Out>` from `src/core/types.ts`, whose In/Out pair rides on a
 * PHANTOM property that never exists at runtime. `capability<In, Out>()` is the
 * only place in the library that mints one.
 *
 * The whole inference technique (R6 §5.1, re-verified on tsc 6.0.3 by
 * `test/support/type-inference.test.ts`) rests on two things:
 *
 *  1. the phantom carrier stays TYPE-ONLY — never materialise `[phantom]`;
 *  2. `defineContract` constrains with the F-bounded `Contract<C>` from core,
 *     NOT with `AnyContract`. `Readonly<Record<string, Capability>>` carries a
 *     string index signature; a contract declared against it collapses
 *     `CapabilityName<C>` from `'chat' | 'embed'` to `string`, and
 *     `call('typo', {})` then compiles clean. Do not "simplify" the constraint.
 */

import { ConfigError } from '../core/errors.ts';
import type { Capability, CapabilityName, Contract } from '../core/types.ts';

/**
 * The runtime half of a capability descriptor. Everything here is advisory
 * metadata — the In/Out types are carried separately, by the phantom.
 */
export interface CapabilityMeta {
  /** Safe to re-issue. Retry and routing read this; nothing here enforces it. */
  readonly idempotent?: boolean | undefined;
  readonly description?: string | undefined;
}

/** Widens an object to a readable string-keyed table without touching values. */
function asTable(value: object): Readonly<Record<string, unknown>> {
  return value as Readonly<Record<string, unknown>>;
}

/**
 * Mints a capability descriptor carrying the `In`/`Out` pair at the type level.
 *
 * ```ts
 * const ai = defineContract({
 *   chat: capability<ChatIn, ChatOut>({ idempotent: false }),
 *   embed: capability<EmbedIn, EmbedOut>({ idempotent: true }),
 * });
 * ```
 *
 * The returned object is a frozen COPY of `meta`, so a caller mutating their
 * own literal afterwards cannot retroactively change a contract. It carries no
 * symbol keys: the phantom is a type, and materialising it would break both
 * structural typing and JSON round-tripping.
 */
export function capability<In, Out>(meta: CapabilityMeta = {}): Capability<In, Out> {
  // The single cast that mints a phantom carrier. The value is a plain object;
  // only the DECLARED type knows about In/Out.
  return Object.freeze({ ...meta }) as Capability<In, Out>;
}

/**
 * Gives an object literal its contract type without widening the key union.
 *
 * Identity at runtime apart from one cheap guard: a member that is not an
 * object cannot be a capability descriptor, and catching that here is far
 * kinder than a `TypeError` three layers down inside a provider call.
 */
export function defineContract<C extends Contract<C>>(contract: C): C {
  if (typeof contract !== 'object' || contract === null) {
    throw new ConfigError('defineContract() expects an object of capability descriptors');
  }
  for (const [name, descriptor] of Object.entries(asTable(contract))) {
    if (typeof descriptor !== 'object' || descriptor === null) {
      throw new ConfigError(
        `contract capability "${name}" is not a capability descriptor ` +
          `(got ${descriptor === null ? 'null' : typeof descriptor}). ` +
          'Declare it with capability<In, Out>().',
      );
    }
  }
  return contract;
}

/**
 * The declared capability names, in declaration order.
 *
 * The return type keeps the literal union, so this is usable as a type-safe
 * iteration source and not merely as `string[]`.
 */
export function capabilityNames<C extends Contract<C>>(contract: C): readonly CapabilityName<C>[] {
  // `Object.keys` is typed `string[]` by the standard library and cannot be
  // told otherwise; the bridge re-types it without widening anything.
  return Object.keys(asTable(contract)) as unknown as readonly CapabilityName<C>[];
}

/**
 * The advisory metadata of one capability. Returns `undefined` for a name the
 * contract does not declare, so callers holding a runtime string (the registry,
 * the router) can ask without a cast.
 */
export function capabilityMeta<C extends Contract<C>>(
  contract: C,
  name: string,
): CapabilityMeta | undefined {
  const descriptor = asTable(contract)[name];
  if (typeof descriptor !== 'object' || descriptor === null) return undefined;
  const meta = descriptor as CapabilityMeta;
  return {
    ...(meta.idempotent !== undefined ? { idempotent: meta.idempotent } : {}),
    ...(meta.description !== undefined ? { description: meta.description } : {}),
  };
}

/** Whether `name` is declared by `contract`. Narrows a runtime string. */
export function declaresCapability<C extends Contract<C>>(
  contract: C,
  name: string,
): name is CapabilityName<C> {
  return Object.hasOwn(asTable(contract), name);
}
