/**
 * FROZEN. The error taxonomy — R3 §5.3, with R6's public class names.
 *
 * Dual model on purpose: real `Error` subclasses (so `try/catch`, stack traces
 * and Node's `cause` printing all work) whose instances form a DISCRIMINATED
 * UNION via a literal `code`. Assert on `code` — it is a string, so it survives
 * duplicate package copies and realm boundaries where `instanceof` does not.
 */

import type { ErrorCode, ErrorContext, Issue } from './types.ts';

/** Cross-realm / duplicate-copy safe brand. `instanceof` alone is not reliable. */
const MARKER = 'interlayer.error.v1';
const kInterlayer = Symbol.for(MARKER);

export abstract class InterlayerError extends Error {
  abstract readonly code: ErrorCode;

  readonly providerId: string | undefined;
  readonly capability: string | undefined;
  readonly callId: string | undefined;
  readonly attempt: number | undefined;
  readonly details: Readonly<Record<string, unknown>>;
  /** Whether a *different* provider or a later attempt could plausibly succeed. */
  readonly retryable: boolean;

  protected constructor(message: string, ctx: ErrorContext = {}, retryableDefault = false) {
    super(message, ctx.cause !== undefined ? { cause: ctx.cause } : undefined);
    // The brand is stamped here rather than as a `[kInterlayer] = true` class
    // field: `isolatedDeclarations` rejects computed property names on classes
    // (TS9038). Behaviour is identical — `isInterlayerError` reads it off the
    // instance either way.
    (this as unknown as Record<symbol, unknown>)[kInterlayer] = true;
    this.name = new.target.name;
    this.providerId = ctx.providerId;
    this.capability = ctx.capability;
    this.callId = ctx.callId;
    this.attempt = ctx.attempt;
    this.details = ctx.details ?? {};
    this.retryable = ctx.retryable ?? retryableDefault;
    // V8 only; keeps the constructor frame out of the stack. No Object.setPrototypeOf
    // needed because we target ES2024, not ES5.
    Error.captureStackTrace?.(this, new.target);
  }

  /**
   * Returns a COPY with blank context slots filled in. Never overwrites a value
   * the thrower already supplied. Preserves prototype (so `instanceof` and the
   * brand survive), `stack`, `message` and `cause`.
   *
   * Needed because providers legitimately throw `new TransportError('down')`
   * with no idea of their own id; the router stamps identity on the way out.
   */
  withContext(ctx: ErrorContext): this {
    if (
      (ctx.providerId === undefined || this.providerId !== undefined) &&
      (ctx.capability === undefined || this.capability !== undefined) &&
      (ctx.callId === undefined || this.callId !== undefined) &&
      (ctx.attempt === undefined || this.attempt !== undefined)
    ) {
      return this;
    }
    const next = Object.create(Object.getPrototypeOf(this) as object) as this;
    for (const key of Reflect.ownKeys(this)) {
      const desc = Object.getOwnPropertyDescriptor(this, key);
      if (desc !== undefined) Object.defineProperty(next, key, desc);
    }
    const fill = (k: 'providerId' | 'capability' | 'callId' | 'attempt', v: unknown): void => {
      if (v !== undefined && (next as unknown as Record<string, unknown>)[k] === undefined) {
        Object.defineProperty(next, k, {
          value: v,
          enumerable: true,
          writable: false,
          configurable: true,
        });
      }
    };
    fill('providerId', ctx.providerId);
    fill('capability', ctx.capability);
    fill('callId', ctx.callId);
    fill('attempt', ctx.attempt);
    return next;
  }

  /** Structured, log-safe projection. Never includes the stack. */
  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      ...(this.providerId !== undefined ? { providerId: this.providerId } : {}),
      ...(this.capability !== undefined ? { capability: this.capability } : {}),
      ...(this.callId !== undefined ? { callId: this.callId } : {}),
      ...(this.attempt !== undefined ? { attempt: this.attempt } : {}),
      ...(Object.keys(this.details).length > 0 ? { details: this.details } : {}),
      ...(this.cause !== undefined ? { cause: describeCause(this.cause) } : {}),
    };
  }
}

/* ---------- Leaf classes ---------- */

export class ValidationError extends InterlayerError {
  readonly code = 'VALIDATION' as const;
  readonly issues: readonly Issue[];
  constructor(message: string, issues: readonly Issue[], ctx: ErrorContext = {}) {
    super(message, ctx, false);
    this.issues = issues;
  }
}

export class ConfigError extends InterlayerError {
  readonly code = 'CONFIG' as const;
  readonly issues: readonly Issue[];
  constructor(message: string, issues: readonly Issue[] = [], ctx: ErrorContext = {}) {
    super(message, ctx, false);
    this.issues = issues;
  }
}

export class UnsupportedCapabilityError extends InterlayerError {
  readonly code = 'UNSUPPORTED_CAPABILITY' as const;
  constructor(capability: string, ctx: ErrorContext = {}) {
    super(`No provider declares capability "${capability}"`, { ...ctx, capability }, false);
  }
}

export class NoProviderError extends InterlayerError {
  readonly code = 'NO_PROVIDER' as const;
  constructor(message: string, ctx: ErrorContext = {}) {
    super(message, ctx, false);
  }
}

export class TransportError extends InterlayerError {
  readonly code = 'TRANSPORT' as const;
  constructor(message: string, ctx: ErrorContext = {}) {
    super(message, ctx, true);
  }
}

export class TimeoutError extends InterlayerError {
  readonly code = 'TIMEOUT' as const;
  readonly timeoutMs: number;
  readonly scope: 'attempt' | 'call';
  constructor(timeoutMs: number, scope: 'attempt' | 'call', ctx: ErrorContext = {}) {
    super(`${scope} timed out after ${timeoutMs}ms`, ctx, scope === 'attempt');
    this.timeoutMs = timeoutMs;
    this.scope = scope;
  }
}

/** Caller-initiated abort. Never retryable — the caller asked us to stop. */
export class CancelledError extends InterlayerError {
  readonly code = 'CANCELLED' as const;
  constructor(message = 'Operation cancelled', ctx: ErrorContext = {}) {
    super(message, ctx, false);
  }
}

export class RateLimitedError extends InterlayerError {
  readonly code = 'RATE_LIMITED' as const;
  /** Server- or limiter-supplied hint, in ms. */
  readonly retryAfterMs: number | undefined;
  constructor(message: string, retryAfterMs?: number, ctx: ErrorContext = {}) {
    super(message, ctx, true);
    this.retryAfterMs = retryAfterMs;
  }
}

export class CircuitOpenError extends InterlayerError {
  readonly code = 'CIRCUIT_OPEN' as const;
  readonly key: string;
  readonly openedAt: number;
  readonly halfOpenAt: number;
  /**
   * How long from NOW until the breaker will admit a probe — a usable
   * `Retry-After`. `undefined` once the cooldown has already elapsed.
   *
   * This is `halfOpenAt − now`, and `now` is a required constructor argument for
   * exactly that reason. It was once `halfOpenAt − openedAt`, which is the
   * CONFIGURED `resetMs` and never changes as time passes: a caller five seconds
   * into a ten-second cooldown was told to wait ten more. There is no clock in
   * this file (and there must not be), so the thrower supplies the instant.
   */
  readonly retryAfterMs: number | undefined;
  constructor(
    key: string,
    openedAt: number,
    halfOpenAt: number,
    now: number,
    ctx: ErrorContext = {},
  ) {
    super(`Circuit "${key}" is open`, { ...ctx, details: { ...ctx.details, key } }, true);
    this.key = key;
    this.openedAt = openedAt;
    this.halfOpenAt = halfOpenAt;
    this.retryAfterMs = halfOpenAt > now ? halfOpenAt - now : undefined;
  }
}

export class BulkheadFullError extends InterlayerError {
  readonly code = 'BULKHEAD_FULL' as const;
  constructor(key: string, limit: number, ctx: ErrorContext = {}) {
    super(`Bulkhead "${key}" is full (limit ${limit})`, ctx, true);
  }
}

/** A provider returned a domain-level failure. `cause` holds the original throw. */
export class ProviderError extends InterlayerError {
  readonly code = 'PROVIDER_ERROR' as const;
  readonly status: number | undefined;
  constructor(message: string, ctx: ErrorContext = {}, status?: number) {
    super(message, ctx, ctx.retryable ?? isRetryableStatus(status));
    this.status = status;
  }
}

/**
 * Every configured attempt against ONE provider failed — R4 §1.6.
 *
 * Thrown by the retry policy ONLY when it actually made two or more attempts.
 * A single failure is rethrown unwrapped, because `maxAttempts: 1` must behave
 * exactly like no retry wrapper at all and because wrapping one error buries it.
 *
 * `retryable` is INHERITED FROM THE LAST FAILURE rather than defaulted. The
 * routing layer above decides whether to try another provider by reading
 * `error.retryable`; if this wrapper answered for itself, exhausting the retries
 * against a transiently-broken provider would silently stop the fallback chain.
 *
 * `cause` is the last error (Node's default error printing follows it) and
 * `errors` is every one of them in order — the first failure is frequently the
 * diagnostic one and the later ones are `ECONNREFUSED` from a now-dead process.
 */
export class RetryExhaustedError extends InterlayerError {
  readonly code = 'RETRY_EXHAUSTED' as const;
  /** Physical invocations made. Always `>= 2`; a single failure is not wrapped. */
  readonly attempts: number;
  /** Every failure, chronologically. `.at(-1)` is also `.cause`. */
  readonly errors: readonly unknown[];
  constructor(attempts: number, errors: readonly unknown[], ctx: ErrorContext = {}) {
    const last = errors.at(-1);
    super(
      `All ${attempts} attempt(s) failed${
        last instanceof Error ? `; last: ${last.name}: ${last.message}` : ''
      }`,
      { ...ctx, cause: ctx.cause ?? last, retryable: ctx.retryable ?? retryableOf(last) },
      false,
    );
    this.attempts = attempts;
    this.errors = errors;
  }
}

/** Whether a *different* provider could plausibly do better than this error did. */
function retryableOf(error: unknown): boolean {
  return isInterlayerError(error) ? error.retryable : false;
}

/**
 * Terminal error of a fallback chain. Aggregates one error per provider tried.
 *
 * NEVER wrap a single failure: it buries the real error one level down and
 * breaks `code` assertions. Aggregate only at >= 2 failures (R3, guide §4/U6).
 */
export class AllProvidersFailedError extends InterlayerError {
  readonly code = 'ALL_FAILED' as const;
  /** Every suppressed failure, in the order the providers were tried. */
  readonly failures: readonly AnyInterlayerError[];
  readonly triedProviderIds: readonly string[];
  constructor(failures: readonly AnyInterlayerError[], ctx: ErrorContext = {}) {
    const ids = failures.map((e) => e.providerId ?? '<unknown>');
    super(
      `All ${failures.length} provider(s) failed for "${ctx.capability ?? '?'}": ${failures
        .map((e, i) => `${ids[i]}=${e.code}`)
        .join(', ')}`,
      { ...ctx, cause: failures.at(-1) },
      false,
    );
    this.failures = failures;
    this.triedProviderIds = ids;
  }
}

/* ---------- Union: enables exhaustive `switch (err.code)` ---------- */

export type AnyInterlayerError =
  | ValidationError
  | ConfigError
  | UnsupportedCapabilityError
  | NoProviderError
  | TransportError
  | TimeoutError
  | CancelledError
  | RateLimitedError
  | CircuitOpenError
  | BulkheadFullError
  | ProviderError
  | RetryExhaustedError
  | AllProvidersFailedError;

/* ---------- Guards & normalisation ---------- */

export function isInterlayerError(e: unknown): e is AnyInterlayerError {
  return (
    typeof e === 'object' && e !== null && (e as Record<symbol, unknown>)[kInterlayer] === true
  );
}

export function hasCode<K extends ErrorCode>(
  e: unknown,
  code: K,
): e is Extract<AnyInterlayerError, { code: K }> {
  return isInterlayerError(e) && e.code === code;
}

/** Single funnel: every non-Interlayer throw becomes a typed error, cause preserved. */
export function toInterlayerError(e: unknown, ctx: ErrorContext = {}): AnyInterlayerError {
  if (isInterlayerError(e)) return e.withContext(ctx);
  if (isAbortLike(e)) return new CancelledError('Aborted', { ...ctx, cause: e });
  const message = e instanceof Error ? e.message : String(e);
  return new ProviderError(message, { ...ctx, cause: e }, extractStatus(e));
}

function isAbortLike(e: unknown): boolean {
  return (
    typeof e === 'object' &&
    e !== null &&
    ((e as { name?: unknown }).name === 'AbortError' ||
      (e as { name?: unknown }).name === 'TimeoutError')
  );
}

function extractStatus(e: unknown): number | undefined {
  if (typeof e !== 'object' || e === null) return undefined;
  const s = (e as { status?: unknown }).status ?? (e as { statusCode?: unknown }).statusCode;
  return typeof s === 'number' ? s : undefined;
}

function isRetryableStatus(status: number | undefined): boolean {
  if (status === undefined) return false;
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function describeCause(cause: unknown): unknown {
  if (isInterlayerError(cause)) return cause.toJSON();
  if (cause instanceof Error) return { name: cause.name, message: cause.message };
  return cause;
}

/** Flattens the `cause` chain for logs: "TimeoutError: … <- TransportError: …". */
export function formatErrorChain(e: unknown, max = 8): string {
  const parts: string[] = [];
  let cur: unknown = e;
  for (let i = 0; i < max && cur instanceof Error; i++) {
    parts.push(`${cur.name}: ${cur.message}`);
    cur = cur.cause;
  }
  return parts.join(' <- ');
}
