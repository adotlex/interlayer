/**
 * Timeout policies — U5. `total-timeout` (call scope) and `attempt-timeout`
 * (attempt scope), both built on one wrapper transcribed from R4 §5.2.
 *
 * THREE RULES THIS FILE EXISTS TO ENFORCE
 *
 * 1. **Never `AbortSignal.timeout()`.** R4 verified on Node v22.22.2 that its
 *    timer is *unref'd* — a script whose only pending work was
 *    `AbortSignal.timeout(1)` exited before it fired — and that
 *    `AbortSignal.timeout(0).aborted === false` immediately after construction.
 *    An unref'd timer is invisible to fake timers, which would make every
 *    timeout test untestable. We use a manual `AbortController` plus the
 *    INJECTED timer (`ctx.runtime.deadline`), so tests drive it on a virtual
 *    clock and can assert `runtime.pendingTimers === 0`.
 *
 * 2. **Clear the timer in a `finally`.** R4 disproved the folklore that
 *    `Promise.race` leaks unhandled rejections (it subscribes to every input,
 *    so the loser is already handled). The real defect is the uncleared timer
 *    holding the event loop open for its full duration after a 1 ms success.
 *    `timer.dispose()` and `removeEventListener` both run in `finally`, on
 *    every path.
 *
 * 3. **Caller-abort is not timeout-abort.** A private latch is set BEFORE
 *    `ac.abort()`, so the abort listener knows which cause fired without
 *    sniffing `reason`. A deadline breach surfaces `TimeoutError` (retryable
 *    when attempt-scoped); a caller abort surfaces `CancelledError` (never
 *    retryable) carrying the caller's own `reason` as `cause`.
 *
 * The narrowed context is produced by `{ ...ctx }` and handed to `next(ctx')`.
 * The incoming context is NEVER mutated; `state`, `failures` and `stats` stay
 * shared by reference, exactly as `src/core/types.ts` intends.
 */

import { CancelledError, TimeoutError } from '../../core/errors.ts';
import {
  definePolicy,
  POLICY_SCOPE,
  type Policy,
  type PolicyFactory,
  type PolicyScope,
  type TimeoutOptions,
} from '../../core/policy.ts';
import type { AttemptContext, CallContext, ErrorContext, Next, Runtime } from '../../core/types.ts';
import {
  assertTimeoutMs,
  composeDeadline,
  type DeadlineComposition,
  describeMs,
  resolveTimeoutOptions,
} from './deadline.ts';

/* ------------------------------------------------------------------ *
 * 1. The wrapper — R4 §5.2, "the tricky one".
 * ------------------------------------------------------------------ */

/** What the narrowed context replaces. Nothing else about `ctx` changes. */
export interface TimeoutPatch {
  readonly signal: AbortSignal;
  readonly deadlineAt: number | undefined;
}

interface RunWithTimeoutInput<Ctx extends CallContext, R> {
  readonly ctx: Ctx;
  readonly next: Next<Ctx, R>;
  /**
   * Builds the replacement context. Supplied by the caller — where `Ctx` is
   * concrete — so this generic helper never spreads a type parameter.
   */
  readonly derive: (patch: TimeoutPatch) => Ctx;
  readonly budget: DeadlineComposition;
  readonly scope: PolicyScope;
  readonly errorContext: ErrorContext;
}

async function runWithTimeout<Ctx extends CallContext, R>(
  input: RunWithTimeoutInput<Ctx, R>,
): Promise<R> {
  const { ctx, next, derive, budget, scope, errorContext } = input;
  const outer: AbortSignal = ctx.signal;
  const runtime: Runtime = ctx.runtime;
  const failContext: ErrorContext = { ...errorContext, details: { source: budget.source } };

  /* Entry guards, before any timer or listener exists (R4 §2.5). */
  if (outer.aborted) {
    // Caller already gave up: do not invoke the operation, do not create a timer.
    throw new CancelledError(`Cancelled before the ${scope} started`, {
      ...errorContext,
      cause: outer.reason,
    });
  }
  if (budget.expired) {
    // `timeoutMs: 0`, or an inherited deadline that has already elapsed.
    throw new TimeoutError(budget.timeoutMs, scope, failContext);
  }
  if (!Number.isFinite(budget.timeoutMs)) {
    // Unbounded: no timer at all, and the caller's signal passes straight through.
    return await next();
  }

  const timeoutMs: number = budget.timeoutMs;
  const ac = new AbortController();
  /** The latch. Assigned BEFORE `ac.abort()` so the listener can tell the causes apart. */
  let timeoutError: TimeoutError | undefined;

  // The INJECTED timer. `runtime.deadline()` is the seam `src/core/runtime.ts`
  // documents for exactly this; under the fake runtime it is an ordinary
  // virtual-clock timer, counted by `pendingTimers` and killed by `dispose()`.
  const timer = runtime.deadline(timeoutMs);
  const onDeadline = (): void => {
    timeoutError = new TimeoutError(timeoutMs, scope, failContext);
    // Aborting WITH the error makes `ctx'.signal.reason` the TimeoutError, so a
    // provider that honours the signal sees the same error the caller will.
    ac.abort(timeoutError);
  };
  timer.signal.addEventListener('abort', onDeadline, { once: true });

  // The caller's signal is linked by hand rather than via `AbortSignal.any()`
  // or the runtime's `parent` argument, so the listener can be removed
  // deterministically below: Node emits no MaxListenersExceededWarning for
  // AbortSignal, so accumulation on a long-lived caller signal is silent.
  const onOuterAbort = (): void => {
    ac.abort(outer.reason);
  };
  outer.addEventListener('abort', onOuterAbort, { once: true });

  const narrowed: Ctx = derive({ signal: ac.signal, deadlineAt: budget.deadlineAt });

  try {
    return await new Promise<R>((resolve, reject) => {
      ac.signal.addEventListener(
        'abort',
        () => {
          reject(
            timeoutError ??
              new CancelledError(`Cancelled during the ${scope}`, {
                ...errorContext,
                cause: outer.reason,
              }),
          );
        },
        { once: true },
      );
      // `.then(resolve, reject)` attaches a rejection handler UNCONDITIONALLY, so
      // a late rejection arriving after the timeout is absorbed instead of
      // becoming an unhandled rejection (R4 verified). Wrapping in
      // `Promise.resolve().then(...)` turns a synchronous throw from `next()`
      // into a rejection of this promise rather than of the whole call.
      Promise.resolve()
        .then(() => next(narrowed))
        .then(resolve, reject);
    });
  } finally {
    timer.dispose(); // no timer leak on the fast path
    outer.removeEventListener('abort', onOuterAbort); // no listener accumulation
  }
}

/* ------------------------------------------------------------------ *
 * 2. The policies.
 * ------------------------------------------------------------------ */

function callErrorContext(ctx: CallContext): ErrorContext {
  return { callId: ctx.callId, capability: ctx.capability };
}

/**
 * Bounds the WHOLE logical operation — every provider, every retry, every
 * backoff sleep. Outermost in the canonical order, on the call stack.
 *
 * Honours `ctx.hints.timeoutMs` as a genuine per-call override of the
 * configured budget, then clamps the result to `ctx.deadlineAt` if one is
 * already in force: an absolute deadline is authoritative and may only be
 * tightened. `hints.deadlineMs` is deliberately NOT read here — translating it
 * into `ctx.deadlineAt` belongs to the facade, which owns `startedAt`.
 */
export const totalTimeout: PolicyFactory<TimeoutOptions, CallContext> = (
  options?: TimeoutOptions,
): Policy<CallContext, unknown> => {
  const configuredMs: number = resolveTimeoutOptions(options).totalTimeoutMs;

  return definePolicy<CallContext>({
    kind: 'total-timeout',
    name: `total-timeout(${describeMs(configuredMs)})`,
    scope: POLICY_SCOPE['total-timeout'],
    execute: (ctx, next) => {
      const requestedMs =
        ctx.hints.timeoutMs === undefined
          ? configuredMs
          : assertTimeoutMs(ctx.hints.timeoutMs, 'hints.timeoutMs');
      const budget = composeDeadline({
        now: ctx.runtime.now(),
        timeoutMs: requestedMs,
        deadlineAt: ctx.deadlineAt,
      });
      return runWithTimeout({
        ctx,
        next,
        budget,
        scope: 'call',
        errorContext: callErrorContext(ctx),
        derive: (patch) => ({ ...ctx, signal: patch.signal, deadlineAt: patch.deadlineAt }),
      });
    },
    describe: () => ({
      kind: 'total-timeout',
      scope: 'call',
      timeoutMs: configuredMs,
    }),
  });
};

/**
 * Bounds ONE provider call. Innermost in the canonical order, immediately
 * outside the handler.
 *
 * Always clamped to whatever the call-level deadline left, so it can never
 * outlive it — `attemptTimeoutMs: 10_000` with 300 ms of call budget left arms
 * a 300 ms timer, not a 10 s one. Per-call `hints.timeoutMs` is NOT read here:
 * it describes the whole call, and the call-scope policy has already turned it
 * into the deadline this one inherits.
 */
export const attemptTimeout: PolicyFactory<TimeoutOptions, AttemptContext> = (
  options?: TimeoutOptions,
): Policy<AttemptContext, unknown> => {
  const configuredMs: number = resolveTimeoutOptions(options).attemptTimeoutMs;

  return definePolicy<AttemptContext>({
    kind: 'attempt-timeout',
    name: `attempt-timeout(${describeMs(configuredMs)})`,
    scope: POLICY_SCOPE['attempt-timeout'],
    execute: (ctx, next) => {
      const budget = composeDeadline({
        now: ctx.runtime.now(),
        timeoutMs: configuredMs,
        deadlineAt: ctx.deadlineAt,
      });
      return runWithTimeout({
        ctx,
        next,
        budget,
        scope: 'attempt',
        errorContext: {
          ...callErrorContext(ctx),
          providerId: ctx.provider.id,
          attempt: ctx.attempt,
        },
        derive: (patch) => ({ ...ctx, signal: patch.signal, deadlineAt: patch.deadlineAt }),
      });
    },
    describe: () => ({
      kind: 'attempt-timeout',
      scope: 'attempt',
      timeoutMs: configuredMs,
    }),
  });
};
