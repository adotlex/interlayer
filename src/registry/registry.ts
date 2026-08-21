/**
 * The provider registry — U1.
 *
 * Owns provider identity, priority ordering, enable/disable, capability
 * negotiation and disposal. It is the one place that turns "some objects a user
 * handed us" into the ordered `ProviderRecord` list the router walks.
 *
 * ── DETERMINISM ──
 * Nothing here reads the clock or the PRNG. Ordering is `priority` ascending,
 * ties broken by REGISTRATION INDEX, so the candidate list for a given set of
 * registrations is byte-identical on every run and on every machine. No
 * `Runtime` is needed and none is taken; that is a property, not an oversight.
 *
 * ── THE RUNTIME BACKSTOP ──
 * `register` never trusts the static type of what it is given (see the header
 * of `provider.ts`): the capability map is derived from the runtime value, a
 * declared-but-uncallable handler is rejected, and — when the registry was
 * built with a contract — a capability outside the contract is rejected too.
 * `Provider<C>[]` annotations over-report, and over-reporting fails OPEN.
 */

import { ConfigError } from '../core/errors.ts';
import type {
  AnyContract,
  Contract,
  Handlers,
  ProviderRecord,
  ProviderTraits,
  RegisterOptions,
  Registrable,
  Registry,
} from '../core/types.ts';
import { capabilityNames } from './capability.ts';
import { type ProviderLike, toProviderRecord } from './provider.ts';

export interface RegistryOptions<C extends Contract<C>> {
  /**
   * The contract these providers implement. Supplying it turns on the strictest
   * backstop: registering a handler under a name the contract does not declare
   * (a typo, or a provider built for a different contract) becomes a
   * `ConfigError` instead of a capability nobody can ever call.
   */
  readonly contract?: C | undefined;
}

interface Entry {
  readonly record: ProviderRecord;
  /** Monotonic registration counter. Never reused, so ordering is stable. */
  readonly index: number;
}

function byPriorityThenRegistration(a: Entry, b: Entry): number {
  return a.record.priority - b.record.priority || a.index - b.index;
}

/**
 * Creates an empty registry.
 *
 * ```ts
 * const registry = createRegistry({ contract: ai })
 *   .register(openai)
 *   .register(anthropic, { priority: -1 });   // lower runs first
 *
 * registry.candidatesFor('chat');             // [anthropic, openai]
 * ```
 *
 * The contract is optional so the registry is usable in isolation (and in
 * tests) with bare `ProviderRecord`-shaped objects; pass it whenever you have
 * it, because it buys the strongest validation available.
 */
export function createRegistry<C extends Contract<C> = AnyContract>(
  options: RegistryOptions<C> = {},
): Registry<C> {
  const entries = new Map<string, Entry>();
  /** capability -> entries implementing it, kept in canonical order. */
  const byCapability = new Map<string, Entry[]>();
  const contract = options.contract;
  const allowedCapabilities = contract === undefined ? undefined : capabilityNames(contract);
  let sequence = 0;
  let disposed = false;

  function index(entry: Entry): void {
    for (const name of entry.record.capabilities.keys()) {
      const bucket = byCapability.get(name);
      if (bucket === undefined) {
        byCapability.set(name, [entry]);
        continue;
      }
      bucket.push(entry);
      bucket.sort(byPriorityThenRegistration);
    }
  }

  function deindex(entry: Entry): void {
    for (const name of entry.record.capabilities.keys()) {
      const bucket = byCapability.get(name);
      if (bucket === undefined) continue;
      const at = bucket.indexOf(entry);
      if (at !== -1) bucket.splice(at, 1);
      if (bucket.length === 0) byCapability.delete(name);
    }
  }

  const registry: Registry<C> = {
    /**
     * Accepts a typed `Provider` OR an already-erased `ProviderRecord` (see
     * `Registrable` in core). Either way the capability map is rebuilt from the
     * runtime value, so the two paths cannot diverge — the wider signature buys
     * a test double that needs no cast, not a weaker check.
     */
    register<Id extends string, H extends Partial<Handlers<C>>>(
      provider: Registrable<C, Id, H>,
      opts: RegisterOptions = {},
    ) {
      if (disposed) {
        throw new ConfigError('registry has been disposed; it accepts no further providers');
      }
      if (typeof provider !== 'object' || provider === null) {
        throw new ConfigError(
          `register() expects a provider object (got ${provider === null ? 'null' : typeof provider})`,
        );
      }
      // Both `Provider` and `ProviderRecord` are structurally `ProviderLike`;
      // the erasure reads the runtime value from here on and ignores the static
      // type entirely.
      const like: ProviderLike = provider;
      if (entries.has(like.id)) {
        throw new ConfigError(
          `duplicate provider id "${like.id}"; ids must be unique within a registry. ` +
            'Unregister the existing provider first, or give this one a different id.',
          [],
          { providerId: like.id },
        );
      }
      const traits: ProviderTraits = opts.traits ?? provider.traits ?? {};
      const record = toProviderRecord(like, {
        priority: opts.priority ?? sequence,
        enabled: opts.enabled ?? true,
        traits,
        ...(allowedCapabilities !== undefined ? { allowedCapabilities } : {}),
      });
      const entry: Entry = { record, index: sequence };
      sequence++;
      entries.set(record.id, entry);
      index(entry);
      return this;
    },

    unregister(providerId: string): boolean {
      const entry = entries.get(providerId);
      if (entry === undefined) return false;
      entries.delete(providerId);
      deindex(entry);
      return true;
    },

    get(providerId: string): ProviderRecord | undefined {
      return entries.get(providerId)?.record;
    },

    /**
     * ENABLED providers implementing `capability`, in canonical order:
     * `priority` ascending, ties broken by registration order.
     *
     * `enabled` is read at call time (it is a mutable field on the record, and
     * the router may flip it), so nothing here is cached.
     */
    candidatesFor(capability: string): readonly ProviderRecord[] {
      const bucket = byCapability.get(capability);
      if (bucket === undefined) return [];
      const out: ProviderRecord[] = [];
      for (const entry of bucket) {
        if (entry.record.enabled) out.push(entry.record);
      }
      return out;
    },

    /** Every registered provider, enabled or not, in the same canonical order. */
    list(): readonly ProviderRecord[] {
      return [...entries.values()].sort(byPriorityThenRegistration).map((e) => e.record);
    },

    setEnabled(providerId: string, enabled: boolean): boolean {
      const entry = entries.get(providerId);
      if (entry === undefined) return false;
      entry.record.enabled = enabled;
      return true;
    },

    /**
     * Whether ANY registered provider declares `capability`, enabled or not.
     *
     * Deliberately independent of `enabled`, because the two facts drive two
     * different errors: nothing declares it at all is `UNSUPPORTED_CAPABILITY`,
     * whereas declared-but-none-available right now is `NO_PROVIDER`. Folding
     * `enabled` in here would collapse that distinction.
     */
    supports(capability: string): boolean {
      return byCapability.has(capability);
    },

    /**
     * Disposes every registered provider once, in REVERSE registration order,
     * then empties the registry. Idempotent, and never rejects: one provider
     * with a broken teardown must not strand the others or mask the failure
     * that triggered the shutdown.
     */
    async dispose(): Promise<void> {
      if (disposed) return;
      disposed = true;
      const ordered = [...entries.values()].sort((a, b) => b.index - a.index);
      entries.clear();
      byCapability.clear();
      for (const entry of ordered) {
        const teardown = entry.record.dispose;
        if (teardown === undefined) continue;
        try {
          await teardown();
        } catch {
          /* teardown must never mask the real failure */
        }
      }
    },
  };

  return registry;
}
