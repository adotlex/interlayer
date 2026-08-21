/**
 * Router tests.
 *
 * Every `Policy` here is HAND-BUILT from the `Policy` interface — `{ kind,
 * name, scope, execute }`. This unit must not import `src/resilience/**`, so
 * "a retry" below is a two-line fake that calls `next()` twice, not U2's retry.
 * Integration with the real policies is wired post-wave in `src/layer.ts`.
 */

import { describe, expect, it } from 'vitest';
import { fakeProvider, testContext } from '../../test/support/index.ts';
import {
  AllProvidersFailedError,
  hasCode,
  TransportError,
  ValidationError,
} from '../core/errors.ts';
import { type AttemptPolicy, definePolicy, type PolicyKind } from '../core/policy.ts';
import type { AttemptContext, Next, ProviderRecord } from '../core/types.ts';
import { createRouter } from './router.ts';
import { roundRobin, sticky } from './selectors.ts';

function provider(id: string, tags?: readonly string[]): ProviderRecord {
  return fakeProvider(id, tags === undefined ? {} : { traits: { tags } }).record;
}

const A = provider('a');
const B = provider('b');
const C = provider('c');
const ABC: readonly ProviderRecord[] = [A, B, C];

/** A policy that appends its name to `trace` on the way in and on the way out. */
function tracer(kind: PolicyKind, name: string, trace: string[]): AttemptPolicy {
  return definePolicy<AttemptContext, unknown>({
    kind,
    name,
    scope: 'attempt',
    execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) => {
      trace.push(`>${name}`);
      try {
        return await next(ctx);
      } finally {
        trace.push(`<${name}`);
      }
    },
  });
}

/** A re-entrant policy: calls `next()` up to `times`, sequentially. */
function repeater(times: number, name = 'repeat'): AttemptPolicy {
  return definePolicy<AttemptContext, unknown>({
    kind: 'retry',
    name,
    scope: 'attempt',
    execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) => {
      let last: unknown;
      for (let i = 1; i <= times; i++) {
        try {
          return await next({ ...ctx, attempt: i });
        } catch (e) {
          last = e;
        }
      }
      throw last;
    },
  });
}

function attemptOf(script: Readonly<Record<string, unknown>>): {
  run: (p: ProviderRecord) => Promise<unknown>;
  readonly entered: readonly string[];
} {
  const entered: string[] = [];
  return {
    entered,
    run: async (p: ProviderRecord): Promise<unknown> => {
      entered.push(p.id);
      const outcome = script[p.id];
      if (outcome instanceof Error) throw outcome;
      return outcome;
    },
  };
}

describe('createRouter — the attempt stack', () => {
  it('composes the policies it is handed and runs them per candidate', async () => {
    const trace: string[] = [];
    const router = createRouter({ policies: [tracer('custom', 'outer', trace)] });
    const h = testContext();
    const s = attemptOf({ a: new TransportError('a down'), b: 'from-b' });

    await expect(router.routeWith(h.call, ABC, s.run)).resolves.toBe('from-b');
    expect(trace).toEqual(['>outer', '<outer', '>outer', '<outer']);
    expect(s.entered).toEqual(['a', 'b']);
  });

  it('orders policies canonically regardless of the order they are supplied in', async () => {
    const trace: string[] = [];
    const router = createRouter({
      policies: [
        tracer('attempt-timeout', 'timeout', trace),
        tracer('retry', 'retry', trace),
        tracer('circuit-breaker', 'breaker', trace),
        tracer('rate-limit', 'limiter', trace),
      ],
    });
    const h = testContext();

    await router.routeWith(h.call, [A], attemptOf({ a: 'ok' }).run);

    // POLICY_ORDER: retry -> circuit-breaker -> rate-limit -> attempt-timeout.
    // The breaker is OUTSIDE the limiter so an open circuit sheds load at once
    // instead of buying a token it will never use — see POLICY_ORDER.
    expect(trace).toEqual([
      '>retry',
      '>breaker',
      '>limiter',
      '>timeout',
      '<timeout',
      '<limiter',
      '<breaker',
      '<retry',
    ]);
  });

  it('ignores call-scope policies — they belong to the outer stack', async () => {
    const trace: string[] = [];
    const callScoped = definePolicy<AttemptContext, unknown>({
      kind: 'total-timeout',
      name: 'total',
      scope: 'call',
      execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) => {
        trace.push('>total');
        return await next(ctx);
      },
    });
    const router = createRouter({ policies: [callScoped, tracer('custom', 'inner', trace)] });
    const h = testContext();

    await router.routeWith(h.call, [A], attemptOf({ a: 'ok' }).run);

    expect(trace).toEqual(['>inner', '<inner']);
  });

  it('runs with no policies at all', async () => {
    const router = createRouter();
    const h = testContext();
    await expect(router.routeWith(h.call, [A], attemptOf({ a: 'ok' }).run)).resolves.toBe('ok');
    expect(router.policies).toEqual([]);
  });

  it('hands the terminal the context the policies produced, not the original', async () => {
    const marker = new AbortController();
    const narrow = definePolicy<AttemptContext, unknown>({
      kind: 'attempt-timeout',
      name: 'narrow',
      scope: 'attempt',
      execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) =>
        await next({ ...ctx, signal: marker.signal, attempt: 7 }),
    });
    const router = createRouter({ policies: [narrow] });
    const h = testContext();
    const seen: AttemptContext[] = [];

    await router.routeWith(h.call, [A], async (_p: ProviderRecord, ctx: AttemptContext) => {
      seen.push(ctx);
      return 'ok';
    });

    expect(seen[0]?.signal).toBe(marker.signal);
    expect(seen[0]?.attempt).toBe(7);
    expect(seen[0]?.provider.id).toBe('a');
  });

  it('starts every candidate at attempt 1 and carries the provider on the context', async () => {
    const router = createRouter();
    const h = testContext();
    const seen: Array<{ id: string; attempt: number }> = [];

    await router
      .routeWith(h.call, ABC, async (_p: ProviderRecord, ctx: AttemptContext) => {
        seen.push({ id: ctx.provider.id, attempt: ctx.attempt });
        throw new TransportError('down');
      })
      .catch(() => undefined);

    expect(seen).toEqual([
      { id: 'a', attempt: 1 },
      { id: 'b', attempt: 1 },
      { id: 'c', attempt: 1 },
    ]);
  });
});

describe('createRouter — stats', () => {
  it('counts physical handler invocations at the terminal, re-entry included', async () => {
    const router = createRouter({ policies: [repeater(3)] });
    const h = testContext();
    const s = attemptOf({ a: new TransportError('a down'), b: 'from-b' });

    await expect(router.routeWith(h.call, ABC, s.run)).resolves.toBe('from-b');

    // a: three physical tries (the repeater), b: one.
    expect(s.entered).toEqual(['a', 'a', 'a', 'b']);
    expect(h.stats.attempts).toBe(4);
    expect(h.stats.providersTried).toBe(2);
    expect(h.stats.winnerId).toBe('b');
  });

  it('shares stats by reference through the derived attempt contexts', async () => {
    const router = createRouter({ policies: [repeater(2)] });
    const h = testContext();
    await router
      .routeWith(h.call, [A], attemptOf({ a: new TransportError('down') }).run)
      .catch(() => undefined);
    expect(h.stats.attempts).toBe(2);
  });
});

describe('createRouter — selection', () => {
  it('uses the supplied selector', async () => {
    const router = createRouter({ selector: roundRobin({ start: 1 }) });
    const h = testContext();
    const s = attemptOf({ a: 'from-a', b: 'from-b', c: 'from-c' });
    await expect(router.routeWith(h.call, ABC, s.run)).resolves.toBe('from-b');
  });

  it('honours hints.providers ahead of the selector, in the hinted order', async () => {
    const router = createRouter();
    const h = testContext({ hints: { providers: ['c', 'b'] } });
    const s = attemptOf({ a: 'from-a', b: 'from-b', c: new TransportError('c down') });
    await expect(router.routeWith(h.call, ABC, s.run)).resolves.toBe('from-b');
    expect(s.entered).toEqual(['c', 'b']);
  });

  it('honours hints.tags', async () => {
    const eu = provider('eu', ['eu']);
    const us = provider('us', ['us']);
    const router = createRouter();
    const h = testContext({ hints: { tags: ['eu'] } });
    const s = attemptOf({ eu: 'from-eu', us: 'from-us' });
    await expect(router.routeWith(h.call, [us, eu], s.run)).resolves.toBe('from-eu');
    expect(s.entered).toEqual(['eu']);
  });

  it('is deterministic under a sticky hint', async () => {
    const router = createRouter({ selector: sticky() });
    const first = testContext({ hints: { stickyKey: 'tenant-1' } });
    const second = testContext({ hints: { stickyKey: 'tenant-1' } });
    const s1 = attemptOf({ a: 'a', b: 'b', c: 'c' });
    const s2 = attemptOf({ a: 'a', b: 'b', c: 'c' });
    const [r1, r2] = await Promise.all([
      router.routeWith(first.call, ABC, s1.run),
      router.routeWith(second.call, ABC, s2.run),
    ]);
    expect(r1).toBe(r2);
  });

  it('throws NoProviderError for zero candidates', async () => {
    const router = createRouter();
    const h = testContext({ capability: 'chat' });
    const thrown = await router.routeWith(h.call, [], attemptOf({}).run).catch((e: unknown) => e);
    expect(hasCode(thrown, 'NO_PROVIDER')).toBe(true);
  });

  it('throws NoProviderError when the hints filter every candidate out', async () => {
    const router = createRouter();
    const h = testContext({ hints: { providers: ['nobody'] } });
    const s = attemptOf({ a: 'from-a' });
    const thrown = await router.routeWith(h.call, ABC, s.run).catch((e: unknown) => e);
    expect(hasCode(thrown, 'NO_PROVIDER')).toBe(true);
    expect(s.entered).toEqual([]);
  });
});

describe('createRouter — aggregation through the full stack', () => {
  it('surfaces a SINGLE provider failure unwrapped, even with policies installed', async () => {
    const router = createRouter({ policies: [repeater(2), tracer('custom', 'x', [])] });
    const h = testContext();
    const s = attemptOf({ a: new TransportError('only one down') });

    const thrown = await router.routeWith(h.call, [A], s.run).catch((e: unknown) => e);

    expect(thrown).not.toBeInstanceOf(AllProvidersFailedError);
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect((thrown as TransportError).message).toBe('only one down');
  });

  it('aggregates at two or more provider failures', async () => {
    const router = createRouter();
    const h = testContext({ capability: 'chat' });
    const s = attemptOf({
      a: new TransportError('a down'),
      b: new TransportError('b down'),
      c: new TransportError('c down'),
    });

    const thrown = await router.routeWith(h.call, ABC, s.run).catch((e: unknown) => e);

    expect(hasCode(thrown, 'ALL_FAILED')).toBe(true);
    expect((thrown as AllProvidersFailedError).failures).toHaveLength(3);
    expect((thrown as AllProvidersFailedError).triedProviderIds).toEqual(['a', 'b', 'c']);
  });

  it('respects maxProviders', async () => {
    const router = createRouter({ maxProviders: 2 });
    const h = testContext();
    const s = attemptOf({
      a: new TransportError('a'),
      b: new TransportError('b'),
      c: new TransportError('c'),
    });
    const thrown = await router.routeWith(h.call, ABC, s.run).catch((e: unknown) => e);
    expect(s.entered).toEqual(['a', 'b']);
    expect((thrown as AllProvidersFailedError).failures).toHaveLength(2);
  });

  it('respects a custom shouldFallback', async () => {
    const router = createRouter({ shouldFallback: (): boolean => false });
    const h = testContext();
    const s = attemptOf({ a: new TransportError('a down'), b: 'from-b' });
    const thrown = await router.routeWith(h.call, ABC, s.run).catch((e: unknown) => e);
    expect(hasCode(thrown, 'TRANSPORT')).toBe(true);
    expect(s.entered).toEqual(['a']);
  });

  it('stops on a non-retryable failure without trying the rest', async () => {
    const router = createRouter();
    const h = testContext();
    const s = attemptOf({ a: new ValidationError('bad', []), b: 'from-b' });
    const thrown = await router.routeWith(h.call, ABC, s.run).catch((e: unknown) => e);
    expect(hasCode(thrown, 'VALIDATION')).toBe(true);
    expect(s.entered).toEqual(['a']);
  });
});

describe('createRouter — the core Router interface', () => {
  it('route() satisfies `Router`, handing the callback just the provider', async () => {
    const router = createRouter();
    const h = testContext();
    const seen: string[] = [];
    const value = await router.route(h.call, ABC, async (p: ProviderRecord) => {
      seen.push(p.id);
      return `from-${p.id}`;
    });
    expect(value).toBe('from-a');
    expect(seen).toEqual(['a']);
  });

  it('route() walks the chain exactly as routeWith does', async () => {
    const router = createRouter();
    const h = testContext();
    const s = attemptOf({ a: new TransportError('a down'), b: 'from-b' });
    await expect(router.route(h.call, ABC, s.run)).resolves.toBe('from-b');
    expect(h.emitted('fallback:advance')).toHaveLength(1);
  });
});

describe('createRouter — dispose', () => {
  it('disposes every policy that declares a teardown, and never rejects', async () => {
    const disposed: string[] = [];
    const withTeardown = (name: string): AttemptPolicy =>
      definePolicy<AttemptContext, unknown>({
        kind: 'custom',
        name,
        scope: 'attempt',
        execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) =>
          await next(ctx),
        dispose: (): void => {
          disposed.push(name);
        },
      });
    const exploding = definePolicy<AttemptContext, unknown>({
      kind: 'custom',
      name: 'boom',
      scope: 'attempt',
      execute: async (ctx: AttemptContext, next: Next<AttemptContext, unknown>) => await next(ctx),
      dispose: (): void => {
        throw new Error('teardown failed');
      },
    });

    const router = createRouter({
      policies: [withTeardown('one'), exploding, withTeardown('two')],
    });
    await expect(router.dispose()).resolves.toBeUndefined();
    expect(disposed).toEqual(['one', 'two']);
  });
});
