/**
 * Zero-dependency combinator validators — R3 §5.6.
 *
 * Three properties that matter:
 *  - `Infer<typeof schema>` gives the narrowed TypeScript type, so a schema is
 *    written once and the type follows;
 *  - issues are PATH-QUALIFIED (`endpoints[0].url: expected string, got number`);
 *  - ALL issues are collected, never just the first.
 *
 * `parse` throws `ValidationError` (a caller-data problem, non-retryable);
 * `parseConfig` throws `ConfigError` (a programmer problem, never retried).
 */

import { ConfigError, ValidationError } from './errors.ts';
import type { Infer, Issue, PathSegment, ValidationResult, Validator } from './types.ts';

export type { Infer, Issue, ValidationResult, Validator } from './types.ts';

const ok = <T>(value: T): ValidationResult<T> => ({ ok: true, value });
const bad = (
  path: readonly PathSegment[],
  message: string,
  code = 'invalid_type',
): ValidationResult<never> => ({ ok: false, issues: [{ path, message, code }] });

function typeOf(v: unknown): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function show(v: unknown): string {
  return typeof v === 'string' ? JSON.stringify(v) : String(v);
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return false;
  const proto: unknown = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function define<T>(
  typeName: string,
  fn: (v: unknown, p: readonly PathSegment[]) => ValidationResult<T>,
): Validator<T> {
  return { typeName, validate: (v: unknown, p: readonly PathSegment[] = []) => fn(v, p) };
}

/* ---------- Primitives ---------- */

export const string = (): Validator<string> =>
  define('string', (v, p) =>
    typeof v === 'string' ? ok(v) : bad(p, `expected string, got ${typeOf(v)}`),
  );

export const number = (): Validator<number> =>
  define('number', (v, p) =>
    typeof v === 'number' && Number.isFinite(v)
      ? ok(v)
      : bad(p, `expected finite number, got ${typeOf(v)}`),
  );

export const integer = (): Validator<number> =>
  define('integer', (v, p) =>
    typeof v === 'number' && Number.isInteger(v)
      ? ok(v)
      : bad(p, `expected integer, got ${typeOf(v)}`),
  );

export const boolean = (): Validator<boolean> =>
  define('boolean', (v, p) =>
    typeof v === 'boolean' ? ok(v) : bad(p, `expected boolean, got ${typeOf(v)}`),
  );

export const unknown = (): Validator<unknown> => define('unknown', (v) => ok(v));

export const literal = <const T extends string | number | boolean>(value: T): Validator<T> =>
  define(show(value), (v, p) =>
    v === value ? ok(v as T) : bad(p, `expected ${show(value)}, got ${show(v)}`),
  );

export const enums = <const T extends readonly (string | number)[]>(
  values: T,
): Validator<T[number]> =>
  define(values.map(show).join(' | '), (v, p) =>
    (values as readonly unknown[]).includes(v)
      ? ok(v as T[number])
      : bad(p, `expected one of ${values.map(show).join(', ')}, got ${show(v)}`),
  );

/* ---------- Refinements ---------- */

export function refine<T>(
  inner: Validator<T>,
  pred: (v: T) => boolean,
  message: string,
  code = 'refinement',
): Validator<T> {
  return define(inner.typeName, (v, p) => {
    const r = inner.validate(v, p);
    if (!r.ok) return r;
    return pred(r.value) ? r : bad(p, message, code);
  });
}

export const min = (v: Validator<number>, n: number): Validator<number> =>
  refine(v, (x) => x >= n, `must be >= ${n}`, 'too_small');

export const max = (v: Validator<number>, n: number): Validator<number> =>
  refine(v, (x) => x <= n, `must be <= ${n}`, 'too_big');

export const nonEmpty = (v: Validator<string>): Validator<string> =>
  refine(v, (x) => x.length > 0, 'must not be empty', 'too_small');

/* ---------- Modifiers ---------- */

export function optional<T>(inner: Validator<T>): Validator<T | undefined> {
  return define(`${inner.typeName}?`, (v, p) =>
    v === undefined ? ok(undefined) : inner.validate(v, p),
  );
}

export function withDefault<T>(inner: Validator<T>, fallback: () => T): Validator<T> {
  return define(inner.typeName, (v, p) =>
    v === undefined ? ok(fallback()) : inner.validate(v, p),
  );
}

/* ---------- Composites ---------- */

export function array<T>(inner: Validator<T>): Validator<T[]> {
  return define(`${inner.typeName}[]`, (v, p) => {
    if (!Array.isArray(v)) return bad(p, `expected array, got ${typeOf(v)}`);
    const out: T[] = [];
    const issues: Issue[] = [];
    for (let i = 0; i < v.length; i++) {
      const r = inner.validate(v[i], [...p, i]); // path accumulates
      if (r.ok) out.push(r.value);
      else issues.push(...r.issues);
    }
    return issues.length > 0 ? { ok: false, issues } : ok(out); // ALL issues, not the first
  });
}

type Shape = Readonly<Record<string, Validator<unknown>>>;
type ShapeOutput<S extends Shape> = { [K in keyof S]: Infer<S[K]> };

export function object<S extends Shape>(
  shape: S,
  opts: { readonly strict?: boolean | undefined } = {},
): Validator<ShapeOutput<S>> {
  const keys = Object.keys(shape);
  const typeName = `{ ${keys.map((k) => `${k}: ${shape[k]?.typeName ?? '?'}`).join('; ')} }`;
  return define(typeName, (v, p) => {
    if (!isPlainObject(v)) return bad(p, `expected object, got ${typeOf(v)}`);
    const out: Record<string, unknown> = {};
    const issues: Issue[] = [];
    for (const k of keys) {
      const field = shape[k];
      if (field === undefined) continue;
      const r = field.validate(v[k], [...p, k]);
      if (r.ok) {
        if (r.value !== undefined || k in v) out[k] = r.value;
      } else {
        issues.push(...r.issues);
      }
    }
    if (opts.strict === true) {
      for (const k of Object.keys(v)) {
        if (!(k in shape)) {
          issues.push({
            path: [...p, k],
            message: `unknown key "${k}" (allowed: ${keys.join(', ')})`,
            code: 'unrecognized_key',
          });
        }
      }
    }
    return issues.length > 0 ? { ok: false, issues } : ok(out as ShapeOutput<S>);
  });
}

export function record<T>(inner: Validator<T>): Validator<Record<string, T>> {
  return define(`Record<string, ${inner.typeName}>`, (v, p) => {
    if (!isPlainObject(v)) return bad(p, `expected object, got ${typeOf(v)}`);
    const out: Record<string, T> = {};
    const issues: Issue[] = [];
    for (const k of Object.keys(v)) {
      const r = inner.validate(v[k], [...p, k]);
      if (r.ok) out[k] = r.value;
      else issues.push(...r.issues);
    }
    return issues.length > 0 ? { ok: false, issues } : ok(out);
  });
}

/* ---------- Terminals ---------- */

/** Assertion function: narrows `value` in the CALLER's scope on success. */
export function assertValid<T>(
  value: unknown,
  schema: Validator<T>,
  label = 'value',
): asserts value is T {
  const r = schema.validate(value, []);
  if (!r.ok) throw new ValidationError(`Invalid ${label}: ${formatIssues(r.issues)}`, r.issues);
}

export function parse<T>(value: unknown, schema: Validator<T>, label = 'value'): T {
  const r = schema.validate(value, []);
  if (!r.ok) throw new ValidationError(`Invalid ${label}: ${formatIssues(r.issues)}`, r.issues);
  return r.value;
}

/** Config-flavoured parse: throws `ConfigError`, which is never retried. */
export function parseConfig<T>(value: unknown, schema: Validator<T>, label = 'config'): T {
  const r = schema.validate(value, []);
  if (!r.ok) {
    throw new ConfigError(`Invalid ${label}:\n  ${formatIssues(r.issues, '\n  ')}`, r.issues);
  }
  return r.value;
}

export function formatIssues(issues: readonly Issue[], sep = '; '): string {
  return issues
    .map((i) => `${i.path.length > 0 ? pathString(i.path) : '<root>'}: ${i.message}`)
    .join(sep);
}

export function pathString(path: readonly PathSegment[]): string {
  return path.reduce<string>(
    (acc, seg) => (typeof seg === 'number' ? `${acc}[${seg}]` : acc === '' ? seg : `${acc}.${seg}`),
    '',
  );
}
