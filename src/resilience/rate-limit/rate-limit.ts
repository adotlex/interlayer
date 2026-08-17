/**
 * THE RATE-LIMIT POLICY — a token bucket plus a bounded FIFO wait queue.
 *
 * Position in the onion (R4 §5.6, `POLICY_ORDER`, DECIDED):
 *
 * ```
 *   TotalTimeout > Retry > [ RateLimit ] > CircuitBreaker > AttemptTimeout > call
 * ```
 *
 * RateLimit sits INSIDE Retry, unlike Polly. Polly's limiter is a *concurrency*
 * limiter admitting logical operations; ours is a *quota* limiter counting
 * PHYSICAL calls, and a retry is a physical call. Retry storms are precisely
 * when quota protection matters most, so a limiter that exempted retries would
 * be disabled exactly when it is needed. This divergence is deliberate — do not
 * "fix" it back. It also means this middleware is entered once per attempt and
 * calls `next()` exactly once; the re-entrancy lives in retry, above.
 *
 * Everything time-shaped comes from `ctx.runtime` — a MONOTONIC clock and a
 * cancellable `sleep`. No `Date.now()`, no global `setTimeout`. The bucket is
 * bound to the first runtime it sees and keeps it, so one limiter never mixes
 * two clocks.
 *
 * Exhaustion (R4 §4.3): the default is `'wait'`, because "10/s" is a *pacing*
 * instruction, not an error condition. The guard rails are all mandatory —
 * bounded depth, bounded wait, strict FIFO, head-of-line rejection for an
 * unsatisfiable cost, clean abort while queued, and one timer-driven wake-up
 * for the head of the queue rather than polling.
 */

import { CancelledError, RateLimitedError } from '../../core/errors.ts';
import {
  DEFAULTS,
  definePolicy,
  type Policy,
  type RateLimitOptions,
  type ResolvedRateLimitOptions,
} from '../../core/policy.ts';
import type { AttemptContext, ErrorContext, Middleware, Runtime } from '../../core/types.ts';
import {
  assertValidBucketConfig,
  consume,
  createTokenBucket,
  DEFAULT_COST,
  isSatisfiable,
  refill,
  type TokenBucketState,
  waitMsFor,
} from './token-bucket.ts';

/**
 * `RateLimitOptions` (core, frozen) plus the one thing the core record cannot
 * carry: the limiter's identity. `ratelimit:throttled` has a `key` field and a
 * layer may hold one limiter per provider, so the key has to be nameable.
 * Every added field is optional, which keeps this assignable to
 * `PolicyFactory<RateLimitOptions>`.
 */
export interface RateLimitPolicyOptions extends RateLimitOptions {
  /** Identity in `name`, in `ratelimit:throttled` and in `describe()`. Default `'default'`. */
  readonly key?: string | undefined;
}

interface ResolvedOptions extends ResolvedRateLimitOptions {
  readonly key: string;
}

/** One caller parked in the FIFO queue. */
interface Waiter {
  readonly cost: number;
  /** Grant admission. Idempotent; detaches the abort listener. */
  readonly admit: () => void;
  /** Reject the waiter. Idempotent; detaches the abort listener. */
  readonly fail: (error: unknown) => void;
}

function resolveOptions(options: RateLimitPolicyOptions): ResolvedOptions {
  const d = DEFAULTS.rateLimit;
  const resolved: ResolvedOptions = {
    key: options.key ?? 'default',
    capacity: options.capacity ?? d.capacity,
    refillPerSec: options.refillPerSec ?? d.refillPerSec,
    onExhaustion: options.onExhaustion ?? d.onExhaustion,
    maxQueueDepth: options.maxQueueDepth ?? d.maxQueueDepth,
    maxQueueWaitMs: options.maxQueueWaitMs ?? d.maxQueueWaitMs,
    cost: options.cost ?? DEFAULT_COST,
  };
  assertValidBucketConfig(resolved);
  if (!Number.isInteger(resolved.maxQueueDepth) || resolved.maxQueueDepth < 0) {
    throw new RangeError(
      `maxQueueDepth must be an integer >= 0, got ${String(resolved.maxQueueDepth)}`,
    );
  }
  if (!Number.isFinite(resolved.maxQueueWaitMs) || resolved.maxQueueWaitMs < 0) {
    throw new RangeError(
      `maxQueueWaitMs must be a finite number >= 0, got ${String(resolved.maxQueueWaitMs)}`,
    );
  }
  if (!Number.isFinite(resolved.cost) || resolved.cost <= 0) {
    throw new RangeError(`cost must be a finite number > 0, got ${String(resolved.cost)}`);
  }
  return resolved;
}

/**
 * Build a rate-limit `Policy`.
 *
 * @throws RangeError on a configuration that cannot describe a bucket — at
 * construction, never on the hot path.
 */
export function rateLimit(options: RateLimitPolicyOptions = {}): Policy<AttemptContext, unknown> {
  const o = resolveOptions(options);

  /** The single mutable cell. Every transition goes through the pure bucket. */
  let state: TokenBucketState | undefined;
  /** Bound to the FIRST runtime seen, so one limiter never mixes two clocks. */
  let bound: Runtime | undefined;
  const queue: Waiter[] = [];
  /** Cancels the single head-of-queue wake-up timer. `undefined` = disarmed. */
  let timer: AbortController | undefined;
  let disposed = false;

  function bucket(runtime: Runtime): TokenBucketState {
    state ??= createTokenBucket(o, runtime.now());
    return state;
  }

  function disarm(): void {
    if (timer === undefined) return;
    // Aborting the sleep clears the underlying timer — that uncleared timer,
    // not an unhandled rejection, is the real leak (R4 §2.2).
    timer.abort();
    timer = undefined;
  }

  /** One timer for the head of the queue, re-armed on every dequeue. Never polls. */
  function arm(runtime: Runtime, waitMs: number): void {
    disarm();
    const ctrl = new AbortController();
    timer = ctrl;
    void runtime.sleep(waitMs, ctrl.signal).then(
      () => {
        if (timer === ctrl) timer = undefined;
        pump(runtime);
      },
      () => {
        /* disarmed: whoever cancelled us owns the next arm */
      },
    );
  }

  /**
   * Admit every waiter the bucket can currently pay for, in strict FIFO order,
   * then arm one timer for the first that it cannot. Safe to call at any time:
   * on enqueue, on wake-up, and after an abort drops a waiter — dropping one
   * can unblock the one behind it (R4 §4.3).
   */
  function pump(runtime: Runtime): void {
    for (;;) {
      const head = queue[0];
      if (head === undefined) {
        disarm();
        return;
      }
      const outcome = consume(bucket(runtime), runtime.now(), head.cost);
      state = outcome.state;
      if (outcome.allowed) {
        queue.shift();
        head.admit();
        continue;
      }
      if (!Number.isFinite(outcome.waitMs)) {
        // Rate 0 with an empty bucket: an infinite timer is not a wait, it is a
        // hang. Refuse the waiter rather than scheduling one.
        queue.shift();
        head.fail(exhausted(outcome.waitMs, 'the bucket never refills', undefined));
        continue;
      }
      arm(runtime, outcome.waitMs);
      return;
    }
  }

  function errorContext(ctx: AttemptContext, waitMs: number): ErrorContext {
    return {
      capability: ctx.capability,
      callId: ctx.callId,
      providerId: ctx.provider.id,
      attempt: ctx.attempt,
      details: { key: o.key, waitMs, queueDepth: queue.length },
    };
  }

  function exhausted(waitMs: number, why: string, ctx: AttemptContext | undefined): Error {
    // `retryAfterMs` is a HINT: only meaningful when finite. An `Infinity` hint
    // would tell a caller to sleep forever.
    const retryAfterMs = Number.isFinite(waitMs) ? waitMs : undefined;
    return new RateLimitedError(
      `Rate limit "${o.key}" exceeded: ${why}`,
      retryAfterMs,
      ctx === undefined ? { details: { key: o.key } } : errorContext(ctx, waitMs),
    );
  }

  function enqueue(runtime: Runtime, ctx: AttemptContext, cost: number): Promise<void> {
    return new Promise<void>((admitted, rejected) => {
      const signal = ctx.signal;
      let settled = false;
      const detach = (): void => {
        signal.removeEventListener('abort', onAbort);
      };
      const waiter: Waiter = {
        cost,
        admit: (): void => {
          if (settled) return;
          settled = true;
          detach();
          admitted();
        },
        fail: (error: unknown): void => {
          if (settled) return;
          settled = true;
          detach();
          rejected(error);
        },
      };
      function onAbort(): void {
        const at = queue.indexOf(waiter);
        if (at !== -1) queue.splice(at, 1);
        // No token is consumed: tokens are only ever spent at admission.
        waiter.fail(
          new CancelledError('Aborted while queued for a rate-limit token', {
            ...errorContext(ctx, 0),
            cause: signal.reason,
          }),
        );
        // Dropping a waiter promotes the next one; re-arm (or disarm) for it.
        pump(runtime);
      }
      signal.addEventListener('abort', onAbort, { once: true });
      queue.push(waiter);
      if (timer === undefined) pump(runtime);
    });
  }

  /** Spend one `cost` worth of tokens, waiting in line if that is the mode. */
  async function acquire(ctx: AttemptContext): Promise<void> {
    if (disposed) throw new CancelledError(`Rate limiter "${o.key}" is disposed`);
    // Bind to the first runtime seen and keep it: one limiter, one clock.
    bound ??= ctx.runtime;
    const runtime = bound;
    const cost = o.cost;

    // Head-of-line rule (R4 §4.3): a cost above capacity can never be paid, so
    // it must never enter the queue — it would block it forever.
    if (!isSatisfiable(o, cost)) {
      throw new RangeError(
        `Rate limit "${o.key}": cost ${cost} exceeds capacity ${o.capacity}; ` +
          'the request can never be satisfied',
      );
    }
    // Entry guard, before any timer or listener exists.
    if (ctx.signal.aborted) {
      throw new CancelledError('Aborted before rate-limit admission', {
        ...errorContext(ctx, 0),
        cause: ctx.signal.reason,
      });
    }

    // Drain anyone already due, so a fresh arrival can never overtake a waiter
    // whose tokens have accrued but whose timer has not fired yet.
    if (queue.length > 0) pump(runtime);

    const now = runtime.now();
    let current: TokenBucketState;
    if (queue.length === 0) {
      const outcome = consume(bucket(runtime), now, cost);
      state = outcome.state;
      if (outcome.allowed) return;
      current = outcome.state;
    } else {
      current = refill(bucket(runtime), now);
      state = current;
    }

    // Cost of joining the back of the line, in closed form: everything already
    // queued must be paid for before us. Tokens are consumed as they arrive, so
    // cumulative demand may exceed capacity — `waitMsFor` prices that honestly.
    const demand = queue.reduce((sum, w) => sum + w.cost, cost);
    const waitMs = waitMsFor(current, demand);
    ctx.events.emit('ratelimit:throttled', { key: o.key, waitMs, at: now });

    if (o.onExhaustion === 'reject') throw exhausted(waitMs, 'no tokens available', ctx);
    if (!Number.isFinite(waitMs)) throw exhausted(waitMs, 'the bucket never refills', ctx);
    if (queue.length >= o.maxQueueDepth) {
      throw exhausted(waitMs, `queue is full (maxQueueDepth ${o.maxQueueDepth})`, ctx);
    }
    // Bounded wait, checked AT ENQUEUE: a waiter that would need longer than
    // the bound is refused now, not admitted and disappointed later.
    if (waitMs > o.maxQueueWaitMs) {
      throw exhausted(waitMs, `wait ${waitMs}ms exceeds maxQueueWaitMs ${o.maxQueueWaitMs}`, ctx);
    }
    await enqueue(runtime, ctx, cost);
  }

  const execute: Middleware<AttemptContext, unknown> = async (ctx, next) => {
    await acquire(ctx);
    // Tokens are spent on ADMISSION and never refunded: the physical call has
    // been made, and the provider's quota was consumed whatever it returned.
    return await next();
  };

  return definePolicy<AttemptContext, unknown>({
    kind: 'rate-limit',
    name: `rate-limit(${o.key})`,
    scope: 'attempt',
    execute,
    describe: () => ({
      key: o.key,
      capacity: o.capacity,
      refillPerSec: o.refillPerSec,
      cost: o.cost,
      onExhaustion: o.onExhaustion,
      maxQueueDepth: o.maxQueueDepth,
      maxQueueWaitMs: o.maxQueueWaitMs,
      // Test hook (R4 RL-23). Compare with a TOLERANCE, never with `===`.
      tokens: state?.tokens ?? o.capacity,
      lastRefillAt: state?.last,
      queueDepth: queue.length,
      timerArmed: timer !== undefined,
    }),
    dispose: () => {
      disposed = true;
      disarm();
      for (const waiter of queue.splice(0, queue.length)) {
        waiter.fail(new CancelledError(`Rate limiter "${o.key}" disposed`));
      }
    },
  });
}
