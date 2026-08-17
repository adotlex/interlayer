/**
 * OPEN QUESTION 1 — do `.ts` relative-import specifiers resolve under VITEST?
 *
 * R1 justified the `./x.ts` import style by a `node --test` benefit (Node's
 * type-stripping does not remap `.js` -> `.ts`). R2 then disqualified
 * `node:test`, so that justification no longer applies and the style had never
 * been exercised under the runner we actually chose.
 *
 * This file is the check. It imports across four different shapes of `.ts`
 * specifier and asserts the imported values really work:
 *
 *   1. sibling file           './seeded-random.ts'
 *   2. cross-tree, deep       '../../src/core/errors.ts'
 *   3. cross-tree via barrel  '../../src/core/index.ts'  (which itself does
 *                             `export * from './types.ts'` etc. — a transitive
 *                             chain of .ts specifiers)
 *   4. type-only import       `import type { … } from '…/types.ts'`
 *
 * The companion checks live outside Vitest and are recorded in the handoff:
 * `tsc -p tsconfig.build.json` must rewrite these to `.js` in `dist/`, and the
 * emitted `dist/` must run under plain `node`.
 */

import { describe, expect, it } from 'vitest';
// 2. deep, direct file
import { CircuitOpenError, isInterlayerError } from '../../src/core/errors.ts';
// 3. barrel — re-exports ./clock.ts, ./compose.ts, ./errors.ts, ./events.ts,
//    ./policy.ts, ./result.ts, ./runtime.ts, ./types.ts, ./validate.ts
import {
  compose,
  DEFAULTS,
  hasCode,
  ok,
  POLICY_ORDER,
  TransportError,
} from '../../src/core/index.ts';
import type { Policy, PolicyKind } from '../../src/core/policy.ts';
// 4. type-only
import type { Middleware } from '../../src/core/types.ts';
// 1. sibling
import { seededRandom } from './seeded-random.ts';

describe('.ts relative-import specifiers under Vitest', () => {
  it('resolves a sibling file', () => {
    expect(typeof seededRandom(1)()).toBe('number');
  });

  it('resolves a deep cross-tree file and its runtime behaviour survives', () => {
    const e = new CircuitOpenError('alpha', 1_000, 11_000);
    expect(isInterlayerError(e)).toBe(true);
    expect(e.code).toBe('CIRCUIT_OPEN');
    expect(e.retryAfterMs).toBe(10_000);
  });

  it('resolves a barrel whose own exports are a chain of .ts specifiers', () => {
    expect(ok(1)).toEqual({ ok: true, value: 1 });
    expect(DEFAULTS.retry.maxAttempts).toBe(3);
    expect(POLICY_ORDER[0]).toBe('total-timeout');
    expect(hasCode(new TransportError('x'), 'TRANSPORT')).toBe(true);
  });

  it('erases type-only imports rather than emitting a runtime import', async () => {
    // `verbatimModuleSyntax` + `import type` is what keeps this from becoming
    // `SyntaxError: … does not provide an export named 'Middleware'`.
    const kind: PolicyKind = 'retry';
    const mw: Middleware<{ n: number }, number> = async (ctx, next) => (await next()) + ctx.n;
    const run = compose<{ n: number }, number>([mw]);
    await expect(run({ n: 2 }, async (c) => c.n * 10)).resolves.toBe(22);
    expect(kind).toBe('retry');
  });

  it('lets a policy value cross the .ts module boundary intact', async () => {
    const p: Policy<{ n: number }, number> = {
      kind: 'retry',
      name: 'probe',
      scope: 'attempt',
      execute: async (_ctx, next) => next(),
    };
    const run = compose<{ n: number }, number>([p.execute]);
    await expect(run({ n: 7 }, async (c) => c.n)).resolves.toBe(7);
  });
});
