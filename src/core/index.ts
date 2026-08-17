/**
 * Internal re-export of CORE ONLY.
 *
 * `src/core/**` is the frozen contract surface: read-only to every unit. If
 * something here is wrong or insufficient, STOP AND REPORT — do not edit it,
 * and do not work around it with a local copy of a core type.
 *
 * This barrel deliberately re-exports nothing from `src/registry/`,
 * `src/resilience/**` or `src/routing/**`. The public barrel is `src/index.ts`,
 * written by the orchestrator after the wave.
 */

export * from './clock.ts';
export * from './compose.ts';
// `types.ts` re-declares `AnyInterlayerError` as a forward reference to break
// the declaration cycle. Both names point at the same type; the explicit
// re-export tells TypeScript which one wins (TS2308).
export type { AnyInterlayerError } from './errors.ts';
export * from './errors.ts';
export * from './events.ts';
export * from './policy.ts';
export * from './result.ts';
export * from './runtime.ts';
export * from './types.ts';
export * as v from './validate.ts';
