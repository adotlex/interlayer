/**
 * HALF-OPEN ADMISSION UNDER PARALLELISM — the probe budget, enforced.
 *
 * `halfOpenMaxConcurrent` is the one breaker setting that is meaningless
 * sequentially: with one call at a time the counter never reaches two, so a
 * suite that never overlaps calls proves nothing about it. Worse,
 * `halfOpenMaxConcurrent > 1` had no end-to-end coverage at all before this
 * file — a half-open breaker admitting more probes than configured is a
 * thundering herd against a backend that has just come back up, which is the
 * failure mode a breaker exists to prevent.
 *
 * Everything here goes through `createLayer`, because the invariant that matters
 * is the assembled one: the check-then-increment has to stay atomic with the
 * router, the fallback chain and the emitter all in the path. Each test starts
 * N calls with NO await between them, which is exactly how a real burst arrives.
 */

import { describe, expect, it } from 'vitest';
import { TransportError } from '../../src/core/errors.ts';
import type { InterlayerEvents } from '../../src/core/types.ts';
import { createLayer } from '../../src/layer.ts';
import { capability, defineContract, defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime, type FakeRuntime } from '../support/index.ts';
import { createGate, createLedger, flush, type Gate, type Ledger } from './harness.ts';

interface ChatRequest {
  readonly prompt: string;
}
interface ChatReply {
  readonly text: string;
  readonly by: string;
}

const ai = defineContract({ chat: capability<ChatRequest, ChatReply>({ idempotent: true }) });

interface BreakerOverrides {
  readonly resetMs: number;
  readonly halfOpenMaxConcurrent?: number | undefined;
  readonly halfOpenSuccessesToClose?: number | undefined;
}

interface Fixture {
  readonly runtime: FakeRuntime;
  readonly gate: Gate;
  readonly ledger: Ledger;
  readonly transitions: readonly InterlayerEvents['breaker:transition'][];
  /** While true the handler throws before reaching the gate. */
  setBroken(value: boolean): void;
  /** Fires one call and records it. Never awaited — that is the point. */
  fire(id: string, signal?: AbortSignal): void;
  close(): Promise<void>;
}

/**
 * One provider, one breaker, no retry, no rate limit and NO TIMEOUTS.
 *
 * The timeouts are off deliberately: with them on there is always a 10 s and a
 * 30 s timer pending, and any clock step large enough to reach the breaker
 * cooldown would fire them first. Off, the only timers in the process are the
 * ones a test arms itself, so every instant asserted below is exact.
 */
function fixture(breaker: BreakerOverrides): Fixture {
  const runtime = createFakeRuntime();
  const gate = createGate(runtime);
  const ledger = createLedger(runtime);
  let broken = true;

  const provider = defineProvider(ai, {
    id: 'p',
    capabilities: {
      chat: async (input: ChatRequest, ctx): Promise<ChatReply> => {
        if (broken) throw new TransportError('down');
        await gate.park(input.prompt, ctx.signal);
        return { text: input.prompt, by: 'p' };
      },
    },
  });

  const layer = createLayer({
    contract: ai,
    providers: [provider],
    resilience: {
      retry: false,
      rateLimit: false,
      timeout: false,
      breaker: {
        minimumThroughput: 2,
        failureRatio: 0.5,
        resetMs: breaker.resetMs,
        ...(breaker.halfOpenMaxConcurrent === undefined
          ? {}
          : { halfOpenMaxConcurrent: breaker.halfOpenMaxConcurrent }),
        ...(breaker.halfOpenSuccessesToClose === undefined
          ? {}
          : { halfOpenSuccessesToClose: breaker.halfOpenSuccessesToClose }),
      },
    },
    runtime,
  });

  const transitions: InterlayerEvents['breaker:transition'][] = [];
  layer.on('breaker:transition', (e) => transitions.push(e));

  return {
    runtime,
    gate,
    ledger,
    transitions,
    setBroken(value: boolean): void {
      broken = value;
    },
    fire(id: string, signal?: AbortSignal): void {
      void ledger.watch(
        id,
        layer.call('chat', { prompt: id }, signal === undefined ? {} : { signal }),
      );
    },
    close: (): Promise<void> => layer.close(),
  };
}

function codeOf(ledger: Ledger, id: string): string {
  const error: unknown = ledger.get(id)?.error;
  if (typeof error === 'object' && error !== null && 'code' in error) {
    return String((error as { readonly code: unknown }).code);
  }
  return `<${id} did not reject>`;
}

/** Trips the breaker with two failures and steps to the end of the cooldown. */
async function tripAndCoolDown(f: Fixture, resetMs: number): Promise<void> {
  f.fire('trip-1');
  f.fire('trip-2');
  await flush();
  expect(f.transitions.map((e) => [e.from, e.to])).toEqual([['closed', 'open']]);
  await f.runtime.advance(resetMs);
  f.setBroken(false);
}

/* ------------------------------------------------------------------ *
 * 1. One probe
 * ------------------------------------------------------------------ */

describe('half-open admission — a burst racing the OPEN -> HALF_OPEN transition', () => {
  it('admits exactly one probe out of eight simultaneous calls', async () => {
    const f = fixture({ resetMs: 5_000, halfOpenMaxConcurrent: 1 });
    await tripAndCoolDown(f, 5_000);

    // Eight calls, no await between them. The first to reach the synchronous
    // admission region ticks the breaker half-open and takes the only slot.
    const ids = ['b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8'];
    for (const id of ids) f.fire(id);
    await flush();

    expect(f.gate.arrivals, 'exactly one probe reached the provider').toEqual(['b1']);
    const shutOut = f.ledger.rejected.filter((id) => ids.includes(id));
    expect(shutOut).toEqual(['b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8']);
    for (const id of shutOut) expect(codeOf(f.ledger, id)).toBe('CIRCUIT_OPEN');
    expect(f.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
    ]);

    // Only one OPEN -> HALF_OPEN event: the transition happened once, not once
    // per call that observed it.
    f.gate.release('b1');
    await flush();
    expect(f.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'closed'],
    ]);
    await f.close();
  });

  it('the released slot of a cancelled probe is reusable and scores neither way', async () => {
    const f = fixture({ resetMs: 5_000, halfOpenMaxConcurrent: 1 });
    await tripAndCoolDown(f, 5_000);

    const hangUp = new AbortController();
    f.fire('probe', hangUp.signal);
    await flush();
    expect(f.gate.arrivals).toEqual(['probe']);

    // The slot really is taken.
    f.fire('crowded-out');
    await flush();
    expect(codeOf(f.ledger, 'crowded-out')).toBe('CIRCUIT_OPEN');

    // The caller withdraws. That is no evidence about the provider: the slot
    // must come back without the outcome being recorded either way.
    hangUp.abort();
    await flush();
    expect(codeOf(f.ledger, 'probe')).toBe('CANCELLED');
    expect(
      f.transitions.map((e) => [e.from, e.to]),
      'a cancellation is not a failed probe — it must not re-open',
    ).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
    ]);

    // And the freed slot is genuinely usable.
    f.fire('second-probe');
    await flush();
    expect(f.gate.arrivals).toEqual(['probe', 'second-probe']);
    f.gate.release('second-probe');
    await flush();
    expect(f.transitions.at(-1)?.to).toBe('closed');
    await f.close();
  });
});

/* ------------------------------------------------------------------ *
 * 2. Half-open concurrency above one — previously untested end to end
 * ------------------------------------------------------------------ */

describe('half-open admission — halfOpenMaxConcurrent > 1', () => {
  it('admits exactly three probes out of eight and fails the rest fast', async () => {
    const f = fixture({ resetMs: 4_000, halfOpenMaxConcurrent: 3, halfOpenSuccessesToClose: 3 });
    await tripAndCoolDown(f, 4_000);

    for (const id of ['b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8']) f.fire(id);
    await flush();

    expect(f.gate.arrivals).toEqual(['b1', 'b2', 'b3']);
    const shutOut = f.ledger.rejected.filter((id) => id.startsWith('b'));
    expect(shutOut).toEqual(['b4', 'b5', 'b6', 'b7', 'b8']);
    for (const id of shutOut) expect(codeOf(f.ledger, id)).toBe('CIRCUIT_OPEN');

    // Three successes are required, and the first two must not close early.
    f.gate.release('b1');
    await flush();
    expect(f.transitions.at(-1)?.to).toBe('half-open');
    f.gate.release('b2');
    await flush();
    expect(f.transitions.at(-1)?.to).toBe('half-open');

    // A ninth call now finds one slot free (two probes settled, one still out)
    // and is admitted — the counter is a live budget, not a one-shot latch.
    f.fire('b9');
    await flush();
    expect(f.gate.arrivals).toEqual(['b1', 'b2', 'b3', 'b9']);

    f.gate.release('b3');
    await flush();
    expect(f.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'closed'],
    ]);
    f.gate.release('b9');
    await flush();
    expect(f.ledger.resolved).toEqual(['b1', 'b2', 'b3', 'b9']);
    await f.close();
  });

  it('a probe failing while a sibling probe succeeds re-opens, and the success is discarded', async () => {
    const f = fixture({ resetMs: 4_000, halfOpenMaxConcurrent: 2, halfOpenSuccessesToClose: 2 });
    await tripAndCoolDown(f, 4_000);

    for (const id of ['p1', 'p2', 'p3']) f.fire(id);
    await flush();
    expect(f.gate.arrivals).toEqual(['p1', 'p2']);
    expect(codeOf(f.ledger, 'p3')).toBe('CIRCUIT_OPEN');

    // Two probes are out against the same provider. One dies; the other lives.
    // Demotion is immediate and unconditional — ANY failed trial re-opens.
    await f.runtime.advance(10);
    f.gate.fail('p1', new TransportError('still down'));
    await flush();
    expect(f.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'open'],
    ]);

    // The surviving probe now belongs to a dead generation. Scored, it would be
    // the second half-open success and would CLOSE a circuit that had just
    // re-opened against a provider still failing.
    f.gate.release('p2');
    await flush();
    expect(f.transitions, 'the stale success must not close the circuit').toHaveLength(3);
    expect(f.ledger.resolved).toEqual(['p2']);

    // The cooldown restarted at the demotion instant, so a call now fails fast.
    f.fire('after');
    await flush();
    expect(codeOf(f.ledger, 'after')).toBe('CIRCUIT_OPEN');
    await f.close();
  });

  it('a success then a failure in the same half-open window re-opens without closing first', async () => {
    const f = fixture({ resetMs: 4_000, halfOpenMaxConcurrent: 2, halfOpenSuccessesToClose: 2 });
    await tripAndCoolDown(f, 4_000);

    f.fire('p1');
    f.fire('p2');
    await flush();
    expect(f.gate.arrivals).toEqual(['p1', 'p2']);

    // One success is not enough to close when two are required.
    f.gate.release('p1');
    await flush();
    expect(f.transitions.at(-1)?.to).toBe('half-open');

    f.gate.fail('p2', new TransportError('down again'));
    await flush();
    expect(f.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'open'],
    ]);
    await f.close();
  });
});
