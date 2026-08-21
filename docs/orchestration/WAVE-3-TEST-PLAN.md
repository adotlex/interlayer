# Wave 3 Test Plan — Comprehensive Suites

**Executors:** six Opus 5 (high effort) test agents, parallel.
**Precondition:** core reconciliation complete, `src/layer.ts` and `src/index.ts` written, `npm run check` green.

## 1. Why a third wave at all

Wave 2 produced 460 co-located unit tests, and they are good — several were
mutation-verified by their authors. But every one of them tests a unit **in isolation**,
against fakes, with no other policy in the stack. Nothing yet tests:

- the assembled layer end to end,
- policies *interacting* (the canonical nesting order is a design decision no unit could verify alone),
- concurrency (every unit tested sequentially),
- the published surface (the barrel, the `exports` map, the emitted `dist/`).

Those four gaps are where integration bugs live, and they are exactly what a unit-test
wave cannot reach by construction.

## 2. Ownership — disjoint by directory, again

Wave 2 owns `src/**/*.test.ts` (co-located). **Wave 3 must not touch those.**
Wave 3 writes only under `test/`, one closed subtree per agent.

```
test/
├── support/            [FROZEN — orchestrator's, read-only to all]
├── integration/        [T1]  end-to-end through createLayer
├── composition/        [T2]  policy nesting order and interaction
├── concurrency/        [T3]  races, parallel calls, shared state
├── failure-modes/      [T4]  adversarial injection, error taxonomy, cause chains
├── types/              [T5]  type-level: inference, @ts-expect-error negatives
└── package/            [T6]  barrel completeness, exports map, built dist/
```

| Agent | Owns | Targets |
|---|---|---|
| T1 | `test/integration/**` | Happy path, provider fallback, retry-then-success, breaker opening, timeout, `only()`, `supports()`, `on()`/unsubscribe, `close()`, async dispose |
| T2 | `test/composition/**` | `TotalTimeout → Retry → RateLimit → Breaker → AttemptTimeout → call`. Retries spend tokens; each retry failure counts toward the breaker; attempt timeout bounds one try while total timeout bounds the whole call |
| T3 | `test/concurrency/**` | Concurrent calls racing a breaker transition; half-open admission under parallelism; rate-limit queue fairness/FIFO under load; per-provider state isolation |
| T4 | `test/failure-modes/**` | Every `ErrorCode` reachable; `cause` chains preserved; `AllProvidersFailedError.failures` retains every suppressed error; single failure unwrapped vs ≥2 aggregated; abort mid-flight at each stack layer |
| T5 | `test/types/**` | Call-site inference; unknown capability rejected; wrong input rejected; unregistered provider id rejected; `interface`-declared contracts still infer; readable error text |
| T6 | `test/package/**` | Every unit barrel export reachable from `src/index.ts`; no internal leakage; `npm run build` output shape; `dist/` importable under plain `node`; `exports` map correctness |

## 3. Rules (verbatim to agents)

1. **You own exactly one directory under `test/`.** Touch nothing outside it.
2. **`src/**` is READ-ONLY.** You are testing it, not fixing it. **If you find a bug, write a
   test that FAILS and report it** — do not fix the source, and do not soften the test to pass.
   A failing test that documents a real bug is a successful outcome for this wave.
3. **`test/support/**` is READ-ONLY** — shared harness. Need a new fake? Build it inside your
   own subtree.
4. Never edit `src/**/*.test.ts` — those belong to Wave 2.
5. Never run any `git` command. Never run `npm install`. Zero runtime dependencies.
6. Determinism is mandatory: no `Date.now()`, no `Math.random()`, no real sleeping. Fake clock
   and seeded RNG from `test/support/`. **A test that sleeps is a broken test.**
7. Fake timers: use the **async** advance variants — the sync ones do not flush microtasks and
   an `await`-chained test will hang to `testTimeout`.
8. Relative imports carry `.ts`. No `enum`/`namespace`/parameter properties.
9. `isolatedDeclarations` is on (TS9010/9037/9038). Biome errors on `any`, `!`, `console`.
10. **Scope your lint writes to your own directory** — `npx biome check --write test/<yours>`.
    `npm run lint:fix` is repo-wide and would rewrite five other agents' in-flight files.
11. **A `@ts-expect-error` that reports TS2578 (unused) is a FAILING test**, not a passing one —
    it means the negative case is not firing and the assertion proves nothing.

## 4. The repair loop (orchestrator, after the wave)

```bash
npx vitest run --reporter=dot --reporter=json --outputFile.json=./.vitest/results.json
node scripts/failed-tests.mjs        # → failing files + escaped, anchored test names
./scripts/rerun-failed.sh            # re-runs ONLY those
```

Two guards are mandatory and non-optional:

> 🔴 **A `-t` pattern matching nothing exits 0 with everything skipped.** Verified: a raw test
> name containing `(`, `[` or `$` produced `Tests 7 skipped (7)`, `EXIT=0`. A naive loop reads
> that as green and ships the bug.
>
> (a) Escape with `s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')` and anchor `^(...)$`.
> (b) Hard-fail when `numPassedTests + numFailedTests === 0` while failures were expected.
> A file that fails to *import* yields zero `assertionResults` — re-run that whole file.

Run the loop **without** `--coverage`; thresholds fail spuriously on partial runs.

**Green is only ever declared from a full-suite run, never a filtered one.**

## 5. Exit criteria

1. Full suite green from an unfiltered `npm test`.
2. `npm run check` (lint → typecheck → test) exit 0.
3. `npm run build` produces an importable `dist/`.
4. Every failure found by Wave 3 either fixed at the source or recorded as a known defect
   with a failing test and an explicit decision. **No test deleted or weakened to reach green.**
