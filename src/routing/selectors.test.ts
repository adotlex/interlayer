import { describe, expect, it } from 'vitest';
import {
  createFakeRuntime,
  type FakeRuntime,
  fakeProvider,
  scriptedRandom,
  testContext,
} from '../../test/support/index.ts';
import type { CallContext, CallOptions, ProviderRecord } from '../core/types.ts';
import {
  byHints,
  chainSelectors,
  defaultSelector,
  filterByTags,
  inOrder,
  roundRobin,
  sticky,
  weighted,
} from './selectors.ts';

function provider(id: string, tags?: readonly string[]): ProviderRecord {
  return fakeProvider(id, tags === undefined ? {} : { traits: { tags } }).record;
}

function ids(providers: readonly ProviderRecord[]): readonly string[] {
  return providers.map((p) => p.id);
}

const A = provider('a');
const B = provider('b');
const C = provider('c');
const ABC: readonly ProviderRecord[] = [A, B, C];

function ctxFor(hints?: CallOptions, runtime?: FakeRuntime): CallContext {
  return testContext({ hints, runtime }).call;
}

describe('inOrder', () => {
  it('is the identity — it never re-sorts the registry order or a caller pin', () => {
    const ctx = ctxFor();
    expect(ids(inOrder()(ABC, ctx))).toEqual(['a', 'b', 'c']);
    // A pinned order arriving from `byHints` must survive the selector.
    expect(ids(inOrder()([C, A, B], ctx))).toEqual(['c', 'a', 'b']);
  });

  it('is what `defaultSelector` is', () => {
    const ctx = ctxFor();
    expect(ids(defaultSelector(ABC, ctx))).toEqual(ids(inOrder()(ABC, ctx)));
  });

  it('tolerates an empty candidate list', () => {
    expect(inOrder()([], ctxFor())).toEqual([]);
  });
});

describe('filterByTags', () => {
  const eu = provider('eu', ['eu', 'fast']);
  const us = provider('us', ['us', 'fast']);
  const slow = provider('slow', ['eu']);
  const pool: readonly ProviderRecord[] = [eu, us, slow];

  it('keeps only providers carrying ALL required tags', () => {
    expect(ids(filterByTags(['eu', 'fast'])(pool, ctxFor()))).toEqual(['eu']);
  });

  it('reads ctx.hints.tags when given no explicit tags', () => {
    expect(ids(filterByTags()(pool, ctxFor({ tags: ['eu'] })))).toEqual(['eu', 'slow']);
  });

  it('an empty requirement is the identity, never an empty pool', () => {
    expect(ids(filterByTags([])(pool, ctxFor()))).toEqual(['eu', 'us', 'slow']);
    expect(ids(filterByTags()(pool, ctxFor()))).toEqual(['eu', 'us', 'slow']);
  });

  it('drops providers that declare no tags at all', () => {
    expect(ids(filterByTags(['eu'])(ABC, ctxFor()))).toEqual([]);
  });
});

describe('byHints', () => {
  it('restricts to hints.providers AND takes the hint order as authoritative', () => {
    expect(ids(byHints()(ABC, ctxFor({ providers: ['c', 'a'] })))).toEqual(['c', 'a']);
  });

  it('ignores hinted ids that are not candidates', () => {
    expect(ids(byHints()(ABC, ctxFor({ providers: ['zzz', 'b'] })))).toEqual(['b']);
  });

  it('de-duplicates a repeated hint', () => {
    expect(ids(byHints()(ABC, ctxFor({ providers: ['b', 'b', 'a'] })))).toEqual(['b', 'a']);
  });

  it('an empty providers hint selects nothing (an explicit restriction to none)', () => {
    expect(byHints()(ABC, ctxFor({ providers: [] }))).toEqual([]);
  });

  it('applies tags and providers together', () => {
    const eu = provider('eu', ['eu']);
    const us = provider('us', ['us']);
    const pool: readonly ProviderRecord[] = [eu, us];
    expect(ids(byHints()(pool, ctxFor({ tags: ['eu'], providers: ['us', 'eu'] })))).toEqual(['eu']);
  });

  it('is idempotent — applying it twice changes nothing', () => {
    const ctx = ctxFor({ providers: ['c', 'a'] });
    const once = byHints()(ABC, ctx);
    expect(ids(byHints()(once, ctx))).toEqual(ids(once));
  });

  it('is the identity when the call carries no hints', () => {
    expect(ids(byHints()(ABC, ctxFor()))).toEqual(['a', 'b', 'c']);
  });
});

describe('chainSelectors', () => {
  it('applies selectors left to right', () => {
    const eu1 = provider('eu1', ['eu']);
    const eu2 = provider('eu2', ['eu']);
    const us1 = provider('us1', ['us']);
    const chained = chainSelectors(filterByTags(['eu']), roundRobin({ start: 1 }));
    expect(ids(chained([eu1, eu2, us1], ctxFor()))).toEqual(['eu2', 'eu1']);
  });

  it('with no selectors is the identity', () => {
    expect(ids(chainSelectors()(ABC, ctxFor()))).toEqual(['a', 'b', 'c']);
  });
});

describe('roundRobin', () => {
  it('rotates by one per invocation, deterministically, with no clock or PRNG', () => {
    const select = roundRobin();
    const ctx = ctxFor();
    expect(ids(select(ABC, ctx))).toEqual(['a', 'b', 'c']);
    expect(ids(select(ABC, ctx))).toEqual(['b', 'c', 'a']);
    expect(ids(select(ABC, ctx))).toEqual(['c', 'a', 'b']);
    expect(ids(select(ABC, ctx))).toEqual(['a', 'b', 'c']);
  });

  it('honours an explicit start offset', () => {
    expect(ids(roundRobin({ start: 2 })(ABC, ctxFor()))).toEqual(['c', 'a', 'b']);
  });

  it('keeps per-instance state — two selectors never share a counter', () => {
    const first = roundRobin();
    const second = roundRobin();
    const ctx = ctxFor();
    first(ABC, ctx);
    expect(ids(first(ABC, ctx))).toEqual(['b', 'c', 'a']);
    expect(ids(second(ABC, ctx))).toEqual(['a', 'b', 'c']);
  });

  it('does not advance the counter on an empty pool', () => {
    const select = roundRobin();
    expect(select([], ctxFor())).toEqual([]);
    expect(ids(select(ABC, ctxFor()))).toEqual(['a', 'b', 'c']);
  });
});

describe('sticky', () => {
  it('is stable: the same key yields the same order every time', () => {
    const ctx = ctxFor({ stickyKey: 'tenant-42' });
    const first = ids(sticky()(ABC, ctx));
    expect(ids(sticky()(ABC, ctx))).toEqual(first);
    expect(ids(sticky()(ABC, ctx))).toEqual(first);
  });

  it('produces a full ordering, not just a pick', () => {
    const order = sticky()(ABC, ctxFor({ stickyKey: 'k1' }));
    expect(ids(order).slice().sort()).toEqual(['a', 'b', 'c']);
  });

  it('sends different keys to different leaders', () => {
    const leaders = new Set(
      ['k1', 'k2', 'k3', 'k4', 'k5', 'k6', 'k7', 'k8'].map(
        (key) => sticky({ key })(ABC, ctxFor())[0]?.id,
      ),
    );
    expect(leaders.size).toBeGreaterThan(1);
  });

  it('rendezvous hashing: removing a non-leader does not remap the leader', () => {
    const ctx = ctxFor({ stickyKey: 'tenant-42' });
    const full = ids(sticky()(ABC, ctx));
    const leader = full[0];
    const withoutLast = ABC.filter((p) => p.id !== full[2]);
    expect(ids(sticky()(withoutLast, ctx))[0]).toBe(leader);
  });

  it('prefers an explicit key over the hint, and keyOf over both', () => {
    const hinted = ctxFor({ stickyKey: 'from-hint' });
    expect(ids(sticky({ key: 'fixed' })(ABC, hinted))).toEqual(
      ids(sticky({ key: 'fixed' })(ABC, ctxFor())),
    );
    expect(ids(sticky({ keyOf: () => 'fixed' })(ABC, hinted))).toEqual(
      ids(sticky({ key: 'fixed' })(ABC, ctxFor())),
    );
  });

  it('is the identity with no key available', () => {
    expect(ids(sticky()(ABC, ctxFor()))).toEqual(['a', 'b', 'c']);
    expect(ids(sticky({ keyOf: () => undefined })(ABC, ctxFor({ stickyKey: 'x' })))).toEqual([
      'a',
      'b',
      'c',
    ]);
  });
});

describe('weighted', () => {
  it('draws from ctx.runtime.random — never Math.random — so a script pins the order', () => {
    // weights a:1 b:1 c:8, total 10. First draw 0.95 -> 9.5 lands in c.
    // Remaining a:1 b:1, total 2. Second draw 0.1 -> 0.2 lands in a. Then b.
    const runtime = createFakeRuntime({ random: scriptedRandom([0.95, 0.1]) });
    const ctx = ctxFor(undefined, runtime);
    const select = weighted({ weights: { a: 1, b: 1, c: 8 } });
    expect(ids(select(ABC, ctx))).toEqual(['c', 'a', 'b']);
  });

  it('draws exactly k-1 values for k positive-weight candidates', () => {
    const random = scriptedRandom([0.5, 0.5, 0.5, 0.5]);
    const runtime = createFakeRuntime({ random });
    weighted()(ABC, ctxFor(undefined, runtime));
    expect(random.drawn).toBe(2);
  });

  it('is reproducible under a seeded runtime and diverges on a different seed', () => {
    const run = (seed: number): readonly string[] =>
      ids(weighted()(ABC, ctxFor(undefined, createFakeRuntime({ seed }))));
    expect(run(7)).toEqual(run(7));
    const seeds = new Set([run(1).join(), run(2).join(), run(3).join(), run(4).join()]);
    expect(seeds.size).toBeGreaterThan(1);
  });

  it('keeps zero, negative and non-finite weights at the tail rather than dropping them', () => {
    const ctx = ctxFor(undefined, createFakeRuntime({ random: scriptedRandom([0.99]) }));
    const select = weighted({ weights: { a: 0, b: -5, c: Number.NaN } });
    // No positive weights at all: original order, no draws.
    expect(ids(select(ABC, ctx))).toEqual(['a', 'b', 'c']);
    const mixed = weighted({ weights: { a: 0, b: 3, c: 0 } });
    expect(
      ids(mixed(ABC, ctxFor(undefined, createFakeRuntime({ random: scriptedRandom([0.5]) })))),
    ).toEqual(['b', 'a', 'c']);
  });

  it('accepts a weightOf override and an injected random', () => {
    const select = weighted({
      weightOf: (p) => (p.id === 'b' ? 100 : 1),
      random: scriptedRandom([0.5, 0.5]),
    });
    expect(ids(select(ABC, ctxFor()))[0]).toBe('b');
  });

  it('short-circuits pools of 0 or 1 without drawing', () => {
    const random = scriptedRandom([0.5]);
    const ctx = ctxFor(undefined, createFakeRuntime({ random }));
    expect(weighted()([], ctx)).toEqual([]);
    expect(ids(weighted()([A], ctx))).toEqual(['a']);
    expect(random.drawn).toBe(0);
  });

  it('returns every candidate exactly once', () => {
    const order = weighted({ weights: { a: 5, b: 1, c: 2 } })(
      ABC,
      ctxFor(undefined, createFakeRuntime({ seed: 99 })),
    );
    expect(ids(order).slice().sort()).toEqual(['a', 'b', 'c']);
  });
});
