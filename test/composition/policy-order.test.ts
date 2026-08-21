/**
 * ORDERING, STRUCTURALLY — does `orderPolicies` / `policyStack` actually produce
 * the canonical nesting, and does `compose()` then nest it that way?
 *
 * ```
 *   TotalTimeout > Retry > CircuitBreaker > RateLimit > AttemptTimeout > call
 * ```
 *
 * CHANGED, DELIBERATELY: the breaker and the limiter used to be the other way
 * round. R4 §5.6 settled `Retry > RateLimit` (a retry is a physical call and
 * must be metered) and `Retry > Breaker` (every physical attempt is recorded);
 * it never settled the pair. Putting the limiter outside meant a call the
 * breaker was about to reject FIRST BOUGHT A TOKEN — draining quota for a
 * request never made and, in the default `'wait'` mode, SLEEPING before failing
 * fast, which is the one property a breaker exists for. Both of R4's stated
 * constraints still hold: retry is still outside the limiter, and the breaker
 * is still inside retry. See `POLICY_ORDER` in `src/core/policy.ts` for the
 * full reasoning and the acknowledged cost.
 *
 * Everything here is deliberately declaration-order-hostile: every input array
 * is shuffled, and several are shuffled into the EXACT REVERSE of the canonical
 * order, so a sort that silently stopped working could not pass by accident.
 *
 * The behavioural half of the proof — that each of those positions has the
 * consequence R4 says it has — lives in the other files in this directory.
 */

import { describe, expect, it } from 'vitest';
import { compose } from '../../src/core/compose.ts';
import {
  definePolicy,
  orderPolicies,
  POLICY_ORDER,
  POLICY_SCOPE,
  type Policy,
  type PolicyKind,
  type PolicyScope,
  policyRank,
  policyStack,
} from '../../src/core/policy.ts';
import type { AttemptContext, CallContext } from '../../src/core/types.ts';
import { circuitBreaker } from '../../src/resilience/circuit-breaker/index.ts';
import { rateLimit } from '../../src/resilience/rate-limit/index.ts';
import { retryPolicy } from '../../src/resilience/retry/index.ts';
import { attemptTimeout, totalTimeout } from '../../src/resilience/timeout/index.ts';
import { testContext } from '../support/index.ts';
import { buildComposition } from './harness.ts';

/** The canonical order, written out once, from the ASCII diagram above. */
const CANONICAL: readonly PolicyKind[] = [
  'total-timeout',
  'retry',
  'circuit-breaker',
  'rate-limit',
  'attempt-timeout',
  'custom',
];

/** The attempt-scope half of it — what `createRouter` composes. */
const CANONICAL_ATTEMPT: readonly PolicyKind[] = [
  'retry',
  'circuit-breaker',
  'rate-limit',
  'attempt-timeout',
  'custom',
];

/** A policy whose only job is to record when it is entered and left. */
function probe<Ctx>(
  kind: PolicyKind,
  scope: PolicyScope,
  log: string[],
  tag: string = kind,
): Policy<Ctx, unknown> {
  return definePolicy<Ctx, unknown>({
    kind,
    name: `probe(${tag})`,
    scope,
    execute: async (_ctx, next): Promise<unknown> => {
      log.push(`>${tag}`);
      try {
        return await next();
      } finally {
        log.push(`<${tag}`);
      }
    },
  });
}

/* ------------------------------------------------------------------ *
 * 1. The declared order
 * ------------------------------------------------------------------ */

describe('POLICY_ORDER / POLICY_SCOPE — the declared contract', () => {
  it('is exactly the canonical nesting, outermost first', () => {
    expect([...POLICY_ORDER]).toEqual([...CANONICAL]);
  });

  it('puts each kind on the stack the diagram puts it on', () => {
    // TotalTimeout is the ONLY call-scope kind: it must wrap provider selection,
    // so that trying three providers cannot take 3 x totalTimeoutMs.
    expect(POLICY_SCOPE['total-timeout']).toBe('call');
    expect(POLICY_SCOPE.retry).toBe('attempt');
    expect(POLICY_SCOPE['rate-limit']).toBe('attempt');
    expect(POLICY_SCOPE['circuit-breaker']).toBe('attempt');
    expect(POLICY_SCOPE['attempt-timeout']).toBe('attempt');
    expect(POLICY_SCOPE.custom).toBe('attempt');
  });

  it('ranks strictly increasingly, outermost = 0', () => {
    const ranks = CANONICAL.map((kind) => policyRank(kind));
    expect(ranks).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it('sorts an unknown kind last, behind custom', () => {
    // The cast is the point of the test: a kind added to the union later, or one
    // that arrives from a JS caller, must not be able to jump the queue.
    const unknown = 'bulkhead' as unknown as PolicyKind;
    expect(policyRank(unknown)).toBe(POLICY_ORDER.length);
    expect(policyRank(unknown)).toBeGreaterThan(policyRank('custom'));
  });
});

/* ------------------------------------------------------------------ *
 * 2. orderPolicies
 * ------------------------------------------------------------------ */

describe('orderPolicies', () => {
  it('sorts a fully REVERSED input back into the canonical order', () => {
    const log: string[] = [];
    const reversed = [...CANONICAL]
      .reverse()
      .map((kind) => probe<AttemptContext>(kind, POLICY_SCOPE[kind], log));

    expect(
      reversed.map((p) => p.kind),
      'precondition: the input is reversed',
    ).toEqual([...CANONICAL].reverse());
    expect(orderPolicies(reversed).map((p) => p.kind)).toEqual([...CANONICAL]);
  });

  it('sorts an interleaved shuffle into the canonical order', () => {
    const log: string[] = [];
    const shuffled: readonly PolicyKind[] = [
      'rate-limit',
      'custom',
      'total-timeout',
      'attempt-timeout',
      'retry',
      'circuit-breaker',
    ];
    const policies = shuffled.map((kind) => probe<AttemptContext>(kind, POLICY_SCOPE[kind], log));
    expect(orderPolicies(policies).map((p) => p.kind)).toEqual([...CANONICAL]);
  });

  it('keeps SAME-KIND policies in declaration order (stable sort)', () => {
    const log: string[] = [];
    const policies = [
      probe<AttemptContext>('custom', 'attempt', log, 'custom-a'),
      probe<AttemptContext>('circuit-breaker', 'attempt', log, 'breaker-global'),
      probe<AttemptContext>('custom', 'attempt', log, 'custom-b'),
      probe<AttemptContext>('circuit-breaker', 'attempt', log, 'breaker-per-provider'),
      probe<AttemptContext>('custom', 'attempt', log, 'custom-c'),
    ];
    expect(orderPolicies(policies).map((p) => p.name)).toEqual([
      'probe(breaker-global)',
      'probe(breaker-per-provider)',
      'probe(custom-a)',
      'probe(custom-b)',
      'probe(custom-c)',
    ]);
  });

  it('is a pure function: the input array is not mutated', () => {
    const log: string[] = [];
    const input = [
      probe<AttemptContext>('attempt-timeout', 'attempt', log),
      probe<AttemptContext>('retry', 'attempt', log),
    ];
    const before = input.map((p) => p.kind);
    orderPolicies(input);
    expect(input.map((p) => p.kind)).toEqual(before);
  });

  it('handles the empty and single-policy cases', () => {
    const log: string[] = [];
    expect(orderPolicies<AttemptContext, unknown>([])).toEqual([]);
    const one = probe<AttemptContext>('retry', 'attempt', log);
    expect(orderPolicies([one])).toEqual([one]);
  });
});

/* ------------------------------------------------------------------ *
 * 3. policyStack — scope partition plus ordering
 * ------------------------------------------------------------------ */

describe('policyStack', () => {
  it('splits the two stacks by scope and orders each canonically', () => {
    const log: string[] = [];
    const mixed: Policy<CallContext, unknown>[] = [
      probe<CallContext>('custom', 'attempt', log, 'attempt-custom'),
      probe<CallContext>('total-timeout', 'call', log),
      probe<CallContext>('custom', 'call', log, 'call-custom'),
      probe<CallContext>('retry', 'attempt', log),
    ];

    // The call stack keeps total-timeout outermost and user middleware inside it.
    const callStack = policyStack(mixed, 'call');
    expect(callStack).toHaveLength(2);
    expect(callStack[0]).toBe(mixed[1]?.execute);
    expect(callStack[1]).toBe(mixed[2]?.execute);

    // The attempt stack sees only the attempt-scope entries, retry first.
    const attemptStack = policyStack(mixed, 'attempt');
    expect(attemptStack).toHaveLength(2);
    expect(attemptStack[0]).toBe(mixed[3]?.execute);
    expect(attemptStack[1]).toBe(mixed[0]?.execute);
  });

  it('returns the policies’ own execute functions, not wrappers', () => {
    const log: string[] = [];
    const policy = probe<AttemptContext>('retry', 'attempt', log);
    expect(policyStack([policy], 'attempt')[0]).toBe(policy.execute);
  });
});

/* ------------------------------------------------------------------ *
 * 4. The onion `compose()` actually builds
 * ------------------------------------------------------------------ */

describe('policyStack + compose — the nesting that results', () => {
  it('enters outermost-first and leaves innermost-first', async () => {
    const handle = testContext();
    const log: string[] = [];
    // Declared in exactly the wrong order.
    const declared = [...CANONICAL_ATTEMPT]
      .reverse()
      .map((kind) => probe<AttemptContext>(kind, 'attempt', log));

    const run = compose<AttemptContext, unknown>(policyStack(declared, 'attempt'));
    const value = await run(handle.attempt, () => {
      log.push('=provider');
      return Promise.resolve('ok');
    });

    expect(value).toBe('ok');
    expect(log).toEqual([
      '>retry',
      '>circuit-breaker',
      '>rate-limit',
      '>attempt-timeout',
      '>custom',
      '=provider',
      '<custom',
      '<attempt-timeout',
      '<rate-limit',
      '<circuit-breaker',
      '<retry',
    ]);
  });

  it('a re-entrant outer policy re-runs the WHOLE inner chain each time', async () => {
    // This is what makes "retries spend tokens" and "retries count toward the
    // breaker" true at all: everything inside retry is re-entered per attempt.
    const handle = testContext();
    const log: string[] = [];
    const looper = definePolicy<AttemptContext, unknown>({
      kind: 'retry',
      name: 'looper(3)',
      scope: 'attempt',
      execute: async (_ctx, next): Promise<unknown> => {
        let last: unknown;
        for (let i = 0; i < 3; i++) last = await next();
        return last;
      },
    });
    const declared = [
      probe<AttemptContext>('attempt-timeout', 'attempt', log),
      probe<AttemptContext>('circuit-breaker', 'attempt', log),
      looper,
      probe<AttemptContext>('rate-limit', 'attempt', log),
    ];

    const run = compose<AttemptContext, unknown>(policyStack(declared, 'attempt'));
    await run(handle.attempt, () => {
      log.push('=provider');
      return Promise.resolve('ok');
    });

    expect(log.filter((entry) => entry === '=provider')).toHaveLength(3);
    expect(
      log.filter((entry) => entry === '>rate-limit'),
      'a token per PHYSICAL try',
    ).toHaveLength(3);
    expect(
      log.filter((entry) => entry === '>circuit-breaker'),
      'a breaker record per PHYSICAL try',
    ).toHaveLength(3);
    expect(
      log.filter((entry) => entry === '>attempt-timeout'),
      'one attempt timer per PHYSICAL try',
    ).toHaveLength(3);
  });
});

/* ------------------------------------------------------------------ *
 * 5. The REAL policies, from the real factories
 * ------------------------------------------------------------------ */

describe('the five shipped policy factories', () => {
  it('declare the kind and scope the canonical order assigns them', () => {
    const retry = retryPolicy({});
    const limiter = rateLimit({});
    const breaker = circuitBreaker({});
    const attempt = attemptTimeout({});
    const total = totalTimeout({});

    expect([retry.kind, retry.scope]).toEqual(['retry', 'attempt']);
    expect([limiter.kind, limiter.scope]).toEqual(['rate-limit', 'attempt']);
    expect([breaker.kind, breaker.scope]).toEqual(['circuit-breaker', 'attempt']);
    expect([attempt.kind, attempt.scope]).toEqual(['attempt-timeout', 'attempt']);
    expect([total.kind, total.scope]).toEqual(['total-timeout', 'call']);

    // `policyStack()` filters on the policy's OWN `scope`, not on
    // `POLICY_SCOPE[kind]`, so a factory that declared the two inconsistently
    // would land its policy on the wrong stack — outside the retry loop, say —
    // with no type error anywhere. None of the five do; this is the guard.
    for (const policy of [retry, limiter, breaker, attempt]) {
      expect(policy.scope, `${policy.name} declares its canonical scope`).toBe(
        POLICY_SCOPE[policy.kind],
      );
    }
    expect(total.scope).toBe(POLICY_SCOPE[total.kind]);
  });

  it('sort from a reversed declaration into Retry > Breaker > RateLimit > AttemptTimeout', () => {
    // Declared innermost-first — the exact reverse of the required nesting.
    const declared: Policy<AttemptContext, unknown>[] = [
      attemptTimeout({ attemptTimeoutMs: 10 }),
      rateLimit({ key: 'p' }),
      circuitBreaker({}),
      retryPolicy({ maxAttempts: 2 }),
    ];
    expect(orderPolicies(declared).map((p) => p.kind)).toEqual([...CANONICAL_ATTEMPT].slice(0, 4));
    expect(orderPolicies(declared).map((p) => p.name)).toEqual([
      'retry(full, 2)',
      'circuit-breaker',
      'rate-limit(p)',
      'attempt-timeout(10ms)',
    ]);
  });

  it('two limiters of the same kind keep declaration order', () => {
    const declared: Policy<AttemptContext, unknown>[] = [
      rateLimit({ key: 'openai' }),
      circuitBreaker({}),
      rateLimit({ key: 'anthropic' }),
    ];
    expect(orderPolicies(declared).map((p) => p.name)).toEqual([
      'circuit-breaker',
      'rate-limit(openai)',
      'rate-limit(anthropic)',
    ]);
  });

  it('the assembled composition is ordered by policyStack, not by declaration', () => {
    const composition = buildComposition({
      retry: { maxAttempts: 2 },
      rateLimit: { key: 'p' },
      breaker: {},
      timeout: { attemptTimeoutMs: 10, totalTimeoutMs: 100 },
    });

    expect(
      composition.declaredKinds,
      'precondition: the harness declares them scrambled',
    ).not.toEqual([...CANONICAL_ATTEMPT].slice(0, 4));
    expect(composition.attemptKinds).toEqual([
      'retry',
      'circuit-breaker',
      'rate-limit',
      'attempt-timeout',
    ]);
    expect(composition.callKinds).toEqual(['total-timeout']);
  });
});
