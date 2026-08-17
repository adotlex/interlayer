/**
 * `Result` helpers. The non-throwing edge of the dual error model: internals
 * throw typed errors, the facade's `tryCall` hands back a `Result`.
 *
 * Every function here is pure and total — nothing in this file throws except
 * `unwrap`, which says so in its name.
 */

import { toInterlayerError } from './errors.ts';
import type { AnyInterlayerError, Result } from './types.ts';

export type { Result } from './types.ts';

export function ok<T>(value: T): Result<T, never> {
  return { ok: true, value };
}

export function err<E>(error: E): Result<never, E> {
  return { ok: false, error };
}

export function isOk<T, E>(r: Result<T, E>): r is { readonly ok: true; readonly value: T } {
  return r.ok;
}

export function isErr<T, E>(r: Result<T, E>): r is { readonly ok: false; readonly error: E } {
  return !r.ok;
}

/** Throws the contained error. Named so the throw is never a surprise. */
export function unwrap<T, E>(r: Result<T, E>): T {
  if (r.ok) return r.value;
  throw r.error;
}

export function unwrapOr<T, E>(r: Result<T, E>, fallback: T): T {
  return r.ok ? r.value : fallback;
}

export function unwrapOrElse<T, E>(r: Result<T, E>, fallback: (error: E) => T): T {
  return r.ok ? r.value : fallback(r.error);
}

export function map<T, U, E>(r: Result<T, E>, fn: (value: T) => U): Result<U, E> {
  return r.ok ? { ok: true, value: fn(r.value) } : r;
}

export function mapError<T, E, F>(r: Result<T, E>, fn: (error: E) => F): Result<T, F> {
  return r.ok ? r : { ok: false, error: fn(r.error) };
}

export function andThen<T, U, E>(r: Result<T, E>, fn: (value: T) => Result<U, E>): Result<U, E> {
  return r.ok ? fn(r.value) : r;
}

/** Runs `fn`, funnelling any throw through `toInterlayerError`. */
export function fromThrowable<T>(fn: () => T): Result<T, AnyInterlayerError> {
  try {
    return { ok: true, value: fn() };
  } catch (e) {
    return { ok: false, error: toInterlayerError(e) };
  }
}

/** Awaits `promise`, funnelling any rejection through `toInterlayerError`. */
export async function fromPromise<T>(promise: Promise<T>): Promise<Result<T, AnyInterlayerError>> {
  try {
    return { ok: true, value: await promise };
  } catch (e) {
    return { ok: false, error: toInterlayerError(e) };
  }
}
