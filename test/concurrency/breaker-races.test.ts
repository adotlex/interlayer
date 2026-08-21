/**
 * THE BREAKER UNDER REAL CONCURRENCY — N calls in flight across a transition.
 *
 * Every other breaker test in this repository settles one call before starting
 * the next, so the machine is never observed with several calls simultaneously
 * past admission and before settlement. That is the only window in which the
 * generation counter does anything at all, and it is the window in which real
 * breakers break: a slow call admitted in one state settling in another.
 *
 * These tests drive the POLICY, not the reducer. `state.test.ts` already proves
 * the reducer from a table of transitions, single-threaded by construction;
 * what is unproven is that `breaker.ts` captures the generation at admission,
 * hands the same one back at settlement, and therefore discards a settlement
 * whose machine no longer exists. Nothing but genuinely overlapped promises can
 * show that.
 *
 * Interleaving is produced by parking each call on a {@link createGate} and
 * releasing them out of order — never by `runAll()`, never by real time.
 */

import { describe, expect, it } from 'vitest';
import { CancelledError, TransportError } from '../../src/core/errors.ts';
import type { Policy } from '../../src/core/policy.ts';
import type { AttemptContext, InterlayerEvents } from '../../src/core/types.ts';
import {
  type BreakerData,
  circuitBreaker,
  reduceBreaker,
  resolveBreakerOptions,
} from '../../src/resilience/circuit-breaker/index.ts';
import {
  createFakeRuntime,
  fakeProvider,
  type TestContextHandle,
  testContext,
} from '../support/index.ts';
import { breakerView, createGate, createLedger, flush, type Gate, type Ledger } from './harness.ts';

/* ------------------------------------------------------------------ *
 * Fixtures
 * ------------------------------------------------------------------ */

const KEY = 'p';

interface Harness {
  readonly h: TestContextHandle;
  readonly gate: Gate;
  readonly ledger: Ledger;
  readonly policy: Policy<AttemptContext, unknown>;
  /**
   * Starts one call through the breaker WITHOUT awaiting it. The handler parks
   * on the gate, so the call sits past admission and before settlement until the
   * test says otherwise.
   */
  start(id: string): void;
  /** Starts a call whose handler settles immediately — for probing admission. */
  startImmediate(id: string, outcome: 'ok' | 'fail'): void;
  readonly transitions: readonly InterlayerEvents['breaker:transition'][];
  view(): ReturnType<typeof breakerView>;
}

function harness(options: Parameters<typeof circuitBreaker>[0]): Harness {
  const runtime = createFakeRuntime();
  const h = testContext({ runtime, provider: fakeProvider(KEY).alwaysSucceed('unused') });
  const gate = createGate(runtime);
  const ledger = createLedger(runtime);
  const policy = circuitBreaker(options);
  const transitions: InterlayerEvents['breaker:transition'][] = [];
  h.events.on('breaker:transition', (e) => transitions.push(e));

  return {
    h,
    gate,
    ledger,
    policy,
    transitions,
    start(id: string): void {
      void ledger.watch(
        id,
        policy.execute(h.attempt, () => gate.park(id)),
      );
    },
    startImmediate(id: string, outcome: 'ok' | 'fail'): void {
      void ledger.watch(
        id,
        policy.execute(h.attempt, () =>
          outcome === 'ok' ? Promise.resolve(id) : Promise.reject(new TransportError(id)),
        ),
      );
    },
    view: () => breakerView(policy.describe?.bind(policy), KEY),
  };
}

/** The `code` of a rejection recorded in the ledger. */
function codeOf(ledger: Ledger, id: string): string {
  const settled = ledger.get(id);
  const error: unknown = settled?.error;
  if (typeof error === 'object' && error !== null && 'code' in error) {
    return String((error as { readonly code: unknown }).code);
  }
  return `<no error for ${id}>`;
}

/* ------------------------------------------------------------------ *
 * 1. Racing the trip itself
 * ------------------------------------------------------------------ */

describe('breaker — N calls in flight when the circuit trips', () => {
  it('admits every concurrent call while closed and trips on exactly the threshold failure', async () => {
    const t = harness({ minimumThroughput: 3, failureRatio: 0.5, resetMs: 10_000 });

    // Five calls enter the breaker back to back with no await between them.
    // Admission is synchronous by contract (R4 §3.7), so all five are inside.
    for (const id of ['c1', 'c2', 'c3', 'c4', 'c5']) t.start(id);
    await flush();

    expect(t.gate.arrivals).toEqual(['c1', 'c2', 'c3', 'c4', 'c5']);
    expect(t.ledger.size, 'nothing has settled yet — all five are in flight').toBe(0);
    expect(t.view()?.state).toBe('closed');
    expect(t.view()?.generation).toBe(0);

    // Settle them one at a time. The window reaches minimumThroughput on the
    // third, and 3/3 >= 0.5 trips it.
    t.gate.fail('c1', new TransportError('1'));
    await flush();
    expect(t.transitions).toHaveLength(0);

    t.gate.fail('c2', new TransportError('2'));
    await flush();
    expect(t.transitions, 'total 2 < minimumThroughput 3').toHaveLength(0);

    t.gate.fail('c3', new TransportError('3'));
    await flush();
    expect(t.transitions.map((e) => [e.from, e.to])).toEqual([['closed', 'open']]);
    expect(t.transitions[0]?.failures).toBe(3);
    expect(t.view()?.generation).toBe(1);

    // c4 and c5 were admitted at generation 0 and settle into generation 1.
    // Their outcomes describe a machine that no longer exists.
    t.gate.fail('c4', new TransportError('4'));
    t.gate.fail('c5', new TransportError('5'));
    await flush();

    expect(
      t.transitions,
      'the late failures must not re-trip an already open breaker',
    ).toHaveLength(1);
    expect(t.view()?.state).toBe('open');
    expect(t.view()?.generation).toBe(1);
    expect(t.ledger.rejected).toEqual(['c1', 'c2', 'c3', 'c4', 'c5']);
  });

  it('late settlements never push `openedAt` forward and so never extend the cooldown', async () => {
    const t = harness({ minimumThroughput: 2, failureRatio: 0.5, resetMs: 10_000 });

    for (const id of ['a', 'b', 'c']) t.start(id);
    await flush();

    t.gate.fail('a', new TransportError('a'));
    t.gate.fail('b', new TransportError('b'));
    await flush();
    const trippedAt = t.view()?.openedAt;
    expect(trippedAt).toBe(0);
    expect(t.view()?.halfOpenAt).toBe(10_000);

    // The straggler settles 7 virtual seconds later. If its failure were
    // recorded, `openedAt` would move to 7_000 and the cooldown would silently
    // stretch to 17_000 — an outage extended by a call that had already lost.
    await t.h.runtime.advance(7_000);
    t.gate.fail('c', new TransportError('c'));
    await flush();

    expect(t.view()?.openedAt).toBe(0);
    expect(t.view()?.halfOpenAt).toBe(10_000);
    expect(t.transitions).toHaveLength(1);
  });

  it('a success in flight when the circuit trips cannot un-trip it', async () => {
    // `consecutive` with a threshold of 1 makes every recorded outcome visible:
    // one recorded failure re-opens, and a recorded success zeroes the counter.
    const t = harness({ mode: 'consecutive', consecutiveFailureThreshold: 1, resetMs: 5_000 });

    t.start('winner');
    t.start('loser');
    await flush();

    t.gate.fail('loser', new TransportError('down'));
    await flush();
    expect(t.transitions.map((e) => [e.from, e.to])).toEqual([['closed', 'open']]);
    expect(t.view()?.generation).toBe(1);

    // The success belongs to generation 0. Recording it would clear
    // `consecutiveFailures`, and in the half-open state it would CLOSE the
    // circuit outright.
    t.gate.release('winner', 'ok');
    await flush();

    expect(t.view()?.state).toBe('open');
    expect(t.view()?.generation).toBe(1);
    expect(t.transitions).toHaveLength(1);
    expect(t.ledger.resolved).toEqual(['winner']);
  });
});

/* ------------------------------------------------------------------ *
 * 2. The generation counter, proved
 * ------------------------------------------------------------------ */

describe('breaker — generation guard across a real transition', () => {
  it('a stale success settling in half-open does NOT close the circuit or free the probe slot', async () => {
    const t = harness({
      minimumThroughput: 2,
      failureRatio: 0.5,
      resetMs: 5_000,
      halfOpenMaxConcurrent: 1,
    });

    // Three calls admitted while closed, at generation 0.
    for (const id of ['a', 'b', 'straggler']) t.start(id);
    await flush();
    expect(t.view()?.generation).toBe(0);

    t.gate.fail('a', new TransportError('a'));
    t.gate.fail('b', new TransportError('b'));
    await flush();
    expect(t.view()?.state).toBe('open'); // generation 1

    // Cooldown elapses; the next arrival ticks OPEN -> HALF-OPEN (generation 2)
    // and takes the single probe slot.
    await t.h.runtime.advance(5_000);
    t.start('probe');
    await flush();

    expect(t.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
    ]);
    expect(t.view()?.state).toBe('half-open');
    expect(t.view()?.generation).toBe(2);
    expect(t.view()?.halfOpenInFlight).toBe(1);

    // THE RACE. `straggler` was admitted two generations ago and succeeds now.
    // Scored, it would count as the half-open success that closes the circuit —
    // a dead provider declared healthy by a call that predates the outage.
    t.gate.release('straggler', 'stale-ok');
    await flush();

    expect(t.view()?.state, 'the stale success must not close the circuit').toBe('half-open');
    expect(t.view()?.halfOpenSuccesses).toBe(0);
    expect(t.view()?.halfOpenInFlight, 'and must not release a probe slot it never took').toBe(1);
    expect(t.transitions).toHaveLength(2);

    // NON-VACUITY. Same machine, same event, only the generation differs. The
    // pure reducer says a CURRENT success closes and a stale one changes
    // nothing, so the assertions above are the guard doing work rather than an
    // accident of how the promises happened to be scheduled.
    const options = resolveBreakerOptions({
      minimumThroughput: 2,
      failureRatio: 0.5,
      resetMs: 5_000,
      halfOpenMaxConcurrent: 1,
    });
    const probing: BreakerData = {
      state: 'half-open',
      openedAt: 0,
      buckets: [],
      halfOpenInFlight: 1,
      halfOpenSuccesses: 0,
      consecutiveFailures: 0,
      generation: 2,
    };
    expect(reduceBreaker(probing, { type: 'success', generation: 2 }, 5_000, options).state).toBe(
      'closed',
    );
    expect(reduceBreaker(probing, { type: 'success', generation: 0 }, 5_000, options).state).toBe(
      'half-open',
    );

    // The slot really is still held: a fresh arrival fails fast.
    t.startImmediate('crowded', 'ok');
    await flush();
    expect(codeOf(t.ledger, 'crowded')).toBe('CIRCUIT_OPEN');

    // The genuine probe still works.
    t.gate.release('probe', 'ok');
    await flush();
    expect(t.view()?.state).toBe('closed');
    expect(t.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'closed'],
    ]);
  });

  it('a stale failure settling after the circuit closed does NOT re-open it', async () => {
    const t = harness({
      mode: 'consecutive',
      consecutiveFailureThreshold: 1,
      resetMs: 5_000,
      halfOpenSuccessesToClose: 1,
    });

    for (const id of ['first', 'straggler']) t.start(id);
    await flush();

    t.gate.fail('first', new TransportError('down'));
    await flush();
    expect(t.view()?.state).toBe('open'); // generation 1

    await t.h.runtime.advance(5_000);
    t.start('probe');
    await flush();
    expect(t.view()?.state).toBe('half-open'); // generation 2

    t.gate.release('probe', 'ok');
    await flush();
    expect(t.view()?.state).toBe('closed'); // generation 3
    expect(t.view()?.consecutiveFailures).toBe(0);

    // THE RACE. `straggler` holds generation 0 and fails now. With
    // `consecutiveFailureThreshold: 1` a single recorded failure re-opens, so if
    // the guard were missing the recovery would be undone by a call that failed
    // against a provider that has since come back.
    t.gate.fail('straggler', new TransportError('ancient'));
    await flush();

    expect(t.view()?.state, 'a two-generation-old failure must not re-open').toBe('closed');
    expect(t.view()?.consecutiveFailures).toBe(0);
    expect(t.view()?.generation).toBe(3);
    expect(t.transitions.map((e) => [e.from, e.to])).toEqual([
      ['closed', 'open'],
      ['open', 'half-open'],
      ['half-open', 'closed'],
    ]);

    // And the breaker is genuinely usable afterwards.
    t.startImmediate('after', 'ok');
    await flush();
    expect(t.ledger.resolved).toContain('after');
  });

  it('a cancelled call settling alongside a real failure scores neither way', async () => {
    const t = harness({ mode: 'consecutive', consecutiveFailureThreshold: 2, resetMs: 5_000 });

    for (const id of ['hangup', 'real']) t.start(id);
    await flush();

    // The caller withdraws. A CancelledError is `ignore`, never a failure —
    // otherwise a burst of client-side cancellations takes a healthy provider
    // out of service.
    t.gate.fail('hangup', new CancelledError('caller went away'));
    await flush();
    expect(t.view()?.consecutiveFailures).toBe(0);
    expect(t.transitions).toHaveLength(0);

    t.gate.fail('real', new TransportError('down'));
    await flush();
    expect(t.view()?.consecutiveFailures).toBe(1);
    expect(t.transitions, 'one real failure, threshold 2 — still closed').toHaveLength(0);
  });
});
