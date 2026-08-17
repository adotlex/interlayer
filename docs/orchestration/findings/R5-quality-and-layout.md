# R5 — Quality Gates & Parallel-Safe Repo Layout

**Stream:** Wave 1 / R5
**Date:** 2026-08-17
**Status:** complete — all recommendations empirically verified in a probe project
**Probe dir:** `/tmp/claude-0/-home-user-interlayer/6a57c904-4aac-5f5f-8723-4dc34e098c33/scratchpad/r5-probe`

Everything below marked **[VERIFIED]** was executed, not recalled. Version numbers come from
`npm view`, not memory. The 2026 ecosystem has moved materially since common training data:
**TypeScript `latest` is now 7.0.2 (Go-native)** and **ESLint `latest` is 10.8.1**. Several
widely-assumed defaults are now wrong. Read §1.1 before anything else.

---

## 1. Headline findings that change the default plan

### 1.1 The TypeScript 6 / 7 split is the single biggest trap for Wave 2

| Fact | Evidence |
|---|---|
| `npm view typescript version` → **7.0.2** | [VERIFIED] |
| `typescript` dist-tags: `latest=7.0.2`, `rc=7.0.1-rc`, `beta=6.0.0-beta`, `next=7.1.0-dev.*` | [VERIFIED] |
| Only **two** TS 6 stable releases exist: `6.0.2`, `6.0.3` | [VERIFIED] |
| TS 7.0 is the Go-native rewrite; GA **2026-07-08** | Web |
| **TS 7.0 ships no stable programmatic API.** 7.1 will ship a *new and different* API, not the old JS surface | Web |
| `typescript-eslint@8.67.0` peer range is **`typescript >=4.8.4 <6.1.0`** — it *cannot install* alongside TS 7 | [VERIFIED] |
| TS 6.0 is the designated bridge release; MS publishes `@typescript/typescript6` (a `tsc6` binary) for tools needing the old API | Web |

**Consequence:** a naive `npm i -D typescript` installs 7.0.2 and silently forecloses
typescript-eslint, ts-morph, typedoc, api-extractor and every other compiler-API tool.
The version **must be pinned explicitly**. See §3.

### 1.2 Node 22 runs TypeScript natively — no transpiler, no ts-node, no tsx

[VERIFIED] On this exact runtime (v22.22.2), `node --test src/foo.test.ts` executes a `.ts` file
with **no flags whatsoever**. Type stripping was unflagged in **Node 22.18.0**.

This is the highest-leverage fact in this report: it satisfies C4 (zero runtime deps) and
C5 (offline) for the test path completely, with zero packages.

**Hard coupling it creates:** type stripping is *erase-only*. Non-erasable syntax throws at
runtime. [VERIFIED]:

```
SyntaxError [ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX]: TypeScript enum is not supported in strip-only mode
```

Therefore **`"erasableSyntaxOnly": true` is mandatory** in `tsconfig.json` — it converts that
runtime explosion into a typecheck error. No `enum`, no `namespace`, no constructor parameter
properties, no old-style `declare` merging. Wave 2 agents must be told this explicitly.

### 1.3 `@types/*` are no longer auto-included

[VERIFIED] on **both TS 6.0.3 and TS 7.0.2**, with `@types/node@22.20.1` installed:

```
src/x.test.ts(1,22): error TS2591: Cannot find name 'node:test'.
```

`--traceResolution` shows `Skipping module 'node:test' that looks like an absolute URI`.
Adding `"types": ["node"]` to `compilerOptions` fixes it. This is *not* TS7-specific; it bites
TS6 identically. Omitting it produces a confusing error that will burn agent turns.

### 1.4 `node --test <directory>` is broken — it is not a discovery mode

[VERIFIED] `node --test src/` does **not** scan the directory. It tries to *load* `src` as a
module and fails:

```
Error: Cannot find module '/…/full/src'   (code: MODULE_NOT_FOUND)
# fail 1
```

Valid forms are bare `node --test` (default recursive discovery) or an explicit quoted glob
`node --test 'src/**/*.test.ts'`. **Use the explicit glob** — it is immune to `dist/` being
scanned after a build and it behaves identically whether or not the shell expands it.

### 1.5 Node's built-in coverage cannot see untested files

[VERIFIED] A module that no test ever imports is **absent from the coverage report entirely**,
and the total still reads `all files | 100.00`. Adding `src/registry/uncovered.ts` (never
imported) changed nothing; `--test-coverage-lines=100` still exited **0**.

This is decisive for §8: a coverage percentage from Node's runner is not a safety property.
A unit that ships with zero tests scores 100%.

---

## 2. Linting & formatting

### 2.1 Options considered

Measured in this environment, clean `npm install`, cold cache. [VERIFIED]

| | **Biome 2.5.8** | **ESLint 10.8.1 + typescript-eslint 8.67.0** | **oxlint 1.78.0** |
|---|---|---|---|
| Packages installed | **2** | **87** | **2** |
| `node_modules` size | 70 MB | 44 MB | 18 MB |
| Install time | **1.6 s** | 6.7 s | 1.6 s |
| Formatter included | **yes** | no (needs Prettier → +1 tool, +1 config) | no |
| Import sorting | **yes**, built in | needs plugin | partial |
| Config files needed | **1** (`biome.json`) | 2–3 (`eslint.config.mjs`, `.prettierrc`, `.prettierignore`) | 1 + separate formatter |
| Lint speed (6 files) | **7 ms** | ~1–2 s (TS program build) | ~5 ms |
| Type-aware rules | yes — own inference engine, **no `tsc`** | yes — via TS compiler API | yes — via `tsgolint` |
| **Couples to TS version** | **no** | **yes — blocks TS 7 entirely** | yes (tracks TS 7) |
| Offline after install | yes, single native binary | yes, but 87-package resolution graph | yes |
| Auto-fix determinism | **verified idempotent** | mostly | mostly |

### 2.2 Recommendation: **Biome 2.5.8**, pinned exactly

Rationale, weighted for a fleet of parallel agents:

1. **It does not couple lint to the TypeScript version.** [VERIFIED] `noFloatingPromises` fired
   correctly with *no TypeScript installed in the lint path at all*. This decouples the R5 gate
   from the TS 6/7 mess in §1.1 — the single most valuable property here.
2. **One tool, one config, one command.** ESLint+Prettier is two tools, two configs and a
   well-known conflict surface (`eslint-config-prettier`). Every extra config file is a file
   agents might be tempted to edit.
3. **Auto-fix is idempotent.** [VERIFIED] `biome check --write` run three times over a
   deliberately mangled file produced a byte-identical result each time
   (`md5 67c45b1d…` × 3). Formatting is not a matter of opinion at runtime, so parallel agents
   converge instead of ping-ponging.
4. **87 packages vs 2** directly serves C4 (supply-chain surface) and C5 (offline reliability).
5. **`biome ci` is a purpose-built non-mutating gate.** [VERIFIED] exits `1` on violation,
   `0` when clean, and leaves files byte-identical (`NON-MUTATING=YES`).

`biome check --write` does **format + lint-fix + import-organise in a single pass**, which is
exactly the "unambiguous auto-fixable formatting" the brief asks for.

### 2.3 Two config hazards found and neutralised

**Hazard A — `vcs.useIgnoreFile: true` hard-fails when `.gitignore` is absent.** [VERIFIED]

```
× Biome couldn't find an ignore file in the following folder: …
× Biome exited because the configuration resulted in errors.
```

The whole gate dies with a *configuration* error, not a lint error. Since `files.includes`
already scopes precisely, the ignore-file coupling buys nothing. **Set `vcs.enabled: false`.**

**Hazard B — never let Biome format `package.json`.** [VERIFIED] With `*.json` in
`files.includes`, `biome ci` failed with 4 errors that were **entirely** re-formatting of
`biome.json`, `package.json`, `tsconfig.json`, `tsconfig.build.json`. npm rewrites
`package.json` on `npm install` / `npm pkg set`, so this makes the green gate fail for reasons
with nothing to do with the code — precisely the failure mode the brief warns about.

After scoping `files.includes` to TS only, [VERIFIED]: `npm pkg set description=…` then
`biome ci` → **exit 0**. The gate is now immune to npm's churn.

### 2.4 `biome.json` — literal contents

Verified to parse and run clean (`Checked 6 files in 7ms`, exit 0).

```json
{
  "$schema": "https://biomejs.dev/schemas/2.5.8/schema.json",
  "vcs": { "enabled": false, "clientKind": "git", "useIgnoreFile": false },
  "files": {
    "ignoreUnknown": false,
    "includes": ["src/**/*.ts", "test/**/*.ts"]
  },
  "formatter": {
    "enabled": true,
    "indentStyle": "space",
    "indentWidth": 2,
    "lineWidth": 100,
    "lineEnding": "lf"
  },
  "linter": {
    "enabled": true,
    "domains": { "types": "recommended" },
    "rules": {
      "preset": "recommended",
      "suspicious": {
        "noExplicitAny": "error",
        "noConsole": "error"
      },
      "correctness": {
        "noUnusedVariables": "error",
        "noUnusedImports": "error"
      },
      "style": {
        "noNonNullAssertion": "error",
        "useConst": "error",
        "useImportType": "error",
        "noParameterAssign": "error"
      },
      "nursery": {
        "noFloatingPromises": "error",
        "noMisusedPromises": "error"
      }
    }
  },
  "javascript": {
    "formatter": {
      "quoteStyle": "single",
      "semicolons": "always",
      "trailingCommas": "all",
      "arrowParentheses": "always",
      "bracketSpacing": true
    }
  },
  "assist": { "enabled": true, "actions": { "source": { "organizeImports": "on" } } }
}
```

Notes on non-obvious keys, all confirmed against Biome 2.5.8:

- `rules.preset: "recommended"` — **2.5 syntax**. Older `"recommended": true` is wrong here.
  This was taken from `biome init` output, not guessed.
- `noFloatingPromises` / `noMisusedPromises` live in the **`nursery`** group and the **`types`**
  domain. [VERIFIED] via `biome explain noFloatingPromises` →
  `Diagnostic category: lint/nursery/noFloatingPromises`, `Domains: types`.
  `domains: { "types": "recommended" }` is what activates the type-aware engine.
- `noConsole: "error"` is deliberate for a *library*. If R6 wants a debug logger it belongs
  behind an injected sink, not `console`.

**Nursery caveat:** nursery rules can move or change between Biome minors. This is contained
by pinning `@biomejs/biome` to an exact version. Do not float it during Wave 2.

---

## 3. Type checking

### 3.1 Recommendation: pin **`typescript@6.0.3`** exactly

This is deliberately *not* `latest`. Measured difference on a small project, 3 runs each
[VERIFIED]:

| | per-run `tsc --noEmit` |
|---|---|
| TypeScript 7.0.2 | **0.128 s** |
| TypeScript 6.0.3 | 0.546 s |

TS 7 is ~4× faster. **It does not matter at this repo's scale** — the entire aggregate gate
runs in 1.3 s either way. What does matter:

- **TS 6.0.3 preserves optionality.** If Biome's nursery type-aware rules churn or prove
  insufficient, typescript-eslint remains available as an escape hatch. Choosing TS 7
  *permanently forecloses that* (peer range `<6.1.0`), and 7.1's replacement API is explicitly
  a different API, so the escape hatch does not simply return.
- **Zero ecosystem risk mid-wave.** typedoc, api-extractor, ts-morph all still work. A
  six-agent parallel wave cannot afford a "tool X refuses to install" discovery at hour three.
- The 0.4 s saving is invisible; the blast radius of being wrong is a stalled wave.

Revisit after 1.0, once TS 7.1 ships its stable API and R1's build tool is fixed.

**Because `latest` is 7.0.2, the pin must be exact and the lockfile committed.** `^6.0.3` is
also safe (stays in 6.x) but exact pinning is what `.npmrc` `save-exact=true` enforces.

### 3.2 Typecheck the tests — yes, definitively

Two configs, one gate each:

- **`tsconfig.json`** — includes `src/**/*.ts` *and* `test/**/*.ts`. Used by `npm run typecheck`
  (`tsc --noEmit`). Tests are typechecked. This is not optional: tests are where mock providers
  and fake clocks get wired, and an untyped mock that has drifted from the contract in
  `src/core/` is precisely the bug this catches. It costs nothing (no emit).
- **`tsconfig.build.json`** — extends the above, sets `noEmit: false`, and **excludes**
  `src/**/*.test.ts` and `test/`. Used by `npm run build`.

[VERIFIED] the split produces a clean `dist/` containing no test artefacts.

### 3.3 `tsconfig.json` — literal contents

> **Do not add `rootDir` here.** [VERIFIED] `rootDir: "src"` in the *base* config makes
> `npm run typecheck` fail with `error TS6059: File 'test/support/fake-clock.ts' is not under
> 'rootDir' 'src'`, because this config deliberately includes `test/**`. `rootDir` belongs
> **only** in `tsconfig.build.json`, which excludes `test/`. This exact mistake was caught by
> building a project from these literal contents; the config below is the corrected version.

```json
{
  "compilerOptions": {
    "target": "es2023",
    "lib": ["es2023"],
    "module": "nodenext",
    "moduleResolution": "nodenext",
    "types": ["node"],

    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "noImplicitReturns": true,

    "erasableSyntaxOnly": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "allowImportingTsExtensions": true,
    "rewriteRelativeImportExtensions": true,

    "declaration": true,
    "declarationMap": true,
    "sourceMap": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src/**/*.ts", "test/**/*.ts"]
}
```

Load-bearing options, each verified:

| Option | Why it is non-negotiable |
|---|---|
| `"types": ["node"]` | Without it, `node:test` does not resolve. §1.3 |
| `"erasableSyntaxOnly": true` | Without it, an `enum` typechecks but explodes at test runtime. §1.2 |
| `"rootDir": "src"` — **build config only** | TS 7 **errors** without it when emitting (`TS5011`), so `tsconfig.build.json` sets it. Putting it in the base config breaks `typecheck` with `TS6059` (see box above). [VERIFIED both directions] |
| `allowImportingTsExtensions` + `rewriteRelativeImportExtensions` | Lets source say `import './retry.ts'` (required by Node ESM + type stripping) while `tsc` emits `'./retry.js'`. This is the hinge that makes "no build step for tests" work. |

**`tsconfig.build.json`:**

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": false,
    "rootDir": "src",
    "outDir": "dist"
  },
  "include": ["src/**/*.ts"],
  "exclude": ["src/**/*.test.ts", "test"]
}
```

### 3.4 A `.d.ts` oddity that looks like a bug but is not

[VERIFIED] `rewriteRelativeImportExtensions` rewrites specifiers in emitted **`.js`**
(`'../resilience/retry.ts'` → `'../resilience/retry.js'`) but **leaves `.ts` in the emitted
`.d.ts`**:

```ts
// dist/routing/fallback.d.ts
import type { Provider } from '../core/types.ts';
```

This alarms on sight. It is fine. I built a separate consumer package with plain
`moduleResolution: nodenext` and **no** `allowImportingTsExtensions`, and typechecked against
the built output. Types resolved correctly — the negative test produced real errors
(`Type 'number' is not assignable to type 'string'`) rather than degrading to `any`.
**Do not "fix" this.** Recorded so Wave 2/3 does not waste turns on it.

---

## 4. `package.json` scripts

Every script below was executed and its exit code checked. [VERIFIED] — none hang, none default
to watch mode, all propagate failure.

```json
{
  "scripts": {
    "build": "tsc -p tsconfig.build.json",
    "clean": "node -e \"require('node:fs').rmSync('dist',{recursive:true,force:true})\"",

    "typecheck": "tsc --noEmit",

    "lint": "biome ci .",
    "lint:fix": "biome check --write .",
    "format": "biome format --write .",
    "format:check": "biome format .",

    "test": "npm run test:unit",
    "test:unit": "node --test --test-reporter=spec 'src/**/*.test.ts'",
    "test:file": "node --test --test-reporter=spec",
    "test:name": "node --test --test-reporter=spec 'src/**/*.test.ts' --test-name-pattern",
    "test:tap": "node --test --test-reporter=tap 'src/**/*.test.ts'",
    "test:cov": "node --test --experimental-test-coverage --test-reporter=spec 'src/**/*.test.ts'",

    "check": "npm run lint && npm run typecheck && npm run test:unit"
  }
}
```

Verification run:

```
clean -> exit=0     build -> exit=0     typecheck -> exit=0
lint  -> exit=0     format:check -> 0   test:unit -> exit=0
test:tap -> exit=0  check -> exit=0     (whole gate: 1.317 s)
```

Failure propagation was confirmed too: with an unformatted file present, `lint` → 1 and
`check` → 1.

### 4.1 Running a subset (this is what Wave 3's repair loop needs — C2)

| Goal | Command |
|---|---|
| One file | `npm run test:file -- src/resilience/retry/backoff.test.ts` |
| Several files | `npm run test:file -- src/registry/registry.test.ts src/routing/fallback.test.ts` |
| One test by name | `npm run test:name -- 'backoff doubles'` |
| Name regex | `npm run test:name -- '^circuit breaker opens'` |
| One directory (unit) | `node --test 'src/resilience/retry/**/*.test.ts'` |
| Machine-readable | `npm run test:tap` (TAP 13 on stdout) |

[VERIFIED] `--test-name-pattern='alpha two'` correctly ran only the matching test out of three.
[VERIFIED] single-file and glob invocation both work.

> **Trap for Wave 3's repair loop — a non-matching name pattern exits 0.** [VERIFIED]
> `npm run test:name -- 'zzz-nonexistent'` reports `tests 1 / pass 1 / fail 0` and **exits 0**.
> The "1 test" is the *file itself*, which Node counts as a passing subtest when no test inside
> it matches. A typo'd or stale test name therefore reports **green while running nothing**.
>
> Mitigation for the repair loop: never trust the exit code of a name-filtered run on its own.
> Either (a) re-run with `test:tap` and assert the expected test name appears as an `ok`/`not ok`
> line, or (b) prefer **file-scoped** re-runs (`test:file`), which cannot silently match zero
> tests, and use the name filter only to narrow *within* a known-failing file.

Available runner flags on this Node, confirmed via `node --test --help`:
`--test-name-pattern`, `--test-reporter`, `--test-reporter-destination`, `--test-only`,
`--test-concurrency`, `--test-force-exit`, `--experimental-test-coverage`,
`--test-coverage-lines`.

**Note for R2 (test framework stream):** R2 owns the runner decision. If R2 selects Vitest, only
the `test:*` lines change; `lint`, `typecheck`, `build` and the ownership map are unaffected. My
strong steer is `node:test` — it is the only option that costs **zero packages**, needs **zero
transpiler**, and satisfies C2/C4/C5 outright, and I verified all of C2's selection requirements
work today.

**Ordering of `check` is deliberate:** lint (7 ms) → typecheck (0.5 s) → tests (slowest).
Cheapest signal first, so an agent that broke formatting learns in milliseconds.

**`--test-force-exit` is *not* set by default.** Leaving it off means a leaked timer or open
handle surfaces as a hang, which is a real bug in a resilience library. But a hang stalls an
automated pipeline, so CI wraps the job in `timeout-minutes` (§6) rather than masking it.

---

## 5. Repo hygiene — literal contents

### `.gitignore`

```gitignore
# dependencies
node_modules/

# build output
dist/
*.tsbuildinfo

# test / coverage output
coverage/
.nyc_output/

# logs
*.log
npm-debug.log*
pnpm-debug.log*

# editors & OS
.DS_Store
Thumbs.db
.idea/
.vscode/*
!.vscode/extensions.json

# env
.env
.env.*
!.env.example
```

### `.npmrc`

Genuinely useful here — the first two lines defend against §1.1.

```ini
# Never let a caret silently pull TypeScript 7 (or any other major) into the wave.
save-exact=true

# Fail loudly if the runtime is older than native type stripping (Node 22.18.0).
engine-strict=true

# Quieter, faster, more deterministic installs in an automated pipeline.
fund=false
audit=false
package-lock=true
```

### `.editorconfig`

Values match `biome.json` exactly so the editor and the gate never disagree.

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
indent_style = space
indent_size = 2
insert_final_newline = true
trim_trailing_whitespace = true
max_line_length = 100

[*.md]
trim_trailing_whitespace = false

[*.{json,jsonc,yml,yaml}]
indent_size = 2
```

### `package.json` — the non-script parts

```json
{
  "name": "interlayer",
  "version": "0.1.0",
  "description": "A typed interoperability layer over interchangeable backend providers.",
  "license": "MIT",
  "type": "module",
  "engines": { "node": ">=22.18.0" },
  "packageManager": "npm@10.9.7",
  "files": ["dist"],
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "import": "./dist/index.js",
      "default": "./dist/index.js"
    },
    "./package.json": "./package.json"
  },
  "sideEffects": false,
  "dependencies": {},
  "devDependencies": {
    "@biomejs/biome": "2.5.8",
    "@types/node": "22.20.1",
    "typescript": "6.0.3"
  }
}
```

`"engines.node": ">=22.18.0"` is exact and load-bearing — 22.18.0 is the release that unflagged
type stripping (§1.2). `dependencies` is `{}` and must stay that way (C4).

---

## 6. CI — GitHub Actions

### `.github/workflows/ci.yml`

Action majors confirmed from the canonical READMEs on `main`, not from memory: both are **v7**.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    name: check
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v7
        with:
          node-version: '22.22.2'
          cache: 'npm'

      - name: Install
        run: npm ci

      - name: Lint & format
        run: npm run lint

      - name: Typecheck
        run: npm run typecheck

      - name: Unit tests
        run: npm run test:unit

      - name: Build
        run: npm run build

      - name: Coverage (report only, non-gating)
        run: npm run test:cov
```

Design notes:

- **Steps are separate, not `npm run check`.** Same commands, same order, but GitHub renders
  each as its own collapsible step with its own red X. An agent reading a failed run learns
  *which* gate broke without parsing logs. `npm run check` remains the single local command.
- `node-version: '22.22.2'` pinned to the local runtime. Type stripping behaviour is
  version-sensitive; a floating `22` could drift.
- `cache: 'npm'` keys off `package-lock.json` — hence `package-lock.json` must be committed.
- `timeout-minutes: 10` is the backstop for a leaked-handle hang (§4).
- `concurrency` + `cancel-in-progress` avoids burning minutes on superseded pushes.
- `permissions: contents: read` — least privilege; nothing here writes.
- **No matrix.** One Node, one OS. The brief asks for minimal and fast; a matrix triples cost
  for a library whose floor is already Node 22.

### 6.1 Proxy / offline: where CI and local differ

This sandbox reaches the network through an egress proxy (`HTTPS_PROXY=http://127.0.0.1:40213`,
CA bundle at `/root/.ccr/ca-bundle.crt`). GitHub-hosted runners have ordinary unrestricted
egress. Practical consequences:

1. **`registry.npmjs.org` is in `NO_PROXY` locally** [VERIFIED from env], so npm installs work
   here without proxy config, and will work on runners too. No `.npmrc` proxy settings are
   needed, and none should be committed — a hardcoded `127.0.0.1:40213` would break CI.
2. **Some hosts are blocked locally that are not blocked in CI.** [VERIFIED]
   `devblogs.microsoft.com` returned `EGRESS_BLOCKED` and `api.github.com` returned 403 during
   this research. This affects *research*, not the build — the gate itself makes no network
   calls after install.
3. **`@biomejs/biome` and `typescript` both resolve platform-specific binaries as optional
   deps.** [VERIFIED] each installs as 2 packages locally on linux-x64. On `ubuntu-latest`
   (also linux-x64) resolution is identical. If a future macOS/Windows runner is added, the
   lockfile must contain those optional deps or `npm ci` will fail — another reason to keep the
   matrix at one OS for now.
4. **Everything after `npm ci` is fully offline.** Biome is a native binary, `tsc` is local,
   `node --test` is built in. This satisfies C5: the green/repair loop cannot be flaked by the
   network.

---

## 7. THE FILE-OWNERSHIP MAP

This is the deliverable that protects the Wave 2 run. The governing idea:

> **Ownership is by DIRECTORY, not by file.**

File-level manifests fail in practice because an agent legitimately needs to add a helper or a
second test file, and any file it invents is unlisted and therefore ambiguous. A directory
subtree is a *closed* allocation: an agent may create anything it wants inside its own subtree
and can never collide with a sibling. It also gives the orchestrator a one-line rule to state
and a one-line rule to audit.

### 7.1 Directory tree

`[ORCH-PRE]` = orchestrator writes **before** the wave, read-only thereafter.
`[ORCH-POST]` = orchestrator writes **after** the wave.
`[Un]` = exclusive to that unit, recursively.

```
interlayer/
├── .editorconfig                          [ORCH-PRE]
├── .gitignore                             [ORCH-PRE]
├── .npmrc                                 [ORCH-PRE]
├── biome.json                             [ORCH-PRE]
├── package.json                           [ORCH-PRE]
├── package-lock.json                      [ORCH-PRE]
├── tsconfig.json                          [ORCH-PRE]
├── tsconfig.build.json                    [ORCH-PRE]
├── README.md                              [ORCH-PRE]
│
├── .github/
│   └── workflows/
│       └── ci.yml                         [ORCH-PRE]
│
├── docs/
│   └── orchestration/                     [ORCH]
│
├── src/
│   ├── index.ts                           [ORCH-POST]  ← THE barrel. The one true conflict point.
│   │
│   ├── core/                              [ORCH-PRE]   ← CONTRACTS. Read-only to every unit.
│   │   ├── types.ts                          Provider, Capability, Request/Response
│   │   ├── policy.ts                         Policy / middleware interface (see 7.4)
│   │   ├── errors.ts                         typed error taxonomy
│   │   ├── result.ts                         Result / outcome type
│   │   ├── clock.ts                          Clock port (injected time — C3)
│   │   └── index.ts                          internal re-export of core only
│   │
│   ├── registry/                          [U1]
│   │   ├── index.ts                          unit's public face
│   │   ├── registry.ts
│   │   ├── capability.ts
│   │   ├── registry.test.ts
│   │   └── capability.test.ts
│   │
│   ├── resilience/                        ← NO index.ts here. Deliberate. See 7.3.
│   │   ├── retry/                         [U2]
│   │   │   ├── index.ts
│   │   │   ├── backoff.ts
│   │   │   ├── retry.ts
│   │   │   ├── backoff.test.ts
│   │   │   └── retry.test.ts
│   │   │
│   │   ├── circuit-breaker/               [U3]
│   │   │   ├── index.ts
│   │   │   ├── breaker.ts
│   │   │   ├── state.ts
│   │   │   ├── breaker.test.ts
│   │   │   └── state.test.ts
│   │   │
│   │   ├── rate-limit/                    [U4]
│   │   │   ├── index.ts
│   │   │   ├── token-bucket.ts
│   │   │   └── token-bucket.test.ts
│   │   │
│   │   └── timeout/                       [U5]
│   │       ├── index.ts
│   │       ├── deadline.ts
│   │       ├── abort.ts
│   │       ├── deadline.test.ts
│   │       └── abort.test.ts
│   │
│   └── routing/                           [U6]
│       ├── index.ts
│       ├── fallback.ts
│       ├── selector.ts
│       ├── fallback.test.ts
│       └── selector.test.ts
│
└── test/
    └── support/                           [ORCH-PRE]   ← shared harness. Read-only to units.
        ├── fake-clock.ts                     deterministic time (C3)
        ├── seeded-random.ts                  deterministic jitter (C3)
        ├── fake-provider.ts                  scriptable Provider double
        └── index.ts
```

### 7.2 Ownership table

| Unit | Scope | Owns exclusively (recursive) | May **read**, never write | Must never touch |
|---|---|---|---|---|
| **ORCH-PRE** | contracts + config | all root config, `.github/**`, `src/core/**`, `test/support/**` | — | — |
| **U1** | provider registry, capability negotiation | `src/registry/**` | `src/core/**`, `test/support/**` | any other `src/**`, all root config, `src/index.ts` |
| **U2** | retry, backoff, jitter | `src/resilience/retry/**` | `src/core/**`, `test/support/**` | ” |
| **U3** | circuit breaker state machine | `src/resilience/circuit-breaker/**` | `src/core/**`, `test/support/**` | ” |
| **U4** | rate limiting (token bucket) | `src/resilience/rate-limit/**` | `src/core/**`, `test/support/**` | ” |
| **U5** | timeout, deadline, AbortSignal | `src/resilience/timeout/**` | `src/core/**`, `test/support/**` | ” |
| **U6** | routing, fallback chain, selection | `src/routing/**` | `src/core/**`, `test/support/**` | ” |
| **ORCH-POST** | barrel + wiring | `src/index.ts` | everything | — |

Six units, six disjoint subtrees, satisfying C1. Tests are co-located inside each subtree, so
**test ownership is disjoint by construction** — no separate test-ownership rule is needed.

### 7.3 Why there is no `src/resilience/index.ts`

An intermediate barrel at `src/resilience/index.ts` would need contributions from U2, U3, U4
and U5 — a guaranteed four-way write conflict on one file, and exactly the failure the brief
warns about. It is omitted on purpose.

Each unit owns an `index.ts` **inside its own subtree**, which is safe (exclusive) and gives the
orchestrator a stable, predictable import target. `src/index.ts` then reaches straight through:

```ts
// src/index.ts — ORCHESTRATOR ONLY, written after the wave
export * from './core/index.ts';
export * from './registry/index.ts';
export * from './resilience/retry/index.ts';
export * from './resilience/circuit-breaker/index.ts';
export * from './resilience/rate-limit/index.ts';
export * from './resilience/timeout/index.ts';
export * from './routing/index.ts';
```

### 7.4 The rule that actually makes the units independent

**No unit may import another unit.** A unit imports only from `src/core/**`, `test/support/**`,
its own subtree, and `node:*` builtins.

The obvious tension is U6 (routing/fallback), which conceptually wants to compose retry and
circuit-breaking. Resolve it through the contract, not through a dependency:

- `src/core/policy.ts` (orchestrator, pre-wave) defines the `Policy` / middleware interface.
- U2–U5 each export a **factory returning a `Policy`**. They never reference each other.
- U6 operates on the **`Policy` interface only** — it composes shapes, not implementations.
- Concrete wiring happens in `src/index.ts` after the wave.

This is what converts a dependency graph into six genuinely parallel units. **R3 must be told
that `src/core/policy.ts` is a required contract file**, because the parallelism depends on it
existing before the wave starts.

### 7.5 Rules for Wave 2 agents (give these verbatim)

1. **You own exactly one directory.** Create, edit and delete anything inside it. Touch nothing
   outside it.
2. **`src/core/**` and `test/support/**` are READ-ONLY.** They are your contract. If one is
   wrong or insufficient, **stop and report to the orchestrator** — do not edit it, and do not
   work around it with a local copy of a core type.
3. **Never edit `src/index.ts`.** The barrel is written by the orchestrator after the wave.
   Export your unit's public surface from *your own* `index.ts` and stop there.
4. **Never edit `package.json`, `package-lock.json`, `tsconfig*.json`, `biome.json`, `.npmrc`,
   `.gitignore`, `.editorconfig`, or anything under `.github/`.**
5. **Never run `npm install`, `npm ci`, `npm i <pkg>`, or `npm pkg set`.** Dependencies are
   fixed and the tree is already installed. This library ships **zero** runtime dependencies
   (C4); if you believe you need one, you are solving the wrong problem — report instead.
6. **Never run any `git` command.** No `add`, no `commit`, no `checkout`, no `stash`. The
   orchestrator owns version control. A `git add -A` from one agent captures half-finished work
   from five others.
7. **Do not import from another unit.** `src/core/**`, `test/support/**`, your own subtree,
   `node:*`. Nothing else.
8. **Do not create shared helpers outside your subtree.** If two units need the same small
   utility, duplicate it. Duplication is cheap; a contested file corrupts the run.
9. **Co-locate your tests** as `*.test.ts` beside the code, inside your subtree.
10. **Determinism is mandatory (C3).** No `Date.now()`, no `setTimeout` for real waiting, no
    `Math.random()`. Take the `Clock` from `src/core/clock.ts` and the fakes from
    `test/support/`. A test that sleeps is a broken test.
11. **No `enum`, `namespace`, or constructor parameter properties.** `erasableSyntaxOnly` is on
    because Node strips types without transpiling; these throw at runtime (§1.2).
12. **Import relative paths with the `.ts` extension** — `import { x } from './retry.ts'`.
    Required by Node ESM; `tsc` rewrites it to `.js` on build (§3.3).
13. **Before you finish, run `npm run lint:fix` then `npm run check`.** `lint:fix` is idempotent
    and only rewrites files you own. Report the result honestly; do not disable a rule to go
    green. Never add a `biome-ignore` to a file you do not own.

### 7.6 Cheap structural audit for the orchestrator

Between the wave and writing the barrel — this catches the failure mode §1.5 hides:

```bash
# 1. Every unit produced a public face.
for d in src/registry src/resilience/retry src/resilience/circuit-breaker \
         src/resilience/rate-limit src/resilience/timeout src/routing; do
  [ -f "$d/index.ts" ] || echo "MISSING BARREL: $d"
  ls "$d"/*.test.ts >/dev/null 2>&1 || echo "NO TESTS: $d"
done

# 2. Nobody wrote outside their lane (should print nothing).
git status --porcelain -- src/core test/support package.json tsconfig.json biome.json

# 3. No cross-unit imports (should print nothing).
grep -rnE "from '\.\./\.\./(registry|routing)" src/ || true
grep -rnE "from '\.\./(retry|circuit-breaker|rate-limit|timeout)/" src/ || true
```

Check 1 is the real coverage gate — see §8.

---

## 8. Coverage: measure, do **not** gate

**Recommendation: collect coverage and print it; do not fail the build on a percentage in the
first build.**

Three reasons, the third decisive:

1. **A threshold failure blocks the green gate for reasons unrelated to correctness.** On a
   greenfield repo the true coverage number is unknown until the code exists. Any number picked
   now is arbitrary, and an arbitrary number that turns CI red teaches agents to game it.
2. **It creates a perverse incentive under parallelism.** An agent that cannot reach 85% will
   write assertion-free tests that execute lines. That is worse than no gate.
3. **[VERIFIED] Node's coverage cannot see untested files at all.** A module no test imports is
   *absent from the report*, and the total still reads `100.00`. `--test-coverage-lines=100`
   exited **0** with a wholly untested file in the tree. A percentage gate here is not merely
   arbitrary — it is **unsound**. It would pass a unit that shipped with zero tests.

**Gate on structure instead, which is sound and cheap:** §7.6 check 1 asserts every unit
directory contains at least one `*.test.ts`. That catches the real risk (a unit shipping
untested) which the percentage provably does not.

Keep `npm run test:cov` in CI as a **reporting** step so the number is visible and trending.
Once the API stabilises after 1.0, turn on a real threshold — the flag is ready:

```
node --test --experimental-test-coverage --test-coverage-lines=80 'src/**/*.test.ts'
```

[VERIFIED] the flag works and exits 0/1 correctly *for files that were loaded*; its blind spot
is only unloaded files.

---

## 9. Rejected, and why (so Wave 2 does not relitigate)

| Rejected | Why |
|---|---|
| **ESLint 10 + typescript-eslint** | 87 packages vs 2; ~4× slower install; needs Prettier + `eslint-config-prettier` (2–3 configs); and its peer range `typescript <6.1.0` **hard-blocks TS 7 forever**, coupling the lint gate to the compiler version. Kept as a documented escape hatch — which is precisely why TS is pinned to 6.0.3. |
| **oxlint** | Smallest and fastest, and its `tsgolint` type-aware engine is genuinely strong. But **it has no formatter**, so it still needs Prettier — reintroducing the second tool and second config that Biome removes. Revisit if Biome's inference proves insufficient. |
| **Prettier (standalone)** | Redundant. Biome's formatter is Prettier-compatible in style and [VERIFIED] idempotent, in the same binary. |
| **`typescript@7.0.2`** | ~4× faster typecheck (0.128 s vs 0.546 s) — **irrelevant** at 1.3 s total gate time. Cost: permanently forecloses typescript-eslint and every compiler-API tool (typedoc, api-extractor, ts-morph) until 7.1 ships a *different* API. Wrong trade during a parallel wave. Revisit post-1.0. |
| **`tsx` / `ts-node` / `swc` to run tests** | Unnecessary. [VERIFIED] Node 22.22 executes `.ts` tests with no flags and no packages. |
| **`node --test <dir>` for discovery** | [VERIFIED] broken — treats the directory as a module and fails with `MODULE_NOT_FOUND`. Use the explicit glob. |
| **A `src/resilience/index.ts` intermediate barrel** | Guaranteed 4-way write conflict between U2–U5. §7.3. |
| **File-level (rather than directory-level) ownership** | Cannot express "agent may add a helper it hasn't thought of yet". Every unanticipated file becomes an ownership question mid-run. |
| **Biome formatting `*.json`** | [VERIFIED] makes the gate fail on `package.json` churn from `npm install`. §2.3. |
| **`vcs.useIgnoreFile: true`** | [VERIFIED] hard config failure if `.gitignore` is missing; buys nothing given explicit `files.includes`. |
| **Coverage percentage threshold in build 1** | [VERIFIED] unsound — untested files are invisible to Node's reporter and score 100%. §8. |
| **CI matrix (multi-OS / multi-Node)** | 3× cost for a Node-22-floor library. Also risks `npm ci` failures on platform-specific optional binaries absent from the lockfile. |
| **Husky / lint-staged pre-commit hooks** | Agents are told never to run `git`. A commit hook is dead weight and an extra dependency (C4). |
| **pnpm** | Both work; npm is one less moving part for a single-package repo, and `setup-node`'s `cache: 'npm'` needs no extra action step. |

---

## 10. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | An agent runs `npm i -D typescript` and pulls **7.0.2**, silently changing compiler behaviour and blocking any future typescript-eslint | **High** | Rule 5 forbids installs; `.npmrc` `save-exact=true`; committed lockfile; `npm ci` in CI |
| R2 | Biome's `noFloatingPromises` / `noMisusedPromises` are **nursery** rules and may move or change semantics between minors | Medium | Exact pin `@biomejs/biome: "2.5.8"`. Do not float during Wave 2. If they vanish, `tsc --noEmit` still holds the type line |
| R3 | Biome's inference engine is not `tsc`; it will miss type-aware cases typescript-eslint would catch | Medium | `typecheck` is a *separate* gate and is the real type authority. TS pinned at 6.0.3 keeps typescript-eslint addable |
| R4 | An agent writes an `enum` → typechecks fine locally under a stale config, explodes at test runtime | Medium | `erasableSyntaxOnly: true` (§3.3) turns it into a typecheck error; Rule 11 states it |
| R5 | Barrel conflict — an agent "helpfully" edits `src/index.ts` | **High** | Rules 3 and 6; orchestrator writes the barrel post-wave; audit §7.6 check 2 |
| R6 | An agent edits `src/core/**` to unblock itself, breaking the other five units | **High** | Rule 2 (read-only, escalate instead); audit §7.6 check 2 catches it before the barrel is written |
| R7 | Cross-unit imports appear, silently serialising the wave | Medium | Rule 7; audit §7.6 check 3 |
| R8 | `src/core/policy.ts` is under-specified, so U6 cannot compose without importing U2–U5 | **High** | R3 must land the `Policy` interface **before** the wave. This is a cross-stream dependency — flag to orchestrator |
| R9 | A leaked timer/handle makes `node --test` hang and stalls the pipeline | Medium | `timeout-minutes: 10` in CI; `--test-force-exit` available as a last resort but deliberately unset so the bug stays visible |
| R10 | `actions/checkout@v7` / `setup-node@v7` unavailable on a self-hosted runner | Low | Confirmed from canonical READMEs on `main`. `@v5`/`@v4` remain valid fallbacks |
| R11 | Coverage gate added prematurely and blocks a correct build | Medium | §8 — report only; gate on structure (§7.6) not percentage |
| R12 | Wave 2 adds a doc tool (typedoc/api-extractor) that needs the TS compiler API | Low | Already mitigated by pinning TS 6.0.3 — this is the main reason for that pin |
| R13 | R2 chooses Vitest, contradicting the `node:test` scripts here | Low | Only the `test:*` lines change; `lint`/`typecheck`/`build`/CI/ownership map are unaffected |
| R14 | **Wave 3 re-runs a failed test by name, mistypes it, and gets a false green** | **High** | [VERIFIED] a non-matching `--test-name-pattern` exits 0 with a phantom pass. Use file-scoped re-runs, or assert the test name in TAP output. See box in §4.1 |
| R15 | `rootDir` placed in the base `tsconfig.json`, breaking `typecheck` with `TS6059` | Medium | [VERIFIED] and corrected in §3.3 — `rootDir` lives only in `tsconfig.build.json` |

---

## 11. Cross-stream notes for the orchestrator

- **→ R3 (architecture):** `src/core/policy.ts` must define the `Policy` / middleware interface,
  and `src/core/clock.ts` the injected `Clock`. Both are **pre-wave contract files**. Unit
  independence (§7.4) and determinism (C3) both depend on them existing before the wave starts.
- **→ R2 (test framework):** everything C2 requires is [VERIFIED] working in `node:test` today —
  per-file selection, `--test-name-pattern`, TAP output, coverage. Zero packages. Recommend
  adopting it; note the two traps in §1.4 and §1.5.
- **→ R1 (build/packaging):** the TS 6.0.3 pin (§3.1) is the load-bearing constraint. If R1
  proposes any tool that embeds the TypeScript compiler API, TS 7 is off the table permanently,
  not just for now. Plain `tsc -p tsconfig.build.json` is [VERIFIED] to produce correct
  `dist/` + `.d.ts`, including cross-package type resolution (§3.4).
- **Orchestrator pre-wave checklist:** write all root config, `.github/workflows/ci.yml`,
  `src/core/**`, `test/support/**`; run `npm install` once; commit the lockfile; confirm
  `npm run check` is green on the empty skeleton **before** dispatching six agents into it.
