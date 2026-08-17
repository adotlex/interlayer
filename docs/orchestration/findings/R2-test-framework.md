# R2 — Test Framework & Selective Re-Run

**Stream:** Wave 1 / R2 · **Date:** 2026-08-17 · **Status:** complete

**Verification legend**

- ✅ **VERIFIED** — I ran this in a scratch project (`/tmp/.../scratchpad/r2-probe`, `r2-nodetest`) on this exact machine (Node v22.22.2, npm 10.9.7, 4 CPUs, 15 GiB) and captured the real output.
- 📄 **DOCS** — from documentation / release notes / web search; not executed here.

> Everything in §2 (selective re-run), §3 (JSON extraction), §4 (fake timers), §5 (coverage),
> §6 (parallelism) is ✅ VERIFIED unless explicitly marked 📄.

---

## 1. Options considered

| Runner | Version (2026-08-17) | TS, no build step | Selective re-run by **name** | Machine-readable JSON | Fake timers (async) | Weight | 4-CPU speed |
|---|---|---|---|---|---|---|---|
| **Vitest** ✅ | **4.1.10** (latest; 5.0.0-rc.1 in rc) | ✅ native, esbuild/Vite, zero config | ✅ `-t <regex>` matching `fullName` | ✅ **built-in `json` reporter** with `file path` + `fullName` per test | ✅ `advanceTimersByTimeAsync` / `runAllTimersAsync` (microtask-flushing) | 51 pkgs / 79 MB incl. TS + coverage; vitest 2.2 MB, vite 2.4 MB | 288 tests / 24 files in **2.08 s** |
| Jest | 30.4.2 | ⚠️ needs Node type-stripping opt-in or ts-jest/SWC 📄 | ✅ `-t` | ✅ `--json --outputFile` (same Jest-shaped schema Vitest copies) | ✅ `jest.advanceTimersByTimeAsync` 📄 | heaviest (~300 pkgs) 📄 | slower; ESM still `--experimental-vm-modules` 📄 |
| **node:test** ✅ | built into Node 22.22.2 | ⚠️ `--experimental-strip-types`; dir glob does **not** pick up `.ts` (had to name files explicitly) | ⚠️ `--test-name-pattern` — but see fatal flaw below | ❌ **no `json` reporter** (`--test-reporter=json` → `ERR_MODULE_NOT_FOUND: Cannot find package 'json'`). Only tap/spec/dot/junit/lcov. | ⚠️ `t.mock.timers.tick()` is **sync only**; chained `await` backoff needs hand-placed `await Promise.resolve()` at the exact microtask depth | 0 deps | fast |
| uvu | unmaintained since ~2023 📄 | needs tsm/tsx | no name filter | none | none | tiny | fast |
| tap | v21 📄 | ✅ via built-in TS 📄 | `--grep` 📄 | TAP/`--reporter=json` 📄 | none built in | medium | medium |

---

## 2. Recommendation

**Vitest 4.1.10**, `pool: 'forks'`, v8 coverage, JSON reporter always on.

```
pnpm add -D vitest@^4.1.10 @vitest/coverage-v8@^4.1.10
```

`engines` on vitest 4.1.10 is `^20.0.0 || ^22.0.0 || >=24.0.0` ✅ — Node 22.22.2 qualifies. It pulls Vite 8.2.1.

**If the stack turns out to be Go:** use `go test` — `-run '^TestName$/^SubTest$'` for selection and `go test -json` (`Action:"fail"`, `Test`, `Package`) for extraction; the repair loop is equally mechanical. **If Rust:** `cargo test -- --exact <path::to::test>` plus `cargo nextest run --message-format libtest-json` (nextest is the one worth adopting — stock `cargo test` has no good machine-readable output). Sections 2–4 below are Vitest-specific and would be rewritten; the *architecture* of the repair loop (full run → JSON → extract → selective re-run → guard against zero-match) carries over unchanged.

---

## 3. Rationale

1. **Selective re-run actually works, both axes.** File filters and `-t` name filters compose in a single command across multiple files. ✅ Verified round-trip: 3 failures across 2 files → one command re-ran exactly those 3 → fixed one → same command reported 2 failing. This is the whole ballgame for the later wave.
2. **JSON reporter is built in and gives file path + full test name together.** `testResults[].name` (absolute path) and `testResults[].assertionResults[].fullName` are exactly the two fields needed to reconstruct a selective re-run. node:test cannot do this at all (see §9).
3. **TypeScript with zero build step**, zero config, and no `ts-jest`. 13 TS tests across 3 files ran in **0.9 s wall** from cold.
4. **Async fake timers are first-class.** `vi.advanceTimersByTimeAsync()` / `vi.runAllTimersAsync()` flush the microtask queue between timers — exactly what retry backoff / circuit-breaker cooldown / rate-limit windows need. node:test's sync-only `tick()` makes the same tests fragile.
5. **Coverage costs ~3.5%** (2050 ms → 2121 ms on 288 tests). Effectively free.

---

## 4. Rejected and why

- **node:test** — rejected on the JSON reporter alone. `--test-reporter=json` does not exist; Node tries to `import('json')` and dies. The only structured output is JUnit XML, and ✅ I confirmed **the file path is unrecoverable from it**: running `test/a.test.ts` and `test/b.test.ts` together produced `classname="test"` (the *directory*) on every `<testcase>`. You cannot map a failed test back to its file without writing a custom reporter. Also: raw `--test-name-pattern` with regex-special chars silently selected the *wrong* test and exited 0.
- **Jest 30** — works, but ~300 packages, ESM still behind `--experimental-vm-modules`, and TS needs an opt-in transform. No advantage over Vitest for a greenfield ESM/TS library.
- **uvu** — effectively unmaintained; no name filtering, no JSON, no fake timers. Disqualified.
- **tap** — capable, but a much smaller ecosystem and no compelling edge here.

---

## 5. Concrete specifics

### 5.1 Config — `vitest.config.ts` (literal, runs with **zero deprecation warnings** ✅)

```ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    environment: 'node',

    // Vitest 4 default is 'forks'. poolOptions was REMOVED in v4 —
    // these are all top-level now.
    pool: 'forks',
    maxWorkers: 4,
    fileParallelism: true,
    isolate: true,

    testTimeout: 5000,
    hookTimeout: 10000,
    teardownTimeout: 5000,

    includeTaskLocation: true,   // populates assertionResults[].location {line,column}

    clearMocks: true,
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,

    // Always emit the JSON report; the repair loop depends on it existing.
    reporters: ['default', 'json'],
    outputFile: { json: './.vitest/results.json' },

    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.ts'],
      exclude: ['**/*.test.ts', '**/index.ts'],
      thresholds: { lines: 80, functions: 80, branches: 70, statements: 80 },
    },
  },
});
```

> ⚠️ **Vitest 4 breaking change** ✅ observed: `test.poolOptions` was removed. Using
> `poolOptions: { forks: { singleFork: false } }` printed
> `DEPRECATED test.poolOptions was removed in Vitest 4`. `singleFork`/`singleThread`
> are gone — use `fileParallelism: false` instead. There is **no** `minWorkers`;
> `maxWorkers` accepts a number or a percentage string.

`package.json`:

```json
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest watch",
    "test:cov": "vitest run --coverage",
    "test:failed": "bash scripts/rerun-failed.sh"
  }
}
```

### 5.2 Selective re-run — literal commands ✅ ALL VERIFIED

```bash
# 1. one specific file (relative or absolute both work)
npx vitest run test/router.test.ts
npx vitest run /abs/path/to/test/router.test.ts

# 2. several specific files in one command
npx vitest run test/router.test.ts test/rate-limit.test.ts

# 3. one specific test by full name (see escaping below — MANDATORY)
npx vitest run test/retry.test.ts \
  -t '^retry WILL FAIL: wrong attempt count \(regex chars\) \[a-z\] \(x\) \$end$'

# 4. a set of specific tests across different files, one command
npx vitest run test/retry.test.ts test/rate-limit.test.ts \
  -t '^(rate limit window WILL FAIL: capacity clamp|retry WILL FAIL: wrong attempt count \(regex chars\) \[a-z\] \(x\) \$end|circuit breaker WILL FAIL: cooldown boundary off-by-one)$'
#  -> "Tests  3 failed | 7 skipped (10)", exit 1.  Exactly the 3 requested ran.

# 5. by file:line (Vitest 3+)
npx vitest run test/retry.test.ts:47

# enumerate everything (useful for sanity-checking the loop)
npx vitest list --json
npx vitest list --filesOnly
```

`fullName` = `ancestorTitles.join(' ') + ' ' + title` (space-joined, **not** `>`). Note that
human-readable console output and `vitest list` use `" > "` as the separator — do **not**
build `-t` patterns from those; build them from the JSON `fullName` field.

`it.each` names are fully interpolated in `fullName` ✅ — e.g. `backoff table attempt 1 -> 100 ms`,
`backoff table pair 1 + 2`. They select fine once escaped.

### 5.3 🔴 THE CRITICAL PITFALL — `-t` that matches nothing exits **0**

This is the single thing that can break the repair loop, and it is silent.

```bash
# raw fullName with regex metacharacters, unescaped:
npx vitest run test/retry.test.ts \
  -t 'retry WILL FAIL: wrong attempt count (regex chars) [a-z] (x) $end'
```

✅ **Actual output:**

```
 Test Files  1 skipped (1)
      Tests  7 skipped (7)
EXIT=0
```

Zero tests ran and the exit code was **0**. A naive loop reads this as "fixed" and moves on.
A completely bogus pattern behaves identically: `13 skipped (13)`, `EXIT=0`.

Two mandatory defenses:

1. **Escape and anchor every name.** `-t` is a JS regex matched against `fullName`.
   ```js
   const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
   const pattern = `^(${names.map(escapeRe).join('|')})$`;
   ```
   Escaping `|` is essential — an unescaped `|` in a test name would silently corrupt the
   alternation. Anchoring with `^…$` prevents a short name from also matching a longer one
   that contains it. ✅ Both the escaped and the escaped+anchored forms correctly selected
   `1 failed | 6 skipped`, exit 1.
2. **Assert that the re-run actually executed the expected number of tests** (see the guard in
   §5.5). `numPassedTests + numFailedTests` must equal the number of tests you asked for.

**Shell quoting:** wrap the pattern in **single quotes** in bash. Inside single quotes,
backslashes and `$` stay literal, which is what the escaper produced. Double quotes would let
the shell eat `$end` and `\(`. Better still, avoid the shell entirely — pass argv directly
(`execFile`/`spawn` with an array), or use the NUL-separated handoff in §5.5.

### 5.4 Machine-readable results — REAL captured JSON ✅

```bash
npx vitest run --reporter=dot --reporter=json --outputFile.json=./.vitest/results.json
```

`--outputFile.json=<path>` (cac dot notation) is the correct form when multiple reporters are
active; a bare `--outputFile=<path>` also worked here but is ambiguous with >1 file-writing
reporter. Available reporters ✅ (from `vitest --help`): `default, agent, minimal, blob,
verbose, dot, json, tap, tap-flat, junit, tree, hanging-process, github-actions`.

Real output, trimmed to the load-bearing fields:

```json
{
  "numTotalTestSuites": 8,
  "numTotalTests": 13,
  "numPassedTests": 10,
  "numFailedTests": 3,
  "numPendingTests": 0,
  "numTodoTests": 0,
  "startTime": 1786951968468,
  "success": false,
  "testResults": [
    {
      "name": "/abs/path/test/retry.test.ts",
      "status": "failed",
      "startTime": 1786951968682,
      "endTime": 1786951968693.7844,
      "message": "",
      "assertionResults": [
        {
          "ancestorTitles": ["retry"],
          "fullName": "retry WILL FAIL: wrong attempt count (regex chars) [a-z] (x) $end",
          "status": "failed",
          "title": "WILL FAIL: wrong attempt count (regex chars) [a-z] (x) $end",
          "duration": 1.4255540000000053,
          "failureMessages": [
            "AssertionError: expected \"vi.fn()\" to be called 2 times, but got 1 times\n    at /abs/path/test/retry.test.ts:50:16\n    ..."
          ],
          "location": { "line": 47, "column": 3 },
          "meta": {},
          "tags": []
        },
        {
          "ancestorTitles": ["retry"],
          "fullName": "retry succeeds on first attempt",
          "status": "passed",
          "title": "succeeds on first attempt",
          "duration": 2.403156999999993,
          "failureMessages": [],
          "location": { "line": 9, "column": 3 },
          "meta": {}, "tags": []
        }
      ]
    }
  ]
}
```

**The two fields the repair loop needs:**

| Need | Field |
|---|---|
| file path | `testResults[i].name` — **absolute** path |
| full test name (feed to `-t`) | `testResults[i].assertionResults[j].fullName` |
| failed? | `assertionResults[j].status === "failed"` |
| where | `assertionResults[j].location` — `{line, column}`, requires `includeTaskLocation: true` |
| why | `assertionResults[j].failureMessages` — `string[]` |

**Status vocabulary quirk** ✅: a filtered-out test has per-test `status: "skipped"`, but is
counted at the top level under **`numPendingTests`** (not `numSkippedTests`, which does not
exist). Don't confuse them.

**File that fails to import** ✅ — no assertions are produced at all:

```json
{ "name": "/abs/path/edge/broken.test.ts",
  "status": "failed",
  "assertionResults": [],
  "message": "Cannot find module '../src/nowhere.js' imported from /abs/path/edge/broken.test.ts" }
```

The loop must special-case this: re-run the **whole file** with no `-t`, because there are no
test names to target.

### 5.5 The repair-loop scripts (verified end-to-end ✅)

`scripts/failed-tests.mjs`:

```js
#!/usr/bin/env node
// Reads a Vitest JSON report and prints argv for a selective re-run.
// Usage: node scripts/failed-tests.mjs <report.json> [--print=argv|json|count]
import { readFileSync } from 'node:fs';
import { relative } from 'node:path';

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const reportPath = process.argv[2] ?? './.vitest/results.json';
const mode = (process.argv[3] ?? '--print=argv').split('=')[1];
const report = JSON.parse(readFileSync(reportPath, 'utf8'));

const failures = [];
for (const suite of report.testResults ?? []) {
  // A suite can fail to even load (import/syntax error) -> no assertions.
  if (suite.status === 'failed' && (suite.assertionResults ?? []).length === 0) {
    failures.push({ file: suite.name, fullName: null, loadError: true });
    continue;
  }
  for (const a of suite.assertionResults ?? []) {
    if (a.status === 'failed') {
      failures.push({
        file: suite.name,
        fullName: a.fullName,
        line: a.location?.line ?? null,
        messages: a.failureMessages ?? [],
      });
    }
  }
}

if (mode === 'json')  { console.log(JSON.stringify(failures, null, 2)); process.exit(0); }
if (mode === 'count') { console.log(String(failures.length));           process.exit(0); }
if (failures.length === 0) process.exit(0);

const files = [...new Set(failures.map((f) => relative(process.cwd(), f.file)))];
const named = failures.filter((f) => f.fullName);
const args = [...files];
// If any file failed to load, re-run those files wholesale (no -t).
if (named.length > 0 && !failures.some((f) => f.loadError)) {
  args.push('-t', `^(${named.map((f) => escapeRe(f.fullName)).join('|')})$`);
}
// NUL-separated so names containing spaces/pipes survive the shell handoff.
process.stdout.write(args.join('\0'));
```

`scripts/rerun-failed.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
REPORT="${1:-./.vitest/results.json}"
OUT="${2:-./.vitest/results.json}"

COUNT=$(node scripts/failed-tests.mjs "$REPORT" --print=count)
if [ "$COUNT" -eq 0 ]; then echo "no failures in $REPORT"; exit 0; fi

mapfile -d '' ARGS < <(node scripts/failed-tests.mjs "$REPORT" --print=argv)
echo "re-running $COUNT failed test(s)"

npx vitest run "${ARGS[@]}" --reporter=dot --reporter=json --outputFile.json="$OUT"
STATUS=$?

# GUARD: a -t pattern matching nothing exits 0 with everything skipped.
node -e '
const r = require(require("path").resolve(process.argv[1]));
const ran = (r.numPassedTests ?? 0) + (r.numFailedTests ?? 0);
const want = Number(process.argv[2]);
if (ran === 0 && want > 0) {
  console.error(`FATAL: selective re-run matched 0 tests (expected ${want}). Pattern is wrong - NOT a pass.`);
  process.exit(97);
}
console.log(`selective re-run executed ${ran} test(s); ${r.numFailedTests} still failing`);
' "$OUT" "$COUNT" || exit 97

exit $STATUS
```

✅ **Verified loop transcript:** full run → `3 failed | 10 passed` → extract 3 (with paths,
names, line numbers) → fix exactly one → `bash scripts/rerun-failed.sh` → `Tests 2 failed |
1 passed | 7 skipped (10)`, `selective re-run executed 3 test(s); 2 still failing` → extract 2.

> ⚠️ **Run the repair loop WITHOUT `--coverage`.** A selective re-run touches a fraction of
> `src/`, so thresholds will fail and muddy the exit code. Run coverage only on the final full
> green run.

### 5.6 Fake timers — the exact pattern ✅

**Correct** (this is the pattern to standardise on):

```ts
import { describe, it, expect, vi, afterEach } from 'vitest';

afterEach(() => { vi.useRealTimers(); });   // MUST restore, or later tests hang

it('backs off exponentially', async () => {
  vi.useFakeTimers();
  let calls = 0;
  const fn = vi.fn(async () => { calls++; if (calls < 3) throw new Error('boom'); return 'ok'; });

  const p = retry(fn, { attempts: 3, baseDelayMs: 1000 });
  // Do NOT `await p` yet — it is parked on a fake timer that nothing has advanced.
  await vi.advanceTimersByTimeAsync(1000);   // 1st backoff; flushes microtasks
  await vi.advanceTimersByTimeAsync(2000);   // 2nd backoff
  await expect(p).resolves.toBe('ok');
  expect(fn).toHaveBeenCalledTimes(3);
});
```

Rejection variant — **create the assertion before advancing**, so the rejection is never
unhandled:

```ts
it('rejects after exhausting attempts', async () => {
  vi.useFakeTimers();
  const fn = vi.fn(async () => { throw new Error('always'); });
  const p = retry(fn, { attempts: 3, baseDelayMs: 100 });
  const assertion = expect(p).rejects.toThrow('always');  // attach handler first
  await vi.runAllTimersAsync();                            // then drive the clock
  await assertion;
});
```

Circuit-breaker cooldown / rate-limit windows read `Date.now()` rather than awaiting, so the
**synchronous** API is correct and simpler there:

```ts
vi.useFakeTimers();
vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));  // also pins Date.now()
cb.recordFailure(); cb.recordFailure();
expect(cb.state).toBe('open');
vi.advanceTimersByTime(29_999); expect(cb.state).toBe('open');
vi.advanceTimersByTime(1);      expect(cb.state).toBe('half-open');
```

**The three hang modes, all ✅ reproduced** (each died with `Error: Test timed out in 3000ms`):

| Mode | Code | Why it hangs |
|---|---|---|
| 1 | `vi.useFakeTimers(); await sleep(5000);` | Nothing advances the clock; the timer never fires. |
| 2 | `const p = retry(...); vi.advanceTimersByTime(1000); vi.advanceTimersByTime(2000); await p;` | **Sync `advanceTimersByTime` does not flush the microtask queue.** The 2nd backoff timer is not even *scheduled* yet when the 2nd advance runs, so it is never fired. |
| 3 | `const p = retry(...); vi.runAllTimers(); await p;` | Same reason — `runAllTimers` only drains timers that exist *now*; timers created by a later `await` continuation are never seen. |

**Rule:** if the code under test has `await` *between* its timers, you must use the `Async`
variants — `advanceTimersByTimeAsync`, `runAllTimersAsync`, `advanceTimersToNextTimerAsync`,
`runOnlyPendingTimersAsync`. They `await` a microtask flush between each timer callback.
Sync variants are only safe when the timer chain is synchronous.

**Reassuring** ✅: all three hang modes were caught by `testTimeout` (5 s default) and the worker
exited cleanly — a wrong fake-timer pattern costs `testTimeout` per test, it does **not** wedge
the pipeline. Keep `testTimeout` at 5000; do not set it to 0.

Other gotchas:
- `vi.useFakeTimers()` must be undone in `afterEach` (`vi.useRealTimers()`), or a later real-time
  test hangs. `restoreMocks: true` does **not** restore timers.
- `test.concurrent` + fake timers share one fake clock inside a file → cross-talk. Do not mark
  timer tests `.concurrent`.
- `vi.setSystemTime()` controls `Date.now()`; `advanceTimersByTime` moves both the timer queue
  and the mocked clock.

### 5.7 Coverage ✅

`provider: 'v8'`. Cost measured on 288 tests / 24 files: **2050 ms → 2121 ms (~3.5%)**.
Istanbul is more precise on branch/statement mapping in transpiled code but roughly 2–4× the
overhead 📄; v8 is correct for a plain-TS library and is the Vitest default.

Threshold violations produce a non-zero exit and explicit lines ✅:

```
ERROR: Coverage for lines (0%) does not meet global threshold (80%)
ERROR: Coverage for branches (0%) does not meet global threshold (70%)
```

`json-summary` writes `coverage/coverage-summary.json`, machine-readable ✅:

```json
{ "total": { "lines":      { "total": 20, "covered": 0, "skipped": 0, "pct": 0 },
             "statements": { "total": 25, "covered": 0, "skipped": 0, "pct": 0 },
             "functions":  { "total": 7,  "covered": 0, "skipped": 0, "pct": 0 },
             "branches":   { "total": 8,  "covered": 0, "skipped": 0, "pct": 0 } } }
```

`coverage.include: ['src/**/*.ts']` makes untested files count as 0% rather than vanishing —
keep it, otherwise thresholds are trivially satisfiable.

### 5.8 Parallelism on 4 CPUs — measured ✅

288 tests / 24 files, this machine:

| Config | Wall time |
|---|---|
| `--pool=forks --maxWorkers=4` | **2075 ms** |
| `--pool=forks --maxWorkers=2` | 2841 ms |
| `--pool=threads --maxWorkers=4` | 2236 ms |
| `--pool=threads --maxWorkers=2` | 3064 ms |
| `--pool=forks --maxWorkers=4 --no-isolate` | 1142 ms |
| `--pool=threads --maxWorkers=4 --no-isolate` | 1102 ms |

**Recommendation: `pool: 'forks'`, `maxWorkers: 4`, `isolate: true`.** Forks beat threads here
and are the Vitest 4 default; process isolation matters for a library that will hold
module-level provider registries and breaker state. `isolate: false` is ~45% faster but shares
module state between files in a worker — the exact failure mode ("circuit already open from a
previous file") that would make the repair loop chase ghosts. Not worth it at this suite size.

Anti-flake settings already in §5.1: `isolate: true`, `clearMocks`, `restoreMocks`,
`unstubEnvs`, `unstubGlobals`, plus the `afterEach(() => vi.useRealTimers())` discipline.
`--sequence.shuffle` is available if cross-test order dependence is suspected.

### 5.9 Assertions ✅

Chai-flavoured `expect` with Jest-compatible matchers, built in. **No expect-extension package
is needed for async/rejection assertions:**

```ts
await expect(p).resolves.toBe('ok');
await expect(p).rejects.toThrow(/nope/);
await expect(p).rejects.toBeInstanceOf(TypeError);
await expect(p).rejects.toMatchObject({ message: 'x', code: 'PROVIDER_DOWN' });
await expect(fn()).rejects.toThrowError(InterlayerTimeoutError);
```

Also available: `expect.soft` (collect multiple failures per test), `expect.poll` (retry an
assertion until timeout — useful for eventual-consistency checks, but prefer fake timers),
`expect.assertions(n)`, `vi.fn()` / `toHaveBeenCalledTimes`.

✅ **Good news on a classic footgun:** a *missing* `await` on `.rejects`/`.resolves` does **not**
silently pass in Vitest 4. Both `expect(Promise.resolve('x')).rejects.toThrow('boom')` and
`expect(Promise.resolve(1)).resolves.toBe(999)` without `await` were reported as failures. Still
always `await` them — but the suite will not lie to you if someone forgets.

### 5.10 Watch mode and CI ✅

- **`vitest run` is the only form that should appear in any script, CI job, or the repair loop.**
  `run` is the explicit non-watch subcommand.
- `watch` defaults to `!process.env.CI`, so `CI=true` is a second belt-and-braces guard.
- ✅ Measured: bare `npx vitest` in this non-interactive sandbox (with and without a pty
  wrapper, `CI` set and unset) ran once and **exited 0 in ~2.3–2.8 s** — it did not hang. But
  this is stdin/TTY-dependent and must **not** be relied on. Use `vitest run`.
- Belt-and-braces for the pipeline: wrap invocations in `timeout 600 npx vitest run …`.
- If a run finishes but the process lingers, that is a leaked handle, not watch mode. Diagnose
  with `--reporter=hanging-process` and `--detectAsyncLeaks`, and keep `teardownTimeout: 5000`.

---

## 6. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **`-t` matching zero tests exits 0** — the repair loop reads a false green and declares victory with the bug still in. ✅ reproduced. | 🔴 **Critical** | Escape + anchor every name (§5.3), and hard-fail when `numPassedTests + numFailedTests === 0` while failures were expected (guard in §5.5, exit 97). Non-negotiable. |
| 2 | Test names containing `|`, `(`, `[`, `$`, `.`, `*`, `?`, `+`, `^`, `{`, `}`, `\` corrupt the alternation. | 🔴 High | The `escapeRe` in §5.5 covers the full metachar set. Also: avoid regex metacharacters in test names by convention; prefer `it('opens after 5 failures')` over `it('opens after 5 failures (threshold)')`. |
| 3 | Sync fake-timer APIs on `await`-chained code hang until `testTimeout`. ✅ reproduced 3 ways. | 🟠 Medium | Always use `*Async` timer variants when the code under test awaits between timers (§5.6). Keep `testTimeout: 5000` so a mistake costs 5 s, not the pipeline. |
| 4 | Files that fail to **import** produce zero `assertionResults` — a naive extractor sees "no failed tests" and reports green. | 🟠 Medium | Handled: treat `suite.status === 'failed' && assertionResults.length === 0` as a file-level failure and re-run the whole file without `-t` (§5.5). Also cross-check top-level `success === false`. |
| 5 | Running `--coverage` during selective re-runs fails thresholds spuriously. | 🟡 Low | Coverage only on the final full run. |
| 6 | Vitest 5.0 is at `rc.1`; 4.1 is the maintained stable. A later agent might install `@latest` and drift. | 🟡 Low | Pin `vitest@^4.1.10` and `@vitest/coverage-v8@^4.1.10` — **the two versions must match exactly**, they are released in lockstep. |
| 7 | `poolOptions` / `singleFork` copied from a Vitest 3 example silently deprecates. ✅ observed. | 🟡 Low | Use the §5.1 config verbatim; top-level `pool` / `maxWorkers` / `isolate` / `fileParallelism`. |
| 8 | Shell mangling of the `-t` pattern (double quotes eat `$end` and `\(`). | 🟡 Low | Single-quote in bash, or bypass the shell with NUL-separated argv + `mapfile -d ''` (§5.5), or `spawn(cmd, argsArray)`. |
| 9 | `isolate: false` tempting for the 45% speedup; would leak module-level breaker/registry state across files. | 🟡 Low | Keep `isolate: true`. 2 s for 288 tests is already fast enough. |
