/**
 * Scriptable provider double — READ-ONLY to Wave 2.
 *
 * Produces a type-erased `ProviderRecord` (what the registry holds and what
 * routing walks) backed by a queue of outcomes you script up front:
 *
 * ```ts
 * const flaky = fakeProvider('flaky')
 *   .failWith(new TransportError('boom'), 2)   // first two calls throw
 *   .alwaysSucceed({ text: 'ok' });            // then succeed forever
 *
 * await policy.execute(ctx, () => flaky.handler('chat')(input, ctx));
 * expect(flaky.callCount).toBe(3);
 * expect(flaky.calls[0]?.capability).toBe('chat');
 * ```
 *
 * `delayMs` is spent on `ctx.runtime.sleep`, i.e. on the VIRTUAL clock — a
 * scripted 30-second call costs microseconds of wall time.
 */

import { CancelledError, ConfigError } from '../../src/core/errors.ts';
import type {
  AttemptContext,
  ErasedHandler,
  ProviderRecord,
  ProviderTraits,
} from '../../src/core/types.ts';

export interface FakeCall {
  readonly capability: string;
  readonly input: unknown;
  /** `ctx.runtime.now()` when the handler was entered. */
  readonly at: number;
  /** 1-based attempt number taken from the context. */
  readonly attempt: number;
  readonly callId: string;
}

interface Step {
  readonly outcome: 'succeed' | 'fail';
  readonly value: unknown;
  readonly delayMs: number;
  /** Repeats forever once reached. */
  readonly forever: boolean;
}

export interface FakeProviderOptions {
  /** Capability names this provider declares. Default `['test.capability']`. */
  readonly capabilities?: readonly string[] | undefined;
  readonly traits?: ProviderTraits | undefined;
  readonly priority?: number | undefined;
  readonly enabled?: boolean | undefined;
}

export interface FakeProvider {
  readonly id: string;
  /** The type-erased entry a registry would hold. */
  readonly record: ProviderRecord;
  /** Every call, in order. */
  readonly calls: readonly FakeCall[];
  readonly callCount: number;
  /** How many times `dispose()` was called. */
  readonly disposeCount: number;

  /** The erased handler for a capability. Throws if this provider lacks it. */
  handler(capability?: string): ErasedHandler;

  /** Enqueue `times` successful results. */
  succeed(value?: unknown, times?: number, delayMs?: number): FakeProvider;
  /** Enqueue `times` throwing results. */
  failWith(error: unknown, times?: number, delayMs?: number): FakeProvider;
  /** Succeed from here on, forever. Terminates the script. */
  alwaysSucceed(value?: unknown, delayMs?: number): FakeProvider;
  /** Fail from here on, forever. Terminates the script. */
  alwaysFail(error: unknown, delayMs?: number): FakeProvider;
  /** Add virtual latency to the next `times` scripted steps. */
  delay(ms: number, times?: number): FakeProvider;

  /** Clears the call log; leaves the remaining script intact. */
  resetCalls(): void;
}

const DEFAULT_CAPABILITY = 'test.capability';

export function fakeProvider(id: string, opts: FakeProviderOptions = {}): FakeProvider {
  const capabilities = opts.capabilities ?? [DEFAULT_CAPABILITY];
  const steps: Step[] = [];
  const calls: FakeCall[] = [];
  let disposeCount = 0;

  function nextStep(): Step {
    const head = steps[0];
    if (head === undefined) {
      throw new ConfigError(
        `fake provider "${id}": script exhausted after ${calls.length} call(s). ` +
          'Add another succeed()/failWith(), or end the script with alwaysSucceed()/alwaysFail().',
      );
    }
    if (!head.forever) steps.shift();
    return head;
  }

  function makeHandler(capability: string): ErasedHandler {
    return async (input: unknown, ctx: AttemptContext): Promise<unknown> => {
      if (ctx.signal.aborted) throw new CancelledError('Aborted before provider call');
      calls.push({
        capability,
        input,
        at: ctx.runtime.now(),
        attempt: ctx.attempt,
        callId: ctx.callId,
      });
      const step = nextStep();
      if (step.delayMs > 0) await ctx.runtime.sleep(step.delayMs, ctx.signal);
      if (step.outcome === 'fail') throw step.value;
      return step.value;
    };
  }

  const handlers = new Map<string, ErasedHandler>(
    capabilities.map((c) => [c, makeHandler(c)] as const),
  );

  const record: ProviderRecord = {
    id,
    capabilities: handlers,
    traits: opts.traits ?? {},
    priority: opts.priority ?? 0,
    enabled: opts.enabled ?? true,
    dispose: (): void => {
      disposeCount++;
    },
  };

  const api: FakeProvider = {
    id,
    record,
    get calls(): readonly FakeCall[] {
      return calls;
    },
    get callCount(): number {
      return calls.length;
    },
    get disposeCount(): number {
      return disposeCount;
    },

    handler(capability = capabilities[0] ?? DEFAULT_CAPABILITY): ErasedHandler {
      const h = handlers.get(capability);
      if (h === undefined) {
        throw new ConfigError(
          `fake provider "${id}" does not declare "${capability}" ` +
            `(declares: ${capabilities.join(', ')})`,
        );
      }
      return h;
    },

    succeed(value: unknown = undefined, times = 1, delayMs = 0): FakeProvider {
      for (let i = 0; i < times; i++) {
        steps.push({ outcome: 'succeed', value, delayMs, forever: false });
      }
      return api;
    },
    failWith(error: unknown, times = 1, delayMs = 0): FakeProvider {
      for (let i = 0; i < times; i++) {
        steps.push({ outcome: 'fail', value: error, delayMs, forever: false });
      }
      return api;
    },
    alwaysSucceed(value: unknown = undefined, delayMs = 0): FakeProvider {
      steps.push({ outcome: 'succeed', value, delayMs, forever: true });
      return api;
    },
    alwaysFail(error: unknown, delayMs = 0): FakeProvider {
      steps.push({ outcome: 'fail', value: error, delayMs, forever: true });
      return api;
    },
    delay(ms: number, times = Number.POSITIVE_INFINITY): FakeProvider {
      const end = Math.min(steps.length, times === Number.POSITIVE_INFINITY ? steps.length : times);
      for (let i = 0; i < end; i++) {
        const s = steps[i];
        if (s !== undefined) steps[i] = { ...s, delayMs: ms };
      }
      return api;
    },

    resetCalls(): void {
      calls.length = 0;
    },
  };

  return api;
}
