/**
 * ONE LAYER, MANY CALLS IN FLIGHT — no cross-talk anywhere.
 *
 * A layer is a long-lived object shared by every caller in a process: one
 * registry, one router, one emitter, one instance of each policy. The per-call
 * state — `stats`, `failures`, `state`, the callId, the deadline — is created
 * inside `invoke()` and must stay there. Nothing in the existing suite ever has
 * two calls alive at once, so "must stay there" has never been observed; a call
 * reading another call's `stats.attempts` would look perfectly correct in every
 * sequential test ever written.
 *
 * Overlap here is real and is forced: every burst starts N promises with no
 * await between them, the handlers finish OUT OF ARRIVAL ORDER on the virtual
 * clock, and only then is anything asserted. Finishing out of order is the point
 * — if the calls quietly serialised, the tests would prove nothing, so each one
 * asserts the interleaving it relies on.
 */

import { describe, expect, it } from 'vitest';
import { TransportError } from '../../src/core/errors.ts';
import type { CallContext, InterlayerEventName, InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { capability, defineContract, defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { createLedger, drive, flush } from './harness.ts';

interface ChatRequest {
  readonly prompt: string;
}
interface ChatReply {
  readonly text: string;
  readonly by: string;
}

const ai = defineContract({ chat: capability<ChatRequest, ChatReply>({ idempotent: true }) });

/** The shape `callWithMeta` resolves to, without re-deriving the generics. */
interface Meta {
  readonly value: ChatReply;
  readonly providerId: string;
  readonly attempts: number;
  readonly durationMs: number;
  readonly errors: readonly { readonly code: string; readonly callId: string | undefined }[];
}

function metaOf(value: unknown): Meta {
  return value as Meta;
}

/** Every event, in emission order, with its name — the emitter is shared. */
interface Recorded {
  readonly name: InterlayerEventName;
  readonly callId: string | undefined;
  readonly payload: unknown;
}

function recorderFor(
  on: <K extends InterlayerEventName>(
    event: K,
    fn: (payload: InterlayerEvents[K]) => void,
  ) => unknown,
): readonly Recorded[] {
  const log: Recorded[] = [];
  const names: readonly InterlayerEventName[] = [
    'call:start',
    'call:success',
    'call:failure',
    'provider:selected',
    'attempt:start',
    'attempt:success',
    'attempt:failure',
    'retry:scheduled',
    'fallback:advance',
  ];
  for (const name of names) {
    on(name, (payload: unknown) => {
      const callId =
        typeof payload === 'object' && payload !== null && 'callId' in payload
          ? String((payload as { readonly callId: unknown }).callId)
          : undefined;
      log.push({ name, callId, payload });
    });
  }
  return log;
}

/* ------------------------------------------------------------------ *
 * 1. Results
 * ------------------------------------------------------------------ */

describe('parallel calls on one layer — result isolation', () => {
  it('six overlapping calls finishing out of order each get their own value and duration', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    // Deliberately non-monotonic, so completion order is NOT arrival order.
    const latency: Readonly<Record<string, number>> = {
      p1: 500,
      p2: 100,
      p3: 400,
      p4: 200,
      p5: 300,
      p6: 50,
    };

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: async (input: ChatRequest, ctx): Promise<ChatReply> => {
              await ctx.runtime.sleep(latency[input.prompt] ?? 0, ctx.signal);
              return { text: input.prompt.toUpperCase(), by: 'p' };
            },
          },
        }),
      ],
      resilience: { retry: false, rateLimit: false, timeout: false, breaker: false },
      runtime,
    });

    const ids = ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    // They really did overlap: completion order is by latency, not by arrival.
    expect(ledger.order).toEqual(['p6', 'p2', 'p4', 'p5', 'p3', 'p1']);

    for (const id of ids) {
      const meta = metaOf(ledger.get(id)?.value);
      expect(meta.value.text, `${id} received another call's value`).toBe(id.toUpperCase());
      expect(meta.providerId).toBe('p');
      expect(meta.attempts, `${id} counted another call's attempts`).toBe(1);
      expect(meta.durationMs, `${id} reported another call's duration`).toBe(latency[id]);
      expect(meta.errors).toEqual([]);
    }
    await layer.close();
  });

  it('a mixed burst keeps each winner and each suppressed failure with its own call', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    // Odd prompts fail on `first` and fall through; even prompts succeed there.
    const failsOnFirst = (prompt: string): boolean => prompt.endsWith('1') || prompt.endsWith('3');

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'first',
          capabilities: {
            chat: async (input: ChatRequest, ctx): Promise<ChatReply> => {
              await ctx.runtime.sleep(10, ctx.signal);
              if (failsOnFirst(input.prompt)) throw new TransportError(`no ${input.prompt}`);
              return { text: input.prompt, by: 'first' };
            },
          },
        }),
        defineProvider(ai, {
          id: 'second',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> =>
              Promise.resolve({ text: input.prompt, by: 'second' }),
          },
        }),
      ],
      resilience: { retry: false, rateLimit: false, timeout: false, breaker: false },
      runtime,
    });

    const ids = ['q1', 'q2', 'q3', 'q4'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    for (const id of ids) {
      const meta = metaOf(ledger.get(id)?.value);
      const expected = failsOnFirst(id) ? 'second' : 'first';
      expect(meta.value.by, `${id} was answered by the wrong provider`).toBe(expected);
      expect(meta.value.text).toBe(id);
      expect(meta.errors.map((e) => e.code)).toEqual(failsOnFirst(id) ? ['TRANSPORT'] : []);
      // A suppressed failure is stamped with the callId of ITS OWN call.
      for (const e of meta.errors) expect(e.callId).toBeDefined();
      expect(meta.attempts).toBe(failsOnFirst(id) ? 2 : 1);
    }

    // Each call's suppressed error is a distinct object with a distinct callId.
    const errorCallIds = ids
      .flatMap((id) => metaOf(ledger.get(id)?.value).errors)
      .map((e) => e.callId);
    expect(new Set(errorCallIds).size).toBe(errorCallIds.length);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. ctx.stats and the retry loop
 * ------------------------------------------------------------------ */

describe('parallel calls on one layer — stats and retry schedules', () => {
  it('attempt counts stay per call when only some of the burst retries', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    const seen = new Map<string, number>();
    const flaky = new Set(['r1', 'r2']);
    let physicalCalls = 0;

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> => {
              physicalCalls++;
              const n = (seen.get(input.prompt) ?? 0) + 1;
              seen.set(input.prompt, n);
              if (flaky.has(input.prompt) && n === 1) {
                return Promise.reject(new TransportError('first try fails'));
              }
              return Promise.resolve({ text: input.prompt, by: 'p' });
            },
          },
        }),
      ],
      resilience: {
        rateLimit: false,
        timeout: false,
        breaker: false,
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 100 },
      },
      runtime,
    });

    const ids = ['r1', 's1', 'r2', 's2'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    expect(physicalCalls, 'six physical calls in total across the burst').toBe(6);
    // If `stats` were shared, every call would report 6.
    expect(ids.map((id) => metaOf(ledger.get(id)?.value).attempts)).toEqual([2, 1, 2, 1]);
    expect(ids.map((id) => metaOf(ledger.get(id)?.value).durationMs)).toEqual([100, 0, 100, 0]);
    expect(ledger.order, 'the clean calls finish first; the retriers wait out the backoff').toEqual(
      ['s1', 's2', 'r1', 'r2'],
    );
    await layer.close();
  });

  it('four simultaneously retrying calls each keep their own backoff schedule', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    const seen = new Map<string, number>();

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> => {
              const n = (seen.get(input.prompt) ?? 0) + 1;
              seen.set(input.prompt, n);
              if (n < 3) return Promise.reject(new TransportError(`try ${String(n)}`));
              return Promise.resolve({ text: input.prompt, by: 'p' });
            },
          },
        }),
      ],
      resilience: {
        rateLimit: false,
        timeout: false,
        breaker: false,
        // `fixed` rather than the jittered default, so the instants are exact.
        retry: { maxAttempts: 3, strategy: 'fixed', baseDelayMs: 100 },
      },
      runtime,
    });

    const scheduled: InterlayerEvents['retry:scheduled'][] = [];
    layer.on('retry:scheduled', (e) => scheduled.push(e));

    const ids = ['t1', 't2', 't3', 't4'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    expect(ledger.resolved).toHaveLength(4);
    for (const id of ids) {
      const meta = metaOf(ledger.get(id)?.value);
      expect(meta.attempts).toBe(3);
      expect(meta.durationMs, 'two backoffs of 100ms each, not eight').toBe(200);
    }

    // Eight schedules: two per call, never one shared timer for the burst.
    expect(scheduled).toHaveLength(8);
    const byCall = new Map<string, number[]>();
    for (const e of scheduled) byCall.set(e.callId, [...(byCall.get(e.callId) ?? []), e.attempt]);
    expect(byCall.size, 'four distinct callIds scheduled retries').toBe(4);
    // `attempt` on the event is the UPCOMING try, so a 3-attempt call schedules
    // 2 and then 3 — and each call has its own pair, not a shared counter.
    for (const attempts of byCall.values()) expect(attempts).toEqual([2, 3]);
    expect(scheduled.every((e) => e.delayMs === 100)).toBe(true);
    expect(runtime.pendingTimers, 'every backoff timer was cleaned up').toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. The shared emitter and the per-call context
 * ------------------------------------------------------------------ */

describe('parallel calls on one layer — events and per-call context', () => {
  it('every event payload belongs to exactly one call, and each call is complete', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    const failsOnFirst = new Set(['e2', 'e4']);

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'first',
          capabilities: {
            chat: async (input: ChatRequest, ctx): Promise<ChatReply> => {
              await ctx.runtime.sleep(20, ctx.signal);
              if (failsOnFirst.has(input.prompt)) throw new TransportError('nope');
              return { text: input.prompt, by: 'first' };
            },
          },
        }),
        defineProvider(ai, {
          id: 'second',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> =>
              Promise.resolve({ text: input.prompt, by: 'second' }),
          },
        }),
      ],
      resilience: { retry: false, rateLimit: false, timeout: false, breaker: false },
      runtime,
    });

    const log = recorderFor(layer.on.bind(layer));

    const ids = ['e1', 'e2', 'e3', 'e4'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    // Every recorded event carries a callId, and there are exactly four of them.
    expect(log.every((r) => r.callId !== undefined)).toBe(true);
    const callIds = [...new Set(log.map((r) => r.callId))];
    expect(callIds).toHaveLength(4);

    for (const callId of callIds) {
      const mine = log.filter((r) => r.callId === callId);
      const names = mine.map((r) => r.name);
      expect(names.filter((n) => n === 'call:start')).toHaveLength(1);
      expect(names.filter((n) => n === 'call:success')).toHaveLength(1);
      expect(names.filter((n) => n === 'call:failure')).toHaveLength(0);

      // One `attempt:start` per physical call, and the outcomes balance it.
      const starts = names.filter((n) => n === 'attempt:start').length;
      const outcomes = names.filter(
        (n) => n === 'attempt:success' || n === 'attempt:failure',
      ).length;
      expect(outcomes, `call ${callId} has an unbalanced attempt ledger`).toBe(starts);

      // The fallback hop, if any, belongs to this call and no other.
      const advanced = mine.filter((r) => r.name === 'fallback:advance');
      expect(advanced.length === 0 || advanced.length === 1).toBe(true);
      expect(starts).toBe(advanced.length + 1);
    }

    // Two of the four fell back, matching the two scripted failures — the
    // events are not a smeared sum over the burst.
    expect(log.filter((r) => r.name === 'fallback:advance')).toHaveLength(2);
    expect(log.filter((r) => r.name === 'attempt:failure')).toHaveLength(2);
    await layer.close();
  });

  it('user middleware sees a private ctx.state and its own input on every concurrent call', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);
    const observed: { readonly callId: string; readonly mine: unknown; readonly size: number }[] =
      [];
    const maps = new Set<CallContext['state']>();

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'p',
          capabilities: {
            chat: async (input: ChatRequest, ctx): Promise<ChatReply> => {
              // Out-of-order completion, so the middleware frames interleave.
              await ctx.runtime.sleep(input.prompt === 'm1' ? 300 : 100, ctx.signal);
              return { text: input.prompt, by: 'p' };
            },
          },
        }),
      ],
      resilience: { retry: false, rateLimit: false, timeout: false, breaker: false },
      runtime,
    });

    layer.use(async (ctx, next) => {
      const prompt = (ctx.input as ChatRequest).prompt;
      maps.add(ctx.state);
      ctx.state.set('prompt', prompt);
      const value = await next();
      observed.push({ callId: ctx.callId, mine: ctx.state.get('prompt'), size: ctx.state.size });
      return value;
    });

    const ids = ['m1', 'm2', 'm3'];
    for (const id of ids) void ledger.watch(id, layer.call('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    expect(ledger.order, 'the middleware frames really did interleave').toEqual(['m2', 'm3', 'm1']);
    expect(maps.size, 'each call gets its OWN state map, never a shared one').toBe(3);
    expect(observed).toHaveLength(3);
    expect(new Set(observed.map((o) => o.callId)).size).toBe(3);
    for (const o of observed) {
      expect(o.size, 'one entry: no other call wrote into this map').toBe(1);
      expect(typeof o.mine).toBe('string');
    }
    expect([...observed].map((o) => o.mine).sort()).toEqual(['m1', 'm2', 'm3']);

    // A second, later call must not see the earlier calls' state.
    await flush();
    void ledger.watch('late', layer.call('chat', { prompt: 'late' }));
    await drive(runtime, () => ledger.size === 4);
    expect(observed.at(-1)?.size).toBe(1);
    expect(observed.at(-1)?.mine).toBe('late');
    await layer.close();
  });
});
