# R1 — Runtime, Language, Build & Packaging

**Stream:** Wave 1 / R1
**Date:** 2026-08-17
**Status:** complete — every recommendation below was executed and verified locally on this machine (Node v22.22.2, npm 10.9.7, pnpm 10.33.0). Verification log in §7.

---

## 1. Options considered

### 1.1 Language / runtime

| Criterion (weight) | TypeScript / Node 22 | Go 1.24 | Rust 1.94 | Python 3.11 |
|---|---|---|---|---|
| Type expressiveness for a *capability-negotiated provider surface* (discriminated unions, mapped/conditional/template-literal types, `satisfies`) | **Best in class** | Weak — no sum types, no conditional/mapped types, generics cannot narrow a method surface by declared capability | Excellent (traits, enums, assoc. types) but no structural/mapped types | Weakest — `typing` is bolt-on, erased at runtime |
| Runtime dependency count achievable (C4) | **0** (stdlib covers timers, AbortSignal, crypto, test) | 0 | Needs an async runtime (`tokio`) for any real async provider abstraction | 0 for stdlib, but `pytest` needed for C2 |
| Test selection granularity (C2) | `node --test --test-name-pattern` + per-file paths — **verified** | `go test -run '^TestX$' ./pkg` | `cargo test -- --exact name` | `pytest path::test` |
| Deterministic tests without sleeps (C3) | `node:test` `mock.timers`, injectable `AbortSignal`/clock | `context` + fake clock (hand-rolled) | `tokio::time::pause` (needs tokio) | `freezegun`/monkeypatch (extra dep) |
| Build time in an agent repair loop | ~0.2–0.7 s typecheck; **0 s for tests** (Node runs `.ts` directly) | ~1–3 s | 10–60 s cold, slow incremental — hostile to a repair loop | n/a |
| File-disjoint parallel authoring (C1) | One file = one module, no shared registry file needed | Same, but `package` granularity is coarser | Same, but `mod.rs`/`lib.rs` is a shared file all agents must edit | Same, but `__init__.py` is a shared file |
| Ecosystem fit — where provider SDKs actually live | **Dominant** | Partial | Sparse | Strong |
| Publishable artifact ergonomics | npm + `.d.ts` | Go modules | crates.io | PyPI |

### 1.2 Module format

| Option | Node 22 consumers | CJS consumers | Package size | Correctness risk |
|---|---|---|---|---|
| **ESM-only** | native | `require()` works — `require(esm)` unflagged since 22.12.0, stable since 25.4.0 (**verified: named exports came through**) | 1× | lowest — one graph, one set of semantics |
| Dual ESM+CJS | native | native | ~2× | **dual-package hazard**: two module instances → two circuit-breaker state tables, two rate-limit buckets. Fatal for a stateful resilience library |
| CJS-only | works | works | 1× | no top-level await, no `import.meta`, dead end |

### 1.3 Build / packaging tool

Install cost measured on this machine (`npm i -D <tool>` into an empty project):

| Tool | Version | Transitive packages | `node_modules` | Emits `.d.ts`? | Verdict |
|---|---|---|---|---|---|
| **`tsc`** | 6.0.3 | **1** | 24 MB | **yes, natively** | **chosen** |
| `rollup` | 4.62.4 | 1 | 6.3 MB | no — needs `rollup-plugin-dts` + `typescript` anyway | rejected |
| `esbuild` | 0.28.2 | 1 | 12 MB | **no** — cannot emit declarations at all | rejected |
| `tsdown` | 0.22.14 | 24 | 23 MB | yes (via rolldown + oxc) | rejected |
| `tsup` | 8.5.1 | 40 | 25 MB | yes | rejected |
| `unbuild` | 3.6.1 | **111** | **72 MB** | yes | rejected |

`tsc` is a strict subset of everyone else's install cost *and* it is the only option with zero tools between source and output.

### 1.4 TypeScript major version — the one genuinely contested decision

| | TypeScript 6.0.3 | TypeScript 7.0.2 (`latest`, GA 2026‑07‑08) |
|---|---|---|
| Compiler | JS (last of the line) | Go native, ~10× faster |
| Emitted `dist/` with our config | — | **byte-identical to TS 6** (`diff -r` verified) |
| All flags we need (`erasableSyntaxOnly`, `rewriteRelativeImportExtensions`, `isolatedDeclarations`, `module: nodenext`) | present | present |
| Programmatic API | full, stable | **none stable** — package exports only `typescript/unstable/*`; `require("typescript")` yields 2 keys |
| `typescript-eslint@8.67.0` | ✅ peer `>=4.8.4 <6.1.0` | ❌ **excluded by peer range** |
| Install footprint | 24 MB | 3.6 MB wrapper + 27 MB platform binary = 30.6 MB, delivered via 20 platform `optionalDependencies` |
| Typecheck, toy project | 0.67 s | 0.19 s |

### 1.5 Relative-import extension style — decides whether tests need a build step

| Source writes | `node src/x.ts` runs? | `tsc` emit | Emitted `.d.ts` | Consumer TS |
|---|---|---|---|---|
| `./y.js` (classic) | ❌ `ERR_MODULE_NOT_FOUND` — Node's type-stripping does **not** remap `.js`→`.ts` | `./y.js` | `./y.js` | ✅ |
| **`./y.ts`** + `allowImportingTsExtensions` + `rewriteRelativeImportExtensions` | ✅ | **`./y.js`** (rewritten) | keeps `./y.ts` | ✅ **verified on TS 5.7, 5.8, 5.9, 6.0, 7.0** |

### 1.6 Package manager

| | npm 10.9.7 | pnpm 10.33.0 |
|---|---|---|
| Warm install (4 devDeps) | 2.46 s | 1.85 s |
| Offline reinstall after `rm -rf node_modules` | `npm ci --offline` → 1.48 s | `pnpm install --offline --frozen-lockfile` → **0.58 s** |
| Lockfile drift detection | `npm ci` → `EUSAGE`, exit 1 | `ERR_PNPM_OUTDATED_LOCKFILE`, exit 1 |
| Dependency lifecycle scripts | run by default | **blocked by default** (allow-list) |
| Store survives `node_modules` deletion | `~/.npm/_cacache` | content-addressed store, hard-linked |
| Setup cost | zero (ships with Node) | already installed globally at 10.33.0 |

---

## 2. Recommendation

| # | Question | Recommendation |
|---|---|---|
| 1 | Language / runtime | **TypeScript on Node 22** |
| 2 | Module format | **ESM-only** (`"type": "module"`, no CJS build) |
| 3 | Build / packaging | **plain `tsc`** — no bundler, no `tsup`, no `rollup` |
| 4 | `tsconfig.json` | two files: strict base (`noEmit`) + `tsconfig.build.json` — literal contents in §5.2 |
| 5 | `package.json` | literal contents in §5.1 |
| 6 | Package manager | **pnpm 10.33.0**, `--frozen-lockfile` everywhere |
| 7 | Node floor | **`>=22.12.0`**; ES2024 built-ins are safe, ES2025 is **not** |

Plus three decisions that fall out of the above and are equally binding on Wave 2:

| # | Decision | Why it is non-negotiable |
|---|---|---|
| 8 | **Relative imports carry the `.ts` extension** (`import { x } from "./core/retry.ts"`) | The only style under which `node --test test/**/*.test.ts` runs the real source with **zero** transform tooling |
| 9 | **TypeScript pinned to `6.0.3`**, not `7.x` | TS 7 emits an identical `dist/` but has no stable API, so `typescript-eslint` cannot run. Upgrade is a one-line change once TS 7.1 ships the API |
| 10 | **Tests run on `.ts` source; nothing in the test loop reads `dist/`** | No build step between edit and test → the Wave 3 repair loop is a single `node --test` invocation |

---

## 3. Rationale, tied to C1–C5

**C1 — file-disjoint parallel ownership.**
`tsc` compiles one input file to one output file; no bundler merges modules, so a module's file set is exactly its author's file set. ESM-only means no per-file `.cjs`/`.mjs` twin to fight over. `isolatedDeclarations` forces every exported symbol to carry an explicit type annotation, which turns each module boundary into a written contract rather than an inferred one — agent A can no longer silently change agent B's public type by editing a function body. The residual C1 hazard is *not* source files: it is `package.json`, `pnpm-lock.yaml` and the root barrel `src/index.ts` (see §6, R‑1/R‑2).

**C2 — re-run only failed tests.**
`node --test` accepts explicit file paths *and* `--test-name-pattern`, both verified. Because tests import `../src/foo.ts` directly, a failing test file is runnable in isolation with no build: `node --test test/retry.test.ts`. Stack frames point at the `.ts` file with correct line **and column** (`test/math.test.ts:6:43` observed) because Node's type-stripping pads rather than reflows. Detailed runner design is R2's call; R1 only guarantees the runtime supports it.

**C3 — deterministic tests.**
Zero network by construction (zero runtime deps → nothing to phone home). `node:test` ships `mock.timers`; `AbortSignal.timeout`/`AbortSignal.any` are native, so timeout and cancellation logic can be driven by a mocked clock instead of `setTimeout` sleeps. No transform layer means no cache, no watcher, no ordering nondeterminism between runs.

**C4 — zero runtime dependencies.**
The published `package.json` has **no** `dependencies` block at all — verified by `npm pack` + `publint --strict` ("All good!") + `attw --profile esm-only` (all green). Everything the library needs (timers, `AbortSignal`, `structuredClone`, `crypto.randomUUID`, `Error.cause`) is a Node 22 built-in. Choosing `tsc` over any bundler keeps devDependencies at 1 transitive package for the build.

**C5 — offline after install.**
`pnpm install --offline --frozen-lockfile` completes in 0.58 s from the content-addressed store — verified after deleting `node_modules`. `tsc` reads only local files. `node --test` reads only local files. The only tools that could reach the network are the *publish-time* validators (`publint`, `attw`), and both are installed as devDependencies rather than invoked through `npx`, so they too are offline-clean.

**Why not Go**, which also satisfies C2–C5 cleanly: the product hypothesis is a *typed* interoperability layer. The value is in the type system expressing "provider P advertises capabilities {streaming, batching}; the client surface narrows accordingly, and a fallback chain is only well-typed if every member satisfies the requested capability set." That is mapped types + conditional types + discriminated unions. Go cannot express it; consumers would get `any`-equivalent ergonomics and runtime asserts.

**Why not Rust**: the type system is good enough, but every meaningful async provider abstraction pulls in `tokio` — a large runtime dependency, in direct tension with C4 — and cold compile times of tens of seconds are hostile to a machine-driven repair loop (C2).

**Why not Python**: types are erased and advisory; "typed interoperability layer" would be aspirational rather than enforced.

---

## 4. Rejected, and why — do not revisit

| Rejected | Reason |
|---|---|
| **Dual ESM+CJS build** | The dual-package hazard is not theoretical here. A circuit breaker and a rate limiter are *stateful singletons*; loading the package twice (once as ESM, once as CJS) gives two independent breaker state tables that silently disagree. `require(esm)` is unflagged from 22.12.0 and stable from 25.4.0, so dual buys nothing. Verified: `require("interlayer")` from a `.cjs` file returned all named exports. |
| **CJS-only** | No top-level await, no `import.meta`, and TS's CJS emit is the legacy path. |
| **`tsup` / `unbuild` / `tsdown` / `rollup` / `esbuild`** | 24–111 extra packages and 23–72 MB to produce output `tsc` already produces. Bundling actively *hurts* here: it collapses the 1:1 file mapping that C1 depends on, and `sideEffects: false` + per-file ESM already gives consumers full tree-shaking. `esbuild` additionally cannot emit `.d.ts` at all. |
| **TypeScript 7.0.2 as the pinned compiler** | `typescript-eslint@8.67.0` declares peer `typescript: ">=4.8.4 <6.1.0"`. TS 7 ships no stable programmatic API (only `typescript/unstable/*`); Microsoft targets TS 7.1. The workaround (`"typescript": "npm:@typescript/typescript6@^6.0.0"` + `"typescript-7": "npm:typescript@^7.0.2"`) installs **two** compilers (55 MB) and creates two sources of truth for type errors — poison for an automated repair loop. TS 7's only benefit at ~2 kLOC is 0.5 s per typecheck. |
| **`.js` extensions in relative imports** | Verified failure: `node a.ts` where `a.ts` contains `import { v } from "./dep.js"` → `ERR_MODULE_NOT_FOUND`. Node's strip-types does not remap extensions. Choosing `.js` forces a build (or `tsx`) into the test loop, costing C3 determinism and C5 simplicity. |
| **`tsx` / `ts-node` / `swc` as a test-time transform** | Redundant — Node 22.22.2 runs `.ts` with **no flag at all** (verified). Adds a dependency, a cache, and a second parser whose errors differ from `tsc`'s. |
| **`target: "esnext"`** (the `tsc --init` default) | Would let `using`/`await using` through, and Node 22's V8 rejects that syntax (verified: `SyntaxError` at `using r = new R()`). `esnext` also moves under us between TS releases — non-deterministic emit across a version bump. |
| **`lib: ["es2025"]`** | Node 22.22.2 lacks `Promise.try`, `Float16Array`, `RegExp.escape`, `Error.isError` (all probed and `undefined`). ES2025 lib would type them as available and fail at runtime. |
| **npm** | Not wrong, and `npm ci --offline` works. pnpm is 2.5× faster on the offline path, blocks dependency install scripts by default, and is already present at 10.33.0. Marginal call; if pnpm ever misbehaves, `npm ci` is a drop-in fallback with no source changes. |
| **`corepack enable`** | With `packageManager` pinned, corepack will try to *download* the pinned pnpm and verify its hash — a network call that breaks C5. pnpm is already installed globally at exactly 10.33.0; leave corepack disabled. |
| **Subpath exports** (`interlayer/resilience`, …) at v0.1 | Each subpath multiplies the `attw`/`publint` surface and hands every Wave 2 agent a reason to edit `package.json` (see R‑1). Single root entry + `./package.json`. Revisit after v1. |

---

## 5. Concrete specifics

### 5.1 `package.json` — literal, validated

`publint --strict` → *All good!* · `attw --profile esm-only` → all four resolution modes green.

```json
{
  "name": "interlayer",
  "version": "0.1.0",
  "description": "A typed interoperability layer: one stable interface over interchangeable backend providers, with retry, timeout, circuit breaking, rate limiting and routing built in.",
  "license": "MIT",
  "type": "module",
  "main": "./dist/index.js",
  "types": "./dist/index.d.ts",
  "exports": {
    ".": {
      "types": "./dist/index.d.ts",
      "default": "./dist/index.js"
    },
    "./package.json": "./package.json"
  },
  "files": [
    "dist",
    "src"
  ],
  "sideEffects": false,
  "engines": {
    "node": ">=22.12.0"
  },
  "packageManager": "pnpm@10.33.0",
  "publishConfig": {
    "access": "public",
    "provenance": true
  },
  "scripts": {
    "build": "tsc -p tsconfig.build.json",
    "clean": "rm -rf dist node_modules/.cache",
    "typecheck": "tsc -p tsconfig.json",
    "test": "node --test \"test/**/*.test.ts\"",
    "test:cov": "node --test --experimental-test-coverage \"test/**/*.test.ts\"",
    "check:pkg": "publint --strict && attw --pack . --profile esm-only",
    "verify": "pnpm run typecheck && pnpm run test && pnpm run build && pnpm run check:pkg"
  },
  "devDependencies": {
    "@arethetypeswrong/cli": "0.18.5",
    "@types/node": "22.20.1",
    "publint": "0.3.23",
    "typescript": "6.0.3"
  }
}
```

Field-by-field justification:

| Field | Value | Why |
|---|---|---|
| `type` | `"module"` | ESM-only. Every `.ts`/`.js` in the repo is a module. |
| `main` | `"./dist/index.js"` | Costs nothing and turns `attw`'s `node10` row from grey to green. Harmless in 2026 — anything that reads `main` and cannot read `exports` is also old enough that it will not see this package. |
| `types` | `"./dist/index.d.ts"` | Top-level fallback for pre-`exports` type resolution. |
| `exports["."]` | `types` **first**, then `default` | Condition order is significant; `types` must precede. No `import`/`require` split — a single `default` is correct for ESM-only and lets `require(esm)` do its job. |
| `exports["./package.json"]` | self | Several tools (including `publint` and bundler plugins) read it; without this export it is unreachable. |
| `files` | `["dist", "src"]` | `src` **must** ship: `dist/index.d.ts.map` has `"sources": ["../src/index.ts"]`, so go-to-definition and sourcemap navigation break without it. Cost is a few KB of text, zero runtime cost. |
| `sideEffects` | `false` | True by construction and enforced by the no-top-level-side-effects rule (§6, R‑4). Enables full tree-shaking despite per-file emit. |
| `engines.node` | `">=22.12.0"` | 22.12.0 is the first release with `require(esm)` unflagged — the exact floor at which ESM-only is safe for CJS consumers. |
| `dependencies` | **absent** | C4. |

### 5.2 `tsconfig.json` — literal, verified on both TS 6.0.3 and 7.0.2

Base config: typechecks `src` **and** `test`, emits nothing.

```json
{
  "compilerOptions": {
    // ── Environment ──────────────────────────────────────────────
    "target": "es2024",
    "lib": ["es2024"],
    "types": ["node"],
    "moduleDetection": "force",

    // ── Modules ──────────────────────────────────────────────────
    "module": "nodenext",
    "moduleResolution": "nodenext",
    "allowImportingTsExtensions": true,
    "rewriteRelativeImportExtensions": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "erasableSyntaxOnly": true,
    "isolatedDeclarations": true,

    // ── Strictness ───────────────────────────────────────────────
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "noPropertyAccessFromIndexSignature": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noUncheckedSideEffectImports": true,
    "useUnknownInCatchVariables": true,
    "forceConsistentCasingInFileNames": true,

    // ── Emit ─────────────────────────────────────────────────────
    "declaration": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src/**/*.ts", "test/**/*.ts"]
}
```

> `declaration: true` alongside `noEmit: true` is required — `isolatedDeclarations` errors with **TS5069** without it. The combination is legal and emits nothing; verified on 6.0.3 and 7.0.2.

`tsconfig.build.json` — the only config that writes files:

```json
{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": false,
    "rootDir": "./src",
    "outDir": "./dist",
    "declarationMap": true,
    "sourceMap": true,
    "removeComments": false,
    "noEmitOnError": true,
    "incremental": true,
    "tsBuildInfoFile": "./node_modules/.cache/tsbuild/build.tsbuildinfo"
  },
  "include": ["src/**/*.ts"]
}
```

**Verified emit** for `import { ok } from "./core/result.ts"`:

```js
// dist/index.js
export { ok, err } from "./core/result.js";   // ← .ts rewritten to .js
```
```ts
// dist/index.d.ts
export { ok, err } from "./core/result.ts";   // ← .ts retained (see note)
```

The `.d.ts` keeping `.ts` specifiers is `rewriteRelativeImportExtensions`' documented behaviour and it is **safe**: TypeScript resolves `./core/result.ts` inside a declaration file to `./core/result.d.ts`. Confirmed green on consumer TypeScript **5.7.3, 5.8.3, 5.9.3, 6.0.3 and 7.0.2**, and `publint --strict` raises nothing. TS ≤ 5.6 is therefore the effective consumer floor — acceptable, since `rewriteRelativeImportExtensions` itself only exists from 5.7.

#### Which strict flags hurt, and whether they earn it

| Flag | Pain | Verdict |
|---|---|---|
| `strict` | none | **keep** |
| `noUncheckedIndexedAccess` | **high** — every `arr[i]` and `record[k]` becomes `T \| undefined` | **keep.** This library indexes constantly (provider tables, breaker state per key, token buckets per key). Mitigation to put in the Wave 2 guide: prefer `Map<K,V>` over `Record<K,V>` (`.get()` already returns `V \| undefined`, so the flag costs nothing), and use `.at()` + explicit guards for arrays. |
| `exactOptionalPropertyTypes` | **high** — `{ timeoutMs?: number }` rejects `{ timeoutMs: undefined }`, which breaks `{...defaults, ...overrides}` config merging | **keep**, with a mandatory convention: *input option* types declare `readonly timeoutMs?: number \| undefined`; *resolved config* types declare `readonly timeoutMs: number`. Verified: the `\| undefined` form accepts an explicitly-undefined spread; the bare `?:` form errors TS2375. This flag is what makes "unset" vs "explicitly disabled" a compile-time distinction — exactly the semantics a resilience config needs. |
| `isolatedDeclarations` | **highest** — every exported declaration needs an explicit type/return annotation (`export function f(x: number) { return x*2 }` → TS9013/TS9039) | **keep, but this is the one flag to drop if Wave 2 stalls.** It is the single strongest C1 lever: module boundaries become written contracts, so parallel agents cannot perturb each other through inference. Fixes are mechanical (add a return type) and LLM-trivial. |
| `erasableSyntaxOnly` | low — bans `enum`, `namespace`, parameter properties, `import =` | **keep, mandatory.** Without it an agent writes `enum Foo {}`, `tsc` passes, and `node --test` dies with `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX`. All three violations verified caught at typecheck as **TS1294**. Use `as const` objects + union types instead of enums (better types anyway). |
| `verbatimModuleSyntax` | low — forces `import type` | **keep, mandatory.** Node strips types but does **not** elide imports. Verified: `import { Cfg, VERSION } from "./types.ts"` where `Cfg` is a type → `SyntaxError: The requested module './types.ts' does not provide an export named 'Cfg'`. This flag is the only thing preventing that class of runtime failure. |
| `noPropertyAccessFromIndexSignature` | medium | keep — pairs with `noUncheckedIndexedAccess` to make dynamic access visibly dynamic |
| `noUnusedLocals` / `noUnusedParameters` | low | keep — escape hatch is a leading `_` on the parameter name |
| `skipLibCheck: true` | — | keep. Skips `@types/node` internals only; our own `.d.ts` is still checked at emit. |

### 5.3 Node floor and the built-in budget

**Floor: `>=22.12.0`.** Node 22 is Maintenance LTS (EOL 2027‑04‑30); Node 24 is Active LTS (EOL 2028‑04‑30); Node 26 is Current. CI should matrix **22, 24, 26**; the dev container is 22.22.2.

Probed on this machine — **safe to use**:

| API | Status |
|---|---|
| `node:test` (`test`, `describe`, `it`, `mock`, `snapshot`, `assert`, `suite`, `before/after*`) | ✅ |
| `AbortSignal.timeout`, `AbortSignal.any`, `AbortController` | ✅ |
| `structuredClone` | ✅ |
| `Promise.withResolvers` | ✅ |
| `Array.fromAsync`, `Object.groupBy`, `Map.groupBy` | ✅ |
| `Symbol.dispose` / `Symbol.asyncDispose` (as symbols) | ✅ |
| Iterator helpers (`Iterator.prototype.map` …), `Set.prototype.union` | ✅ |
| `Array.prototype.at/findLast/toSorted`, `Object.hasOwn`, `String.toWellFormed` | ✅ |
| `node:fs.glob`, `node:util.styleText` | ✅ |
| RegExp `v` flag, import attributes (`with { type: "json" }`) | ✅ |
| Native `.ts` execution (type stripping, no flag) | ✅ |

**Not available — must not be used:**

| API | Note |
|---|---|
| `using` / `await using` **syntax** | V8 rejects it: `SyntaxError`. The *symbols* exist; the *syntax* does not. Use `try/finally`. |
| `Promise.try` | `undefined` |
| `Float16Array` | `undefined` |
| `RegExp.escape` | `undefined` |
| `Error.isError` | `undefined` |
| `Symbol.metadata` | `undefined` |

**Consumer-visible type caveat:** `AbortSignal` is **not** in any TypeScript `lib.es*`. A consumer with `"lib": ["es2024"], "types": []` gets `TS2304: Cannot find name 'AbortSignal'` from our `.d.ts` (reproduced). This is normal for Node libraries — consumers have `@types/node` or `lib.dom`. Wave 2 should note it in the README rather than shipping a runtime dep. Do **not** add `"dom"` to our `lib`; it would let agents reach for browser globals.

### 5.4 Exact commands

```bash
# one-time setup (network needed once)
pnpm install

# everyday loop — all offline
pnpm run typecheck     # tsc -p tsconfig.json          (src + test, no emit)
pnpm run test          # node --test "test/**/*.test.ts"
pnpm run build         # tsc -p tsconfig.build.json    (dist/, incremental)
pnpm run check:pkg     # publint --strict && attw --pack . --profile esm-only
pnpm run verify        # all four, in order

# selective re-run (C2)
node --test test/resilience/retry.test.ts
node --test --test-name-pattern='backs off exponentially' test/resilience/retry.test.ts

# deterministic / offline install (CI and every agent)
pnpm install --frozen-lockfile            # network allowed, lock enforced
pnpm install --frozen-lockfile --offline  # 0.58 s from the local store
```

### 5.5 Exact versions to pin

| Package | Pin | Notes |
|---|---|---|
| `typescript` | `6.0.3` | exact, no caret. TS 7.0.2 emits identically — upgrade path is a one-line bump once `typescript-eslint` supports it |
| `@types/node` | `22.20.1` | **must track the Node 22 line.** `@types/node@latest` is 26.2.0 and would type ES2025/Node 26 APIs that do not exist here |
| `publint` | `0.3.23` | devDependency, not `npx` (C5) |
| `@arethetypeswrong/cli` | `0.18.5` | devDependency, not `npx` (C5) |

Runtime `dependencies`: **none.**

---

## 6. Risks for later waves

| ID | Risk | Severity | Mitigation the orchestrator must apply |
|---|---|---|---|
| **R‑1** | **`package.json` + `pnpm-lock.yaml` are shared files.** Six parallel agents each running `pnpm add -D …` will corrupt the lockfile and race on `package.json`. This is the single largest threat to C1. | **critical** | Freeze the full dependency set in the Wave 2 setup step (§5.5 is complete — nothing else is needed). Forbid Wave 2 agents from editing `package.json`, `pnpm-lock.yaml`, `tsconfig*.json` or running any `pnpm add`/`npm i`. Enforce with `pnpm install --frozen-lockfile` in CI so drift fails loudly. |
| **R‑2** | **The root barrel `src/index.ts` is a shared file** every module wants to append its exports to. | **high** | Write `src/index.ts` **once**, during Wave 2 setup, with the complete re-export list for all six modules already in place. Agents create the files it points at; nobody edits the barrel. Each module owns `src/<module>/index.ts` and re-exports only from within its own directory. |
| **R‑3** | `isolatedDeclarations` (TS9013/TS9039) could become a friction sink if agents write many un-annotated exports. | medium | Put the rule in the Wave 2 prompt verbatim: *"every `export` needs an explicit type or return-type annotation."* If Wave 2 stalls on it, this is the designated flag to drop — it is the only one whose removal changes no runtime behaviour and no emitted output. |
| **R‑4** | `sideEffects: false` is a promise, not a check. One agent writing a module-level `const registry = new Map()` **and mutating it at import time** silently breaks tree-shaking and, worse, makes import order significant — which would violate C3. | medium | Rule for Wave 2: no work at module scope beyond `const`/`function`/`class` declarations of pure values. R5 should add a lint rule; failing that, a CI grep for top-level statements. |
| **R‑5** | **`typescript-eslint` vs TypeScript version.** R5 will likely recommend `typescript-eslint`; its peer range is `>=4.8.4 <6.1.0`. Pinning TS 6.0.3 keeps it working. **If any later wave bumps TypeScript to 7.x, type-aware linting dies.** | **high** | Treat the TypeScript pin as an orchestrator-level decision, not an agent-level one. If R5 instead recommends `oxlint` (Rust, no TS API; `oxlint-tsgolint@7.0.2001` provides type-aware rules), TS 7 becomes viable — but that is a joint R1/R5 decision, not a unilateral one. **Flag for cross-stream reconciliation.** |
| **R‑6** | `.ts` extensions in imports look wrong to anyone (or any model) trained on the `.js`-extension convention. An agent "fixing" `./x.ts` → `./x.js` breaks every test with `ERR_MODULE_NOT_FOUND` while `tsc` still passes — a confusing split failure. | **high** | State the rule prominently in the Wave 2 guide with the failure mode spelled out. R5 should add `import/extensions`-equivalent enforcement. This is the highest-probability self-inflicted wound in the whole design. |
| **R‑7** | `exactOptionalPropertyTypes` will bite hard the first time an agent merges partial config objects. | medium | Ship the convention (§5.2) as a code snippet in the Wave 2 guide: input options use `?: T \| undefined`, resolved config uses `readonly x: T`. |
| **R‑8** | `attw` reports `cjs-resolves-to-esm` as a warning for any ESM-only package; a naive CI gate would fail on it forever. | low | Always invoke as `attw --pack . --profile esm-only` (verified: suppresses exactly that rule and nothing else). |
| **R‑9** | TypeScript 7 ships **20 platform-specific `optionalDependencies`**. Choosing TS 6.0.3 sidesteps this entirely; if a later wave upgrades, a lockfile generated on linux-x64 may not resolve on another arch. | low | Non-issue while pinned to 6.0.3. On any future TS 7 upgrade, set pnpm `supportedArchitectures` before regenerating the lockfile. |
| **R‑10** | `packageManager: "pnpm@10.33.0"` + `corepack enable` triggers a network download and hash check → breaks C5. | low | Do not run `corepack enable`. pnpm is already global at exactly 10.33.0. If CI must use corepack, set `COREPACK_ENABLE_STRICT=0` and pre-warm it. |
| **R‑11** | Node 22 reaches EOL 2027‑04‑30 and is already Maintenance-only. | low | Floor of `>=22.12.0` supports 22/24/26; CI should matrix all three so the eventual floor bump to 24 is a one-line change. |

---

## 7. Verification log

Every claim marked "verified" above was produced by running the following on this machine, not recalled:

| # | What was verified | Result |
|---|---|---|
| 1 | `node t.ts` with **no flags** on Node 22.22.2 | runs — type stripping is on by default |
| 2 | `import "./dep.js"` from a `.ts` file under type stripping | **fails** `ERR_MODULE_NOT_FOUND` |
| 3 | `import "./dep.ts"` from a `.ts` file | works |
| 4 | `enum` / `namespace` / parameter property under type stripping | `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` ×3 |
| 5 | `erasableSyntaxOnly` catches all three at typecheck | TS1294 ×3 |
| 6 | Unmarked type import under type stripping | `SyntaxError: … does not provide an export named 'Cfg'`; `import type` fixes it |
| 7 | `using r = new R()` on Node 22 | `SyntaxError` — syntax unsupported |
| 8 | Full build with the §5.2 configs on **TS 6.0.3** and **TS 7.0.2** | both exit 0; `diff -r dist6 dist7` → **identical** |
| 9 | `rewriteRelativeImportExtensions` output | `dist/*.js` → `.js`; `dist/*.d.ts` → retains `.ts` |
| 10 | Consumer typecheck against that `.d.ts` on TS 5.7.3 / 5.8.3 / 5.9.3 / 6.0.3 / 7.0.2 | all resolve |
| 11 | `require("interlayer")` from CJS on Node 22.22.2 | works; all named exports present |
| 12 | `publint --strict` on the §5.1 package | *All good!* |
| 13 | `attw --pack . --profile esm-only` | node10 / node16-CJS / node16-ESM / bundler all green |
| 14 | `node --test "test/**/*.test.ts"` on raw `.ts` with zero devDeps | passes; failure stack points at `.ts` with exact line:column |
| 15 | `node --test --test-name-pattern='^adds$'` | selects 1 of 2 tests |
| 16 | `pnpm install --offline --frozen-lockfile` after `rm -rf node_modules` | 0.58 s |
| 17 | `npm ci --offline` after `rm -rf node_modules` | 1.48 s |
| 18 | Lockfile drift with `--frozen-lockfile` / `npm ci` | `ERR_PNPM_OUTDATED_LOCKFILE` / `EUSAGE`, both exit 1 |
| 19 | Install cost of tsup / unbuild / tsdown / rollup / esbuild vs `tsc` | table in §1.3 |
| 20 | Node 22.22.2 built-in probe (25 APIs) | table in §5.3 |
| 21 | `exactOptionalPropertyTypes` escape hatch (`?: T \| undefined`) | bare `?:` errors TS2375; `\| undefined` form compiles |
| 22 | `typescript-eslint@8.67.0` peer range | `typescript: ">=4.8.4 <6.1.0"` — excludes TS 7 |
| 23 | TS 7.0.2 package exports | only `typescript/unstable/*`; `require("typescript")` → 2 keys |

**Sources consulted:**
[Node.js 22.12.0 release](https://nodejs.org/en/blog/release/v22.12.0) ·
[require(esm): from experiment to stability](https://joyeecheung.github.io/blog/2025/12/30/require-esm-in-node-js-from-experiment-to-stability/) ·
[Node.js EOL schedule](https://endoflife.date/nodejs) ·
[TypeScript 7.0 RC — Visual Studio Magazine](https://visualstudiomagazine.com/articles/2026/06/22/typescript-7-0-rc-moves-microsofts-go-rewrite-into-the-mainline-compiler.aspx) ·
[Microsoft Releases TypeScript 7.0 — InfoQ](https://www.infoq.com/news/2026/08/typescript-7-released/) ·
[TypeScript 7 migration readiness](https://www.digitalapplied.com/blog/typescript-7-native-compiler-early-adopter-migration-readiness) ·
[Publishing ESM packages](https://esmodules.com/publishing/)
