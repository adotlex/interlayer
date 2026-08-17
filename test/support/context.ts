/**
 * Context builders — READ-ONLY to Wave 2.
 *
 * `CallContext` has 12 fields and `AttemptContext` adds 3. Without this, five
 * units would each hand-roll their own and drift apart on the first field
 * anyone adds. Build one here:
 *
 * ```ts
 * const h = testContext({ capability: 'chat', provider: fakeProvider('a') });
 * await retry({ maxAttempts: 3 }).execute(h.attempt, () => handler(input, h.attempt));
 * await h.runtime.advance(100);
 * expect(h.emitted('retry:scheduled')).toHaveLength(1);
 * ```
 */

import { createEmitter } from '../../src/core/events.ts';
import type {
  AttemptContext,
  CallContext,
  CallOptions,
  CallStats,
  Emitter,
  InterlayerEvents,
  ProviderRecord,
} from '../../src/core/types.ts';
import { createFakeRuntime, type FakeRuntime, type FakeRuntimeOptions } from './fake-clock.ts';
import { type FakeProvider, fakeProvider } from './fake-provider.ts';

export interface RecordedEvent {
  readonly name: keyof InterlayerEvents;
  readonly payload: unknown;
}

export interface TestContextOptions extends FakeRuntimeOptions {
  readonly callId?: string | undefined;
  readonly capability?: string | undefined;
  readonly input?: unknown;
  readonly signal?: AbortSignal | undefined;
  readonly deadlineAt?: number | undefined;
  readonly hints?: CallOptions | undefined;
  readonly runtime?: FakeRuntime | undefined;
  /** Provider for the attempt context. A fresh scripted fake by default. */
  readonly provider?: FakeProvider | ProviderRecord | undefined;
  readonly attempt?: number | undefined;
}

export interface TestContextHandle {
  readonly runtime: FakeRuntime;
  readonly events: Emitter<InterlayerEvents>;
  readonly call: CallContext;
  readonly attempt: AttemptContext;
  readonly stats: CallStats;
  readonly failures: CallContext['failures'];
  readonly provider: ProviderRecord;
  /** Everything emitted, in order. */
  readonly recorded: readonly RecordedEvent[];
  /** Payloads of one event name, in order. */
  emitted<K extends keyof InterlayerEvents>(name: K): readonly InterlayerEvents[K][];
  /** Aborts the call-scoped signal with `reason`. */
  abort(reason?: unknown): void;
  /** Derives an attempt context with fields replaced (what `next(ctx')` does). */
  withAttempt(patch: Partial<AttemptContext>): AttemptContext;
}

function isFakeProvider(p: FakeProvider | ProviderRecord): p is FakeProvider {
  return 'record' in p;
}

export function testContext(opts: TestContextOptions = {}): TestContextHandle {
  const runtime = opts.runtime ?? createFakeRuntime(opts);
  const events = createEmitter<InterlayerEvents>();
  const recorded: RecordedEvent[] = [];
  events.onAny((name, payload) => {
    recorded.push({ name, payload });
  });

  const controller = new AbortController();
  const signal = opts.signal ?? controller.signal;
  const stats: CallStats = { attempts: 0, providersTried: 0, winnerId: undefined };
  const failures: CallContext['failures'] = [];

  const providerInput = opts.provider ?? fakeProvider('fake-provider').alwaysSucceed('ok');
  const record: ProviderRecord = isFakeProvider(providerInput)
    ? providerInput.record
    : providerInput;

  const call: CallContext = {
    callId: opts.callId ?? runtime.uuid(),
    capability: opts.capability ?? 'test.capability',
    input: opts.input,
    startedAt: runtime.now(),
    deadlineAt: opts.deadlineAt,
    signal,
    runtime,
    events,
    state: new Map<string, unknown>(),
    hints: opts.hints ?? {},
    failures,
    stats,
  };

  const attempt: AttemptContext = {
    ...call,
    provider: record,
    attempt: opts.attempt ?? 1,
  };

  return {
    runtime,
    events,
    call,
    attempt,
    stats,
    failures,
    provider: record,
    recorded,
    emitted<K extends keyof InterlayerEvents>(name: K): readonly InterlayerEvents[K][] {
      return recorded.filter((e) => e.name === name).map((e) => e.payload as InterlayerEvents[K]);
    },
    abort(reason?: unknown): void {
      controller.abort(reason);
    },
    withAttempt(patch: Partial<AttemptContext>): AttemptContext {
      return { ...attempt, ...patch };
    },
  };
}
