/**
 * Provider definition, introspection, and THE SINGLE HANDLER TYPE-ERASURE SITE
 * IN THE CODEBASE — U1.
 *
 * Above this file everything is precisely typed: a handler for capability `K`
 * takes `InputOf<C, K>` and resolves `OutputOf<C, K>`. Below it everything is
 * `(input: unknown, ctx: AttemptContext) => Promise<unknown>`, because the
 * middleware onion and the router must move values they cannot name. Exactly
 * one function crosses that line — {@link eraseHandler} — and every other file
 * in the library goes through {@link toProviderRecord} to reach it.
 *
 * ── THE FAILURE MODE THIS FILE EXISTS TO CATCH (R6 risk R1) ──
 *
 * ```ts
 * const providers: Provider<Ai>[] = [openai];   // ← WRONG
 * ```
 *
 * Annotating collapses `Id` to `string` AND fills `H` with the constraint
 * `Partial<Handlers<C>>`, whose `keyof` is EVERY capability in the contract.
 * The type then claims `openai` implements `transcribe` when the object has no
 * such handler: it fails OPEN, and the first `call('transcribe', …)` finds
 * nothing at runtime.
 *
 * ```ts
 * const providers = [openai] satisfies readonly Provider<Ai>[];   // ← RIGHT
 * ```
 *
 * `satisfies` checks the shape while preserving the literal id and the exact
 * implemented key set. Because a library cannot force its callers to write
 * `satisfies`, the erasure below derives the capability map from the RUNTIME
 * VALUE — never from the type — and the registry rejects anything that does not
 * add up. That runtime pass is the backstop, and it is not optional.
 */

import { ConfigError } from '../core/errors.ts';
import type {
  Contract,
  ErasedHandler,
  Handlers,
  HealthStatus,
  Provider,
  ProviderRecord,
  ProviderTraits,
  Runtime,
} from '../core/types.ts';
import { capabilityNames, declaresCapability } from './capability.ts';

/* ------------------------------------------------------------------ *
 * 1. Definition
 * ------------------------------------------------------------------ */

/** Optional liveness probe. Never called on the hot path. */
export type HealthProbe = (ctx: { runtime: Runtime; signal: AbortSignal }) => Promise<HealthStatus>;

/**
 * What you hand to {@link defineProvider}. Mirrors `Provider` minus the type
 * parameters the helper infers for you.
 */
export interface ProviderDefinition<
  C extends Contract<C>,
  Id extends string,
  H extends Partial<Handlers<C>>,
> {
  /** Provider IDENTITY. `id`, never `name` — `providerId` is load-bearing
   *  across the error context and the whole event map. */
  readonly id: Id;
  /** Implemented subset of the contract. Handler parameters need no annotations. */
  readonly capabilities: H;
  readonly traits?: ProviderTraits | undefined;
  readonly health?: HealthProbe | undefined;
  readonly dispose?: (() => void | Promise<void>) | undefined;
}

/**
 * The minimum structural shape the erasure needs.
 *
 * `capabilities` is `unknown` on purpose: both a `Provider` (a record of typed
 * handlers) and a `ProviderRecord` (a `Map` of erased ones) satisfy it, so
 * introspection works on either side of the line.
 */
export interface ProviderLike {
  readonly id: string;
  readonly capabilities: unknown;
  readonly traits?: ProviderTraits | undefined;
  readonly health?: HealthProbe | undefined;
  readonly dispose?: (() => void | Promise<void>) | undefined;
}

/**
 * Types a provider against a contract, inferring the literal id and the exact
 * implemented capability set.
 *
 * ```ts
 * const openai = defineProvider(ai, {
 *   id: 'openai',
 *   capabilities: {
 *     chat: async (input, ctx) => callChat(input.prompt, ctx.signal),
 *   },
 * });
 * ```
 *
 * `const Id extends string` is what stops `'openai'` widening to `string`.
 * Constraining `H` to `Partial<Handlers<C>>` while still INFERRING it is what
 * contextually types `input`/`ctx` with zero annotations and still keeps the
 * implemented keys exact.
 *
 * The runtime checks here are a courtesy — they fire at definition time, with
 * the contract in hand, which is the earliest and clearest place to fail. They
 * are NOT the backstop: a hand-rolled provider literal never reaches this
 * function, so the registry validates independently.
 */
export function defineProvider<
  C extends Contract<C>,
  const Id extends string,
  H extends Partial<Handlers<C>>,
>(contract: C, definition: ProviderDefinition<C, Id, H>): Provider<C, Id, H> {
  assertUsableId(definition.id);
  const implemented = readHandlerTable(definition.id, definition.capabilities);
  if (implemented.size === 0) {
    throw new ConfigError(
      `provider "${definition.id}" implements no capabilities; it could never be selected`,
      [],
      { providerId: definition.id },
    );
  }
  for (const name of implemented.keys()) {
    if (!declaresCapability(contract, name)) {
      throw new ConfigError(
        `provider "${definition.id}" implements "${name}", which the contract does not declare ` +
          `(declared: ${capabilityNames(contract).join(', ')})`,
        [],
        { providerId: definition.id, capability: name },
      );
    }
  }
  return definition;
}

/* ------------------------------------------------------------------ *
 * 2. Introspection — reads the VALUE, never the type
 * ------------------------------------------------------------------ */

function isReadonlyMap(value: object): value is ReadonlyMap<string, unknown> {
  return value instanceof Map;
}

/**
 * The capability names a provider ACTUALLY implements, in declaration order.
 *
 * Reads the runtime value, so it reports the truth even when the static type
 * over-reports (see the module header). Works on a `Provider` and on a
 * `ProviderRecord` alike.
 */
export function implementedCapabilities(provider: {
  readonly capabilities: unknown;
}): readonly string[] {
  const table = provider.capabilities;
  if (typeof table !== 'object' || table === null) return [];
  if (isReadonlyMap(table)) return [...table.keys()];
  return Object.entries(table as Readonly<Record<string, unknown>>)
    .filter(([, handler]) => typeof handler === 'function')
    .map(([name]) => name);
}

/**
 * Capability introspection. `true` only when a callable handler is really
 * there — a key present with a non-function value is not support.
 */
export function supports(
  provider: { readonly capabilities: unknown },
  capability: string,
): boolean {
  return implementedCapabilities(provider).includes(capability);
}

/* ------------------------------------------------------------------ *
 * 3. Erasure
 * ------------------------------------------------------------------ */

/**
 * THE SINGLE HANDLER TYPE-ERASURE SITE.
 *
 * Everything above this line is `Handler<C, K>`; everything below is
 * `ErasedHandler`. Adding a second site anywhere in the library defeats the
 * point of having one — route through {@link toProviderRecord} instead.
 */
function eraseHandler(handler: unknown): ErasedHandler {
  return handler as ErasedHandler;
}

function assertUsableId(id: unknown): asserts id is string {
  if (typeof id !== 'string' || id.trim() === '') {
    throw new ConfigError(
      `provider id must be a non-empty string (got ${id === null ? 'null' : typeof id})`,
    );
  }
}

/**
 * Validates the capability table and returns the erased handlers.
 *
 * A key whose value is `undefined` means "not implemented" and is skipped; a
 * key whose value is present but not callable is a CONFIG BUG and throws —
 * silently dropping it would hand back a provider that quietly does less than
 * it claims, which is exactly the fail-open behaviour this unit exists to stop.
 */
function readHandlerTable(id: string, capabilities: unknown): ReadonlyMap<string, ErasedHandler> {
  if (typeof capabilities !== 'object' || capabilities === null) {
    throw new ConfigError(
      `provider "${id}" has no capabilities object ` +
        `(got ${capabilities === null ? 'null' : typeof capabilities})`,
      [],
      { providerId: id },
    );
  }
  const erased = new Map<string, ErasedHandler>();
  if (isReadonlyMap(capabilities)) {
    for (const [name, handler] of capabilities) {
      if (handler === undefined) continue;
      assertCallable(id, name, handler);
      erased.set(name, eraseHandler(handler));
    }
    return erased;
  }
  for (const [name, handler] of Object.entries(capabilities as Readonly<Record<string, unknown>>)) {
    if (handler === undefined) continue;
    assertCallable(id, name, handler);
    erased.set(name, eraseHandler(handler));
  }
  return erased;
}

function assertCallable(id: string, name: string, handler: unknown): void {
  if (typeof handler !== 'function') {
    throw new ConfigError(
      `provider "${id}" declares capability "${name}" but its handler is not a function ` +
        `(got ${handler === null ? 'null' : typeof handler}). ` +
        'A provider that claims a capability it cannot serve fails OPEN — rejected.',
      [],
      { providerId: id, capability: name },
    );
  }
}

/** Fully resolved registration facts. Input options live on `RegisterOptions`. */
export interface ResolvedRegistration {
  /** Lower runs first. The registry defaults this to the registration index. */
  readonly priority: number;
  readonly enabled: boolean;
  readonly traits: ProviderTraits;
  /**
   * When present, an implemented capability outside this list is a
   * `ConfigError`. The registry passes the contract's names when it has them.
   */
  readonly allowedCapabilities?: readonly string[] | undefined;
}

/**
 * Erases a typed provider into the `ProviderRecord` the registry holds and the
 * router walks.
 *
 * This is the runtime backstop: the capability map is built from the VALUE, so
 * a provider whose type over-reports (the `Provider<C>[]` annotation trap)
 * lands in the registry declaring only what it can actually serve, and a
 * provider that claims a capability with a non-callable handler is rejected
 * outright rather than failing later at call time.
 */
export function toProviderRecord(
  provider: ProviderLike,
  resolved: ResolvedRegistration,
): ProviderRecord {
  assertUsableId(provider.id);
  const capabilities = readHandlerTable(provider.id, provider.capabilities);
  if (capabilities.size === 0) {
    throw new ConfigError(
      `provider "${provider.id}" implements no capabilities; it could never be selected`,
      [],
      { providerId: provider.id },
    );
  }
  const allowed = resolved.allowedCapabilities;
  if (allowed !== undefined) {
    for (const name of capabilities.keys()) {
      if (!allowed.includes(name)) {
        throw new ConfigError(
          `provider "${provider.id}" implements "${name}", which the contract does not declare ` +
            `(declared: ${allowed.join(', ')})`,
          [],
          { providerId: provider.id, capability: name },
        );
      }
    }
  }
  if (!Number.isFinite(resolved.priority)) {
    throw new ConfigError(`provider "${provider.id}" has a non-finite priority`, [], {
      providerId: provider.id,
    });
  }
  return {
    id: provider.id,
    capabilities,
    traits: resolved.traits,
    priority: resolved.priority,
    enabled: resolved.enabled,
    ...(provider.health !== undefined ? { health: provider.health } : {}),
    ...(provider.dispose !== undefined ? { dispose: provider.dispose } : {}),
  };
}
