# Wave 2 Setup Guide — Implementation

**Derived from:** the six Wave 1 findings docs in `findings/` (5,086 lines, all empirically validated by their authors).
**Audience:** six Opus 5 (high effort) build agents running **in parallel**, plus the orchestrator.
**Rule of this document:** every decision below is **settled**. Wave 1 already considered the
alternatives and recorded why they lost. Do not re-litigate; if you believe a decision is wrong,
**stop and report to the orchestrator** rather than deviating.

---

## 1. Settled decisions

| Area | Decision | Source |
|---|---|---|
| Language / runtime | TypeScript on Node 22, **ESM-only** | R1 |
| TypeScript | **`6.0.3`, pinned exactly** (no caret) | R1 + R5, independently |
| Build | plain `tsc -p tsconfig.build.json` | R1 |
| Package manager | **npm** (`npm ci`, committed lockfile) | orchestrator, §1.1 |
| Test runner | **Vitest 4.1.10** | R2 (overrides R5's steer, §1.2) |
| Lint + format | **Biome 2.5.8**, pinned exactly | R5 |
| Runtime dependencies | **zero**, permanently | C4 |
| Module imports | relative imports carry the **`.ts`** extension | R1 + R5 |
| Layout | **directory-closed ownership** | R5 §7 |
| Architecture | two-stack pipeline, F-bounded contract, dual error model | R3 |
| Public naming | `defineContract` / `capability` / `defineProvider` / `createLayer` / `.call()` | R6 |
| Resilience semantics | full jitter, rolling-window breaker, token bucket | R4 |
| Nesting order | `TotalTimeout → Retry → RateLimit → Breaker → AttemptTimeout → call` | R4 §5.6 |

### 1.1 npm over pnpm — orchestrator call

R1 measured pnpm's offline reinstall at 0.58 s against npm's 1.48 s. R5 built and validated the
**entire** green skeleton on npm, including `.npmrc`, the CI workflow and the 13 agent rules. A
0.9 s install win does not justify invalidating the one configuration set that has actually been
run end to end. **npm.**

### 1.2 Vitest over `node:test` — orchestrator call

This is the wave's one genuine divergence. R5 steered to `node:test` (zero packages, zero
transpiler) and R2 recommended Vitest. **R2 wins, on capability, not preference:**

- `node --test --test-reporter=json` **does not exist** — Node attempts `import('json')` and
  crashes. Its JUnit output emits `classname="test"` for every case, so the **file path is
  unrecoverable**. R2 verified both.
- No file path means no programmatic failed-test extraction, and the failed-test-only re-run
  loop is a hard requirement, not a nicety.
- R5 explicitly deferred this call and scoped the blast radius: **only the `test:*` script lines
  change.** `lint`, `typecheck`, `build`, `check`, CI and the whole ownership map are unaffected.

Two hazards R5 documented **disappear** under Vitest: the phantom-pass on a non-matching name
filter is *narrower* (see §5.2 — it does not disappear, it changes shape), and Vitest's v8
coverage with `all: true` **can** see never-imported files, which Node's cannot.

### 1.3 Deferred out of build 1

The `interlayer/testing` published subpath (R6 §5.3). Test doubles live in `test/support/**`,
unpublished. Revisit after green.

---

## 2. Directory ownership — the rule that protects the run

> **Ownership is by DIRECTORY, not by file.** A subtree is a *closed* allocation: you may create
> anything inside yours and can never collide with a sibling.

```
src/
├── index.ts                      [ORCH-POST]  the barrel — one true conflict point
├── layer.ts                      [ORCH-POST]  createLayer facade — integration
│
├── core/                         [ORCH-PRE]   CONTRACTS — read-only to every unit
│   ├── types.ts                    contract/provider/context types (declarations only)
│   ├── errors.ts                   error taxonomy + guards
│   ├── result.ts                   Result type + helpers
│   ├── clock.ts                    Clock port — injected time (C3)
│   ├── runtime.ts                  Runtime port — clock + random + sleep (C3)
│   ├── policy.ts                   Policy / middleware interface  ← makes U2–U6 independent
│   ├── compose.ts                  compose() onion
│   ├── events.ts                   typed emitter
│   ├── validate.ts                 zero-dep validator combinators
│   └── index.ts                    internal re-export of core only
│
├── registry/                     [U1]  registry.ts · provider.ts · capability.ts
├── resilience/                         ← NO index.ts here. Deliberate: it would be a 4-way conflict.
│   ├── retry/                    [U2]  backoff.ts · retry.ts
│   ├── circuit-breaker/          [U3]  state.ts (pure reducer) · breaker.ts
│   ├── rate-limit/               [U4]  token-bucket.ts (pure) · rate-limit.ts
│   └── timeout/                  [U5]  deadline.ts · timeout.ts
└── routing/                      [U6]  router.ts · selectors.ts · fallback.ts

test/support/                     [ORCH-PRE]  fake-clock · seeded-random · fake-provider
```

| Unit | Owns exclusively (recursive) | May read, never write | Must never touch |
|---|---|---|---|
| U1 registry | `src/registry/**` | `src/core/**`, `test/support/**` | any other `src/**`, all root config, `src/index.ts`, `src/layer.ts` |
| U2 retry | `src/resilience/retry/**` | ” | ” |
| U3 breaker | `src/resilience/circuit-breaker/**` | ” | ” |
| U4 rate limit | `src/resilience/rate-limit/**` | ” | ” |
| U5 timeout | `src/resilience/timeout/**` | ” | ” |
| U6 routing | `src/routing/**` | ” | ” |

Tests are **co-located** inside each subtree, so test ownership is disjoint by construction.

### 2.1 The rule that makes the units genuinely independent

**No unit may import another unit.** Imports come only from `src/core/**`, `test/support/**`,
your own subtree, and `node:*`.

U6 (routing) conceptually wants to compose retry and breaking. It resolves through the
**contract**, not a dependency: `src/core/policy.ts` defines `Policy`; U2–U5 each export a
**factory returning a `Policy`**; U6 operates on the `Policy` **interface only**. Concrete wiring
happens post-wave in `src/layer.ts`. This is what converts a dependency graph into six parallel
units.

---

## 3. Agent rules — verbatim, non-negotiable

1. **You own exactly one directory.** Create, edit, delete anything inside it. Touch nothing outside it.
2. **`src/core/**` and `test/support/**` are READ-ONLY.** They are your contract. If one is wrong or insufficient, **stop and report** — do not edit it, and do not work around it with a local copy of a core type.
3. **Never create or edit `src/index.ts` or `src/layer.ts`.** Export your unit's surface from *your own* `index.ts` and stop.
4. **Never edit** `package.json`, `package-lock.json`, `tsconfig*.json`, `vitest.config.ts`, `biome.json`, `.npmrc`, `.gitignore`, `.editorconfig`, or anything under `.github/`.
5. **Never run `npm install` / `npm ci` / `npm i <pkg>` / `npm pkg set`.** Dependencies are frozen and installed. This library ships **zero** runtime dependencies; if you think you need one, you are solving the wrong problem — report instead.
6. **Never run any `git` command.** No add, commit, checkout, stash. One `git add -A` from one agent captures five others' half-finished work.
7. **Do not import from another unit.** Core, test support, your own subtree, `node:*`. Nothing else.
8. **Do not create shared helpers outside your subtree.** If two units need the same small utility, **duplicate it**. Duplication is cheap; a contested file corrupts the run.
9. **Co-locate tests** as `*.test.ts` beside the code, inside your subtree.
10. **Determinism is mandatory.** No `Date.now()`, no `Math.random()`, no `setTimeout` for real waiting. Take `Runtime`/`Clock` from `src/core/`, fakes from `test/support/`. **A test that sleeps is a broken test.**
11. **No `enum`, `namespace`, or constructor parameter properties.** `erasableSyntaxOnly` is on; these throw at runtime under type-stripping.
12. **Relative imports carry the `.ts` extension** — `import { x } from './retry.ts'`. `tsc` rewrites to `.js` on build. **Do not "correct" these to `.js`** — that breaks every test while `tsc` still passes silently (R1 risk R-6).
13. **Before finishing, run `npm run lint:fix` then `npm run typecheck` and your own unit's tests.** Report honestly. Never disable a rule to go green; never add a `biome-ignore` to a file you do not own.

---

## 4. Unit briefs

Every unit exports a `Policy` factory from its own `index.ts` (U1 and U6 excepted — see below).
Read `src/core/policy.ts` first; it is the contract you implement.

### U1 — Registry & providers · `src/registry/`
Provider registration and capability negotiation. `createRegistry`: id uniqueness, priority
ordering, enable/disable, `candidatesFor(capability)`, disposal. `defineProvider` helper and the
single handler type-erasure site. Capability introspection (`supports`).
**Source:** R3 §5.1 (types), R6 §5.1 (inference technique — the phantom carrier; the F-bounded
`Contract<C>` is already in core, use it), R6 §5.2.
**Watch:** annotating `Provider<C>[]` collapses names to `string` and **over-reports**
implemented capabilities — it fails open. Use `satisfies`, and validate at runtime as backstop
(R6 R1).

### U2 — Retry & backoff · `src/resilience/retry/`
`backoff.ts`: pure `(attempt, opts, random) => delayMs`. Constant / exponential / decorrelated ×
none / full / equal jitter. **Default: exponential + full jitter**, cap applied **before** jitter.
`retry.ts`: the retry `Policy`. Re-entrant — it calls `next()` N times.
**Source:** R4 §1, §5.1, §5.5. **Defaults:** R4 §0.1.
**Non-negotiable:** `maxAttempts: 3` means **three total attempts including the first**
(cockatiel's meaning, *not* Polly's "retries in addition to"). R4 §1.2 fixed this deliberately.
Full jitter is a pure function of `(n, rand())`, so tests assert exact values with a seeded stub.

### U3 — Circuit breaker · `src/resilience/circuit-breaker/`
`state.ts`: **pure reducer** `(state, event, now) => state`. No timers, no I/O — table-testable.
`breaker.ts`: the `Policy` wrapper; emits transitions; throws `CircuitOpenError`.
Rolling time-window failure ratio with a **minimum-throughput guard**, lazy `OPEN→HALF_OPEN`,
generation counter to void stale settlements.
**Source:** R4 §3, §5.3. **Defaults:** `failureRatio 0.5`, `minimumThroughput 10`,
`windowMs 30_000`, `resetMs 10_000`, one half-open probe.
**Non-negotiable:** evaluate the trip condition after **every** outcome, not only failures.
Evaluating on failure alone silently never trips at a true 50% rate — R4 found this by running it (CB-6).

### U4 — Rate limit · `src/resilience/rate-limit/`
`token-bucket.ts`: pure `(state, now, cost) => { allowed, waitMs, state }`. Monotonic clock,
fractional accrual, no drift. `rate-limit.ts`: the `Policy`; bounded FIFO queue-and-wait; abort
while queued.
**Source:** R4 §4, §5.4. **Defaults:** `capacity 10`, `refillPerSec 10`, `maxQueueDepth 100`.
**Non-negotiable:** token counts drift ~1.7e-13. Tests use a tolerance, **never `===`** (R4 RL-6).

### U5 — Timeout & deadline · `src/resilience/timeout/`
`deadline.ts`: deadline arithmetic. `timeout.ts`: the `Policy`; narrows the `AbortSignal` via
`next(ctx')` without mutation; distinguishes caller-abort from timeout-abort.
**Source:** R4 §2, §5.2.
**Non-negotiable:** use a manual `AbortController` + the **injected** timer. Do **not** use
`AbortSignal.timeout()` — its timer is **unref'd**, so it is invisible to fake timers and would
make every timeout test untestable (R4 verified: a script whose only pending work was
`AbortSignal.timeout(1)` exited before it fired). Clear the timer in a `finally`: the real leak is
the uncleared timer holding the loop open, *not* a `Promise.race` unhandled rejection (a myth R4
disproved).

### U6 — Routing & fallback · `src/routing/`
`router.ts`: fallback chain over candidates, `shouldFallback`, aggregation.
`selectors.ts`: `inOrder`, `roundRobin`, `weighted`, `filterByTags`, `sticky` — pure or
`Runtime`-driven. `fallback.ts`: the chain walk.
**Operates on the `Policy` interface only — never on U2–U5 implementations.**
**Source:** R3 §2.1, §5.1.
**Non-negotiable:** aggregate into `AllProvidersFailedError` **only at ≥2 failures**. Wrapping a
single failure buries the error and breaks `code` assertions — R3's smoke suite caught this in its
own draft.

---

## 5. Verification

### 5.1 Commands

```bash
npm run typecheck                      # tsc --noEmit
npm run lint / lint:fix                # biome ci . / biome check --write .
npm test                               # full suite
npx vitest run <file>                  # one file
npm run check                          # lint → typecheck → test  (cheapest signal first)
```

### 5.2 The failed-test-only re-run — and its trap

```bash
npx vitest run --reporter=dot --reporter=json --outputFile.json=./.vitest/results.json
```
Failed set = `testResults[i].name` (absolute file path) + `assertionResults[j].fullName`, filtered
on `status === "failed"`. Re-run = failing **files as positional args** plus
`-t '^(escaped1|escaped2)$'`.

> 🔴 **A `-t` pattern matching nothing exits 0 with everything skipped.** R2 reproduced it: a raw
> `fullName` containing `(`, `[` or `$` produced `Tests 7 skipped (7)`, `EXIT=0`. A naive loop
> reads that as **green** and ships the bug.
>
> Both defenses are **required**: (a) escape with `s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')` and
> anchor `^(...)$`; (b) hard-fail when `numPassedTests + numFailedTests === 0` while failures were
> expected. A file that fails to *import* yields zero `assertionResults` — re-run that whole file.
>
> Run the repair loop **without** `--coverage`; thresholds fail spuriously on partial runs.
> **Green is only ever declared from a full-suite run, never a filtered one.**

### 5.3 Fake timers

Sync `advanceTimersByTime` / `runAllTimers` **do not flush microtasks**, so `await`-chained retry
backoff never completes and the test dies at `testTimeout`. Use
**`advanceTimersByTimeAsync` / `runAllTimersAsync`**. R2 reproduced three hang modes. Keep
`testTimeout: 5000`, never 0.

### 5.4 Structural audit — orchestrator, between the wave and the barrel

```bash
for d in src/registry src/resilience/retry src/resilience/circuit-breaker \
         src/resilience/rate-limit src/resilience/timeout src/routing; do
  [ -f "$d/index.ts" ] || echo "MISSING BARREL: $d"
  ls "$d"/*.test.ts >/dev/null 2>&1 || echo "NO TESTS: $d"
done
git status --porcelain -- src/core test/support package.json tsconfig.json biome.json vitest.config.ts
grep -rnE "from '\.\./\.\./(registry|routing)" src/ || true
grep -rnE "from '\.\./(retry|circuit-breaker|rate-limit|timeout)/" src/ || true
```
Silence on checks 2–4 and no output from check 1 means the lanes held.

### 5.5 Coverage
Collect and report; **do not gate** on a percentage in build 1. A threshold failure blocks the
green gate for reasons unrelated to correctness, and rewards tests written to raise a number.
Gate on **structure** (§5.4) instead.

---

## 6. Orchestrator pre-wave checklist

1. Write all root config, `.github/workflows/ci.yml`, `src/core/**`, `test/support/**`.
2. `npm install` once; commit the lockfile.
3. **Verify the two open cross-stream questions** (neither was tested by any single agent):
   - `.ts` relative-import extensions must resolve under **Vitest**, not just `node --test`. R1
     justified the style by a `node:test` benefit that no longer applies.
   - R6's phantom-carrier inference was verified on **tsc 5.9.3**; the pin is **6.0.3**. Re-verify.
4. Confirm `npm run check` is **green on the empty skeleton** before dispatching six agents into it.
