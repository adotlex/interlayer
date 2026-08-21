/**
 * PER-PROVIDER STATE, UNDER PARALLEL LOAD.
 *
 * `layer.ts` makes two different arrangements for the two stateful policies: the
 * breaker keys its own machine on `ctx.provider.id`, and the rate limiter gets a
 * whole separate instance per provider through `perProviderPolicy`. Both exist
 * for one reason — a dead backend must not take a healthy one down with it — and
 * both are only interesting when several calls are in flight against different
 * providers at the same time.
 *
 * The last test in this file FAILS ON PURPOSE. It documents a real defect found
 * by exactly this kind of overlap; see the comment above it. Per the wave rules
 * it is left failing and reported rather than fixed at the source or softened.
 */

import { describe, expect, it } from 'vitest';
import { TransportError } from '../../src/core/errors.ts';
import type { InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { capability, defineContract, defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import { createGate, createLedger, drive, flush, type Ledger } from './harness.ts';

interface ChatRequest {
  readonly prompt: string;
}
interface ChatReply {
  readonly text: string;
  readonly by: string;
}

const ai = defineContract({ chat: capability<ChatRequest, ChatReply>({ idempotent: true }) });

function codeOf(ledger: Ledger, id: string): string {
  const error: unknown = ledger.get(id)?.error;
  if (typeof error === 'object' && error !== null && 'code' in error) {
    return String((error as { readonly code: unknown }).code);
  }
  return `<${id} did not reject>`;
}

function servedBy(ledger: Ledger, id: string): unknown {
  const value: unknown = ledger.get(id)?.value;
  if (typeof value === 'object' && value !== null && 'by' in value) {
    return (value as { readonly by: unknown }).by;
  }
  return undefined;
}

/* ------------------------------------------------------------------ *
 * 1. Breakers are keyed per provider
 * ------------------------------------------------------------------ */

describe('per-provider isolation — circuit breakers', () => {
  it('a burst that trips one provider still lands on the healthy sibling, every call', async () => {
    const runtime = createFakeRuntime();
    const gate = createGate(runtime);
    const ledger = createLedger(runtime);
    let healthyCalls = 0;

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'dead',
          capabilities: {
            // Parked, so every call in the burst is simultaneously past
            // admission and before settlement — the state the generation
            // counter exists for.
            chat: async (input: ChatRequest): Promise<ChatReply> => {
              await gate.park(input.prompt);
              return { text: input.prompt, by: 'dead' };
            },
          },
        }),
        defineProvider(ai, {
          id: 'healthy',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> => {
              healthyCalls++;
              return Promise.resolve({ text: input.prompt, by: 'healthy' });
            },
          },
        }),
      ],
      resilience: {
        retry: false,
        rateLimit: false,
        timeout: false,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 60_000 },
      },
      runtime,
    });

    const transitions: InterlayerEvents['breaker:transition'][] = [];
    layer.on('breaker:transition', (e) => transitions.push(e));

    // Wave 1: four overlapping calls, all admitted against `dead` while its
    // breaker is still closed.
    const wave1 = ['w1', 'w2', 'w3', 'w4'];
    for (const id of wave1) void ledger.watch(id, layer.call('chat', { prompt: id }));
    await flush();
    expect(gate.arrivals).toEqual(wave1);

    for (const id of wave1) gate.fail(id, new TransportError('down'));
    await flush();

    // The trip happened exactly once, on the second failure. The two stragglers
    // settled into a generation that no longer existed and changed nothing.
    expect(transitions.map((e) => [e.key, e.from, e.to])).toEqual([['dead', 'closed', 'open']]);
    // Every one of the four still got an answer, from the sibling.
    expect(ledger.resolved).toEqual(wave1);
    for (const id of wave1) expect(servedBy(ledger, id)).toBe('healthy');
    expect(healthyCalls).toBe(4);

    // Wave 2: `dead` now fails fast and is never entered again; `healthy` is
    // untouched by its neighbour's outage.
    const wave2 = ['x1', 'x2', 'x3', 'x4'];
    for (const id of wave2) void ledger.watch(id, layer.call('chat', { prompt: id }));
    await flush();

    expect(gate.arrivals, 'the open breaker kept the burst off the dead provider').toEqual(wave1);
    expect(ledger.resolved).toEqual([...wave1, ...wave2]);
    for (const id of wave2) expect(servedBy(ledger, id)).toBe('healthy');
    expect(healthyCalls).toBe(8);
    expect(transitions, 'no transition was ever emitted for the healthy key').toHaveLength(1);

    await layer.close();
  });

  it('each call in a parallel burst carries only its own suppressed failure', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'dead',
          capabilities: { chat: (): Promise<ChatReply> => Promise.reject(new TransportError('x')) },
        }),
        defineProvider(ai, {
          id: 'healthy',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> =>
              Promise.resolve({ text: input.prompt, by: 'healthy' }),
          },
        }),
      ],
      resilience: { retry: false, rateLimit: false, timeout: false, breaker: false },
      runtime,
    });

    const ids = ['a', 'b', 'c', 'd', 'e'];
    for (const id of ids) void ledger.watch(id, layer.callWithMeta('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === ids.length);

    expect(ledger.resolved).toEqual(ids);
    for (const id of ids) {
      const value: unknown = ledger.get(id)?.value;
      const meta = value as {
        readonly errors: readonly { readonly code: string; readonly callId: string | undefined }[];
        readonly value: ChatReply;
        readonly attempts: number;
      };
      expect(meta.value.text, `${id} got its own input back`).toBe(id);
      expect(
        meta.errors.map((e) => e.code),
        `${id} carries exactly one failure`,
      ).toEqual(['TRANSPORT']);
      // Every suppressed error belongs to THIS call, not a concurrent one.
      const callIds = new Set(meta.errors.map((e) => e.callId));
      expect(callIds.size).toBe(1);
      expect(meta.attempts, 'attempt counting is per call, never cumulative').toBe(2);
    }

    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Rate limiters are per-provider instances
 * ------------------------------------------------------------------ */

describe('per-provider isolation — rate limiters', () => {
  it('an exhausted bucket on one provider does not throttle another', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);

    const make = (id: string) =>
      defineProvider(ai, {
        id,
        capabilities: {
          chat: (input: ChatRequest): Promise<ChatReply> =>
            Promise.resolve({ text: input.prompt, by: id }),
        },
      });

    const layer = createLayer({
      contract: ai,
      providers: [make('a'), make('b')],
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        rateLimit: { capacity: 1, refillPerSec: 1 },
      },
      runtime,
    });

    // Three calls pinned to `a` (capacity 1) and one pinned to `b`, all started
    // together. `b` must not queue behind `a`'s exhausted bucket.
    for (const id of ['a1', 'a2', 'a3']) {
      void ledger.watch(id, layer.only('a').call('chat', { prompt: id }));
    }
    void ledger.watch('b1', layer.only('b').call('chat', { prompt: 'b1' }));
    await flush();

    expect(ledger.order, 'b1 is served immediately from its own full bucket').toEqual(['a1', 'b1']);
    expect(ledger.at('a1')).toBe(0);
    expect(ledger.at('b1')).toBe(0);

    await drive(runtime, () => ledger.size === 4);
    expect(ledger.order).toEqual(['a1', 'b1', 'a2', 'a3']);
    expect(ledger.at('a2')).toBe(1_000);
    expect(ledger.at('a3')).toBe(2_000);

    await layer.close();
  });

  it('a fallback hop gets the sibling full bucket, not the exhausted one', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);

    const make = (id: string) =>
      defineProvider(ai, {
        id,
        capabilities: {
          chat: (input: ChatRequest): Promise<ChatReply> =>
            Promise.resolve({ text: input.prompt, by: id }),
        },
      });

    const layer = createLayer({
      contract: ai,
      providers: [make('a'), make('b')],
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        // `reject` rather than `wait`, so exhaustion is an immediate error and
        // the fallback hop is observable at an exact instant.
        rateLimit: { capacity: 1, refillPerSec: 1, onExhaustion: 'reject' },
      },
      runtime,
    });

    for (const id of ['c1', 'c2', 'c3']) {
      void ledger.watch(id, layer.call('chat', { prompt: id }));
    }
    await flush();

    expect(ledger.size, 'no waiting: reject mode settles every call at once').toBe(3);
    expect(ledger.settlements.every((s) => s.at === 0)).toBe(true);

    // One call each for `a` and `b` — each bucket granted exactly its own
    // capacity of 1, which is the whole point of one limiter per provider.
    const winners = ledger.settlements.filter((s) => s.ok).map((s) => servedBy(ledger, s.id));
    expect([...winners].sort()).toEqual(['a', 'b']);

    // The third call exhausted both and aggregates BOTH refusals.
    const failed = ledger.rejected;
    expect(failed).toHaveLength(1);
    const only = failed[0] ?? '';
    expect(codeOf(ledger, only)).toBe('ALL_FAILED');
    const error: unknown = ledger.get(only)?.error;
    const failures = (error as { readonly failures: readonly { readonly code: string }[] })
      .failures;
    expect(failures.map((e) => e.code)).toEqual(['RATE_LIMITED', 'RATE_LIMITED']);

    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * 3. KNOWN DEFECT — reported, not fixed, not softened
 * ------------------------------------------------------------------ */

describe('per-provider isolation — fail-fast is not fast', () => {
  /**
   * FAILING ON PURPOSE. Wave-3 rule 2: a failing test documenting a real defect
   * is the deliverable; do not fix `src/**` and do not weaken the assertion.
   *
   * WHAT IS WRONG. `POLICY_ORDER` puts the rate limiter OUTSIDE the breaker:
   *
   *     Retry > RateLimit > CircuitBreaker > AttemptTimeout > call
   *
   * so every call spends a rate-limit token BEFORE the breaker gets to reject
   * it. When the circuit is open no physical call is ever made, yet the token is
   * consumed anyway — which contradicts `rate-limit.ts`'s own stated contract
   * ("ours is a quota limiter counting PHYSICAL calls") and, far worse, destroys
   * fail-fast: with the default `wait` exhaustion mode a burst against an open
   * circuit does not shed load, it QUEUES, and each caller waits for a token it
   * will never use before being told the circuit is open.
   *
   * The damage is not confined to the dead provider either. Because the wait
   * happens inside the fallback chain, a HEALTHY sibling with a completely
   * untouched bucket is delayed by exactly the dead provider's queue time.
   *
   * OBSERVED (this exact fixture, `capacity: 2, refillPerSec: 1`): the three
   * burst calls are served by `healthy` at t = 1_000 / 2_000 / 3_000. With the
   * default limiter — capacity 10, refill 10/s, `maxQueueWaitMs` 30_000 — a
   * burst of 300 calls against an open circuit would take ~29 virtual seconds
   * to shed, every one of them burning quota for a call that never happened.
   *
   * ASSERTED HERE is the correct behaviour: an open circuit sheds load at t=0
   * and the healthy sibling answers at t=0.
   *
   * SUGGESTED FIX (for whoever owns the source): either move the breaker outside
   * the limiter for the admission decision, or have the breaker's fail-fast path
   * refund / never spend the token. Not attempted here.
   */
  it('KNOWN DEFECT - an open circuit does not shed load; it waits for a rate-limit token first', async () => {
    const runtime = createFakeRuntime();
    const ledger = createLedger(runtime);

    const layer = createLayer({
      contract: ai,
      providers: [
        defineProvider(ai, {
          id: 'dead',
          capabilities: {
            chat: (): Promise<ChatReply> => Promise.reject(new TransportError('down')),
          },
        }),
        defineProvider(ai, {
          id: 'healthy',
          capabilities: {
            chat: (input: ChatRequest): Promise<ChatReply> =>
              Promise.resolve({ text: input.prompt, by: 'healthy' }),
          },
        }),
      ],
      resilience: {
        retry: false,
        timeout: false,
        rateLimit: { capacity: 2, refillPerSec: 1 },
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 60_000 },
      },
      runtime,
    });

    // Trip `dead`, spending its two tokens in the process.
    void ledger.watch('t1', layer.only('dead').call('chat', { prompt: 't1' }));
    void ledger.watch('t2', layer.only('dead').call('chat', { prompt: 't2' }));
    await flush();
    expect(ledger.rejected).toEqual(['t1', 't2']);

    // The circuit is open and `dead`'s bucket is empty. A burst arrives.
    const burst = ['x1', 'x2', 'x3'];
    for (const id of burst) void ledger.watch(id, layer.call('chat', { prompt: id }));
    await drive(runtime, () => ledger.size === 5);

    // The sibling does answer — the routing is right. It is the TIMING that is
    // wrong: observed 1_000 / 2_000 / 3_000 instead of 0 / 0 / 0.
    for (const id of burst) expect(servedBy(ledger, id)).toBe('healthy');
    expect(
      burst.map((id) => ledger.at(id)),
      'an open circuit must shed load at once: no physical call exists to meter',
    ).toEqual([0, 0, 0]);

    await layer.close();
  });
});
