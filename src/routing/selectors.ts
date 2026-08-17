/**
 * Candidate selection — U6, `src/routing/`.
 *
 * A `Selector` (declared in `src/core/types.ts`) takes the candidate providers
 * the registry offered and returns them in the order the fallback chain should
 * walk them. It may also DROP candidates: routing hints and tag filters are
 * expressed as selectors too.
 *
 * Every selector here is either PURE or driven by `ctx.runtime` — never by
 * `Math.random()` and never by `Date.now()`. `weighted()` draws from
 * `ctx.runtime.random`, so a seeded runtime pins the permutation exactly and
 * tests assert an exact provider order rather than a distribution.
 *
 * Selectors compose: `chainSelectors(filterByTags(['eu']), weighted())`.
 */

import type { Random } from '../core/clock.ts';
import type { CallContext, ProviderRecord, Selector } from '../core/types.ts';

/* ------------------------------------------------------------------ *
 * 1. Order-preserving and filtering selectors (pure)
 * ------------------------------------------------------------------ */

/**
 * Identity: walk the candidates exactly as handed in.
 *
 * This is deliberately NOT a priority sort. `Registry.candidatesFor()` already
 * returns "enabled providers declaring `capability`, sorted by priority then
 * registration order", and a caller's `hints.providers` pin carries its own
 * meaningful order ("restrict candidates to these provider ids, IN THIS
 * ORDER"). Re-sorting here would silently discard that pin.
 */
export function inOrder(): Selector {
  return (candidates: readonly ProviderRecord[]): readonly ProviderRecord[] => candidates;
}

/** The router's default. Exported so `src/layer.ts` can name it. */
export const defaultSelector: Selector = inOrder();

function hasAllTags(required: readonly string[]): (provider: ProviderRecord) => boolean {
  return (provider: ProviderRecord): boolean => {
    const own = provider.traits.tags ?? [];
    return required.every((tag) => own.includes(tag));
  };
}

/**
 * Keeps candidates carrying ALL of `tags` (conjunctive, not disjunctive).
 *
 * With no argument it reads `ctx.hints.tags`, so `filterByTags()` is the
 * selector form of the per-call `tags` hint. An empty requirement is the
 * identity — it never empties the pool by accident.
 */
export function filterByTags(tags?: readonly string[]): Selector {
  return (candidates: readonly ProviderRecord[], ctx: CallContext): readonly ProviderRecord[] => {
    const required = tags ?? ctx.hints.tags ?? [];
    if (required.length === 0) return candidates;
    return candidates.filter(hasAllTags(required));
  };
}

/**
 * Applies the caller's routing hints: `tags` filters the pool, `providers`
 * restricts it to those ids AND fixes their order.
 *
 * The router runs this ahead of the configured selector so `CallOptions.tags`
 * and `CallOptions.providers` (and therefore `Layer.only(...)`) are honoured
 * whichever selector is installed. Applying it twice is a no-op.
 */
export function byHints(): Selector {
  return (candidates: readonly ProviderRecord[], ctx: CallContext): readonly ProviderRecord[] => {
    const tags = ctx.hints.tags;
    const tagged =
      tags === undefined || tags.length === 0 ? candidates : candidates.filter(hasAllTags(tags));

    const ids = ctx.hints.providers;
    if (ids === undefined) return tagged;

    const byId = new Map(tagged.map((provider) => [provider.id, provider] as const));
    const picked: ProviderRecord[] = [];
    for (const id of ids) {
      const found = byId.get(id);
      // `byId` de-duplicates the pool; deleting also de-duplicates the hint.
      if (found !== undefined) {
        picked.push(found);
        byId.delete(id);
      }
    }
    return picked;
  };
}

/** Left-to-right composition. `chainSelectors()` with no arguments is identity. */
export function chainSelectors(...selectors: readonly Selector[]): Selector {
  return (candidates: readonly ProviderRecord[], ctx: CallContext): readonly ProviderRecord[] => {
    let pool = candidates;
    for (const select of selectors) pool = select(pool, ctx);
    return pool;
  };
}

/* ------------------------------------------------------------------ *
 * 2. Rotating selection (stateful, but deterministic — no clock, no PRNG)
 * ------------------------------------------------------------------ */

export interface RoundRobinOptions {
  /** First rotation offset. Default 0, so the first call is the identity. */
  readonly start?: number | undefined;
}

/**
 * Rotates the candidate list by one position per invocation, so successive
 * calls lead with a different provider while the remaining fallback order stays
 * stable.
 *
 * State lives in the closure: one selector instance is one rotation counter.
 * There is no randomness and no clock, so a test asserts the exact sequence.
 * Empty candidate lists do not advance the counter.
 */
export function roundRobin(options: RoundRobinOptions = {}): Selector {
  let counter = Math.max(0, Math.trunc(options.start ?? 0));
  return (candidates: readonly ProviderRecord[]): readonly ProviderRecord[] => {
    if (candidates.length === 0) return candidates;
    const offset = counter % candidates.length;
    counter = (counter + 1) % Number.MAX_SAFE_INTEGER;
    if (offset === 0) return candidates;
    return [...candidates.slice(offset), ...candidates.slice(0, offset)];
  };
}

/* ------------------------------------------------------------------ *
 * 3. Sticky selection (pure — rendezvous hashing)
 * ------------------------------------------------------------------ */

/** FNV-1a, 32-bit. Pure, allocation-free, identical on every machine. */
function hash32(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

export interface StickyOptions {
  /** Fixed key, overriding the per-call hint. */
  readonly key?: string | undefined;
  /** Derives the key from the context. Default: `ctx.hints.stickyKey`. */
  readonly keyOf?: ((ctx: CallContext) => string | undefined) | undefined;
}

/**
 * Pins a key to a provider, and — because the fallback chain needs a full
 * ordering, not just a pick — orders the rest deterministically behind it.
 *
 * Uses RENDEZVOUS (highest-random-weight) hashing rather than
 * `hash(key) % length`: scoring each provider independently means adding or
 * removing one provider only remaps the keys that pointed AT it, instead of
 * reshuffling every key. Same key + same pool => byte-identical order, forever.
 *
 * With no key available it is the identity.
 */
export function sticky(options: StickyOptions = {}): Selector {
  return (candidates: readonly ProviderRecord[], ctx: CallContext): readonly ProviderRecord[] => {
    if (candidates.length < 2) return candidates;
    const key =
      options.key ?? (options.keyOf === undefined ? ctx.hints.stickyKey : options.keyOf(ctx));
    if (key === undefined || key === '') return candidates;
    return candidates
      .map((provider, index) => ({ provider, index, score: hash32(`${key}#${provider.id}`) }))
      .sort((a, b) => b.score - a.score || a.index - b.index)
      .map((entry) => entry.provider);
  };
}

/* ------------------------------------------------------------------ *
 * 4. Weighted selection (Runtime-driven)
 * ------------------------------------------------------------------ */

export interface WeightedOptions {
  /** Weight per provider id. Anything unlisted weighs 1. */
  readonly weights?: Readonly<Record<string, number>> | undefined;
  /** Full override; takes precedence over `weights`. */
  readonly weightOf?: ((provider: ProviderRecord) => number) | undefined;
  /** Override the PRNG. Default: `ctx.runtime.random` — never `Math.random`. */
  readonly random?: Random | undefined;
}

/**
 * Weighted random permutation: sampling without replacement, so the result is a
 * full fallback order whose FIRST element is drawn proportionally to weight.
 *
 * Determinism: draws come from `ctx.runtime.random` (or `options.random`), so
 * `createFakeRuntime({ seed })` reproduces the permutation byte-for-byte and
 * `scriptedRandom([...])` pins it outright. Exactly `k - 1` values are drawn for
 * `k` positive-weight candidates — the last one needs no draw.
 *
 * Non-positive, `NaN` and infinite weights are treated as zero: those providers
 * are never drawn, but they are kept at the TAIL of the order so they remain
 * reachable as last-resort fallbacks rather than silently disappearing.
 */
export function weighted(options: WeightedOptions = {}): Selector {
  const { weights, weightOf, random } = options;
  const weigh = (provider: ProviderRecord): number => {
    const raw = weightOf === undefined ? (weights?.[provider.id] ?? 1) : weightOf(provider);
    return Number.isFinite(raw) && raw > 0 ? raw : 0;
  };

  return (candidates: readonly ProviderRecord[], ctx: CallContext): readonly ProviderRecord[] => {
    if (candidates.length < 2) return candidates;
    const rand = random ?? ctx.runtime.random;
    const pool = candidates.map((provider) => ({ provider, weight: weigh(provider) }));
    const ordered: ProviderRecord[] = [];

    for (;;) {
      const positive = pool.filter((entry) => entry.weight > 0);
      const last = positive.at(-1);
      if (last === undefined) break;

      let picked = last;
      if (positive.length > 1) {
        let total = 0;
        for (const entry of positive) total += entry.weight;
        let r = rand() * total;
        for (const entry of positive) {
          r -= entry.weight;
          if (r < 0) {
            picked = entry;
            break;
          }
        }
        // `picked` stays `last` when float drift leaves `r >= 0` throughout.
      }
      ordered.push(picked.provider);
      pool.splice(pool.indexOf(picked), 1);
      if (positive.length === 1) break;
    }

    for (const entry of pool) ordered.push(entry.provider);
    return ordered;
  };
}
