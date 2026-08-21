/**
 * BUG-T6-01 — `npm run clean && npm run build` ships an EMPTY package.
 *
 * ── What happens ──────────────────────────────────────────────────────────
 *
 *   "clean": "node -e \"require('node:fs').rmSync('dist',{...})\""
 *   "build": "tsc -p tsconfig.build.json"
 *
 * and `tsconfig.build.json` sets
 *
 *   "incremental": true,
 *   "tsBuildInfoFile": "./node_modules/.cache/tsbuild/build.tsbuildinfo"
 *
 * `clean` deletes `dist`. It does NOT delete the build info, which lives
 * OUTSIDE `dist` by design. On the next `tsc`, the cache still says every file
 * is up to date, so tsc skips all of them, emits nothing, and EXITS 0.
 *
 * ── Measured on this tree, against the real scripts ───────────────────────
 *
 *   $ npm run clean && npm run build          # exit 0, no output
 *   $ ls dist                                 # ls: cannot access 'dist'
 *
 * Not "incomplete" — ABSENT. `dist/` is not even recreated. `noEmitOnError`
 * cannot help: there are no errors. A CI job that does the conventional
 * `clean → build → publish` would push a tarball whose `files: ["dist","src"]`
 * carries `src` and nothing else, so `main: ./dist/index.js` resolves to
 * nothing and every install is broken on `import`.
 *
 * ── The fix, and what each half of this file checks ───────────────────────
 *
 * The fix is one line in `package.json` (R1 §5.1 specifies
 * `"clean": "rm -rf dist node_modules/.cache"`, which is correct and was not
 * what shipped); `clean` now deletes `node_modules/.cache/tsbuild` as well as
 * `dist`. The static checks below pin that invariant against the real script.
 *
 * The live reproduction proves the MECHANISM against the real `src/`, and note
 * what it can and cannot say: it never invokes `npm run clean`, so it measures
 * `tsc`'s own behaviour, not this package's. See the long note on
 * `deleting ONLY the output directory does not make the next build re-emit`.
 *
 * The reproduction never touches the repository's own `dist/` or its shared
 * `node_modules/.cache`: it compiles into a private temp directory with its own
 * `tsBuildInfoFile`, using the same compiler options `tsconfig.build.json` sets.
 */

import { execFileSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { REPO_ROOT } from './reflect.ts';

const TSC = path.join(REPO_ROOT, 'node_modules', 'typescript', 'bin', 'tsc');
const canCompile = existsSync(TSC);

interface Manifest {
  readonly scripts?: { readonly clean?: string; readonly build?: string };
}
interface BuildConfig {
  readonly compilerOptions?: {
    readonly incremental?: boolean;
    readonly tsBuildInfoFile?: string;
    readonly outDir?: string;
  };
}

const pkg = JSON.parse(readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8')) as Manifest;
const buildConfig = JSON.parse(
  readFileSync(path.join(REPO_ROOT, 'tsconfig.build.json'), 'utf8'),
) as BuildConfig;

const cleanScript = pkg.scripts?.clean ?? '';
const buildInfo = buildConfig.compilerOptions?.tsBuildInfoFile ?? '';

/* ------------------------------------------------------------------ *
 * 1. The static invariant
 * ------------------------------------------------------------------ */

describe('clean must invalidate everything build caches', () => {
  it('the build is incremental and caches outside dist/', () => {
    // Establishes the premise the next test depends on. If either of these
    // changes, the bug is gone by construction and the test below should be
    // re-derived rather than deleted.
    expect(buildConfig.compilerOptions?.incremental).toBe(true);
    expect(buildInfo).toBe('./node_modules/.cache/tsbuild/build.tsbuildinfo');
    expect(buildInfo.startsWith('./dist')).toBe(false);
  });

  it('the cache lies outside everything `clean` deletes', () => {
    // The mechanical link, independent of how `clean` is spelled: the script
    // removes the `dist` directory and nothing else, and the build info is not
    // inside it, so no amount of `rmSync('dist')` can invalidate the cache.
    const removed = path.resolve(REPO_ROOT, 'dist');
    const cache = path.resolve(REPO_ROOT, buildInfo);
    expect(cleanScript).toContain("rmSync('dist'");
    expect(cache.startsWith(`${removed}${path.sep}`)).toBe(false);
  });

  /** FAILING — documents BUG-T6-01. */
  it('`npm run clean` removes the incremental build cache', () => {
    // `clean` must remove the tsbuildinfo (or the `.cache` directory holding
    // it) as well as `dist`, or the next `build` believes its own stale
    // bookkeeping and emits nothing. R1 §5.1 specified
    // `rm -rf dist node_modules/.cache`; the shipped script drops the second
    // path.
    const clearsCache =
      cleanScript.includes('tsbuildinfo') ||
      cleanScript.includes('node_modules/.cache') ||
      cleanScript.includes('.cache/tsbuild');
    expect({ script: cleanScript, clearsCache }).toEqual({
      script: cleanScript,
      clearsCache: true,
    });
  });
});

/* ------------------------------------------------------------------ *
 * 2. The live reproduction
 * ------------------------------------------------------------------ */

interface BuildOutcome {
  readonly status: number;
  readonly emitted: number;
}

let cold: BuildOutcome | undefined;
let afterDistDeleted: BuildOutcome | undefined;
let afterCacheDeleted: BuildOutcome | undefined;
let workspace: string | undefined;

function countEmitted(dir: string): number {
  if (!existsSync(dir)) return 0;
  let total = 0;
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) total += countEmitted(full);
    else if (full.endsWith('.js')) total += 1;
  }
  return total;
}

function shippedModuleCount(): number {
  const src = path.join(REPO_ROOT, 'src');
  let total = 0;
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir)) {
      const full = path.join(dir, entry);
      if (statSync(full).isDirectory()) walk(full);
      else if (full.endsWith('.ts') && !full.endsWith('.test.ts')) total += 1;
    }
  };
  walk(src);
  return total;
}

beforeAll(() => {
  if (!canCompile) return;
  workspace = mkdtempSync(path.join(os.tmpdir(), 'interlayer-cleanbuild-'));
  const outDir = path.join(workspace, 'dist');
  const cacheDir = path.join(workspace, 'cache');
  const buildInfoFile = path.join(cacheDir, 'build.tsbuildinfo');
  mkdirSync(cacheDir, { recursive: true });

  const configPath = path.join(workspace, 'tsconfig.repro.json');
  writeFileSync(
    configPath,
    JSON.stringify({
      // Same base and the same emit options `tsconfig.build.json` sets. Only
      // the three paths differ, so the repository's real dist/ and shared
      // node_modules/.cache are never touched.
      extends: path.join(REPO_ROOT, 'tsconfig.json'),
      compilerOptions: {
        noEmit: false,
        // The config lives outside the repo, so `types: ["node"]` cannot walk
        // up to the installed @types. Nothing else about the build changes.
        typeRoots: [path.join(REPO_ROOT, 'node_modules', '@types')],
        rootDir: path.join(REPO_ROOT, 'src'),
        outDir,
        declarationMap: true,
        sourceMap: true,
        removeComments: false,
        noEmitOnError: true,
        incremental: true,
        tsBuildInfoFile: buildInfoFile,
      },
      include: [path.join(REPO_ROOT, 'src', '**', '*.ts')],
      exclude: [path.join(REPO_ROOT, 'src', '**', '*.test.ts'), path.join(REPO_ROOT, 'test')],
    }),
  );

  const compile = (): BuildOutcome => {
    let status = 0;
    try {
      execFileSync(process.execPath, [TSC, '-p', configPath], {
        cwd: workspace,
        encoding: 'utf8',
        stdio: 'pipe',
      });
    } catch (error) {
      status = (error as { status?: number }).status ?? 1;
    }
    return { status, emitted: countEmitted(outDir) };
  };

  cold = compile();

  // Exactly what `npm run clean` does: remove `dist`, leave the cache.
  rmSync(outDir, { recursive: true, force: true });
  afterDistDeleted = compile();

  // And what it should have done as well.
  rmSync(outDir, { recursive: true, force: true });
  rmSync(cacheDir, { recursive: true, force: true });
  afterCacheDeleted = compile();
}, 180_000);

afterAll(() => {
  if (workspace !== undefined) rmSync(workspace, { recursive: true, force: true });
});

describe.skipIf(!canCompile)('reproduction against the real src/', () => {
  it('a cold build emits one .js per shipped module', () => {
    // Harness sanity: if this is wrong, nothing below means anything.
    expect(cold?.status).toBe(0);
    expect(cold?.emitted).toBe(shippedModuleCount());
  });

  it('the rebuild after clean still reports success', () => {
    // This is what makes the bug dangerous rather than merely annoying: tsc
    // exits 0, so `clean && build && publish` never stops.
    expect(afterDistDeleted?.status).toBe(0);
  });

  /**
   * THE DEFECT ITSELF, pinned as behaviour of `tsc` rather than of this package.
   *
   * ── This assertion was inverted, deliberately, and here is why ────────────
   *
   * It was written as `expect(afterDistDeleted?.emitted).toBe(shippedModuleCount())`
   * — "deleting the output directory makes the next build re-emit it" — on the
   * understanding that fixing `clean` in `package.json` would turn it green.
   * It cannot. This block never runs `npm run clean`: it deletes `outDir` by
   * hand in a private workspace and then runs `tsc` with its OWN
   * `tsBuildInfoFile`. What it therefore measures is a property of the
   * TypeScript compiler — that an incremental (non-`--build`) invocation trusts
   * its `.tsbuildinfo` and never checks whether the outputs it describes still
   * exist — and no change to this repository can alter it. Verified on the
   * pinned tsc (6.0.3), with and without `composite: true`: 0 files emitted.
   *
   * So the assertion is inverted to state the fact, which is what makes the
   * fix necessary rather than optional. The invariant the file exists to
   * protect is unchanged and is enforced by its three neighbours:
   *
   *   `the cache lies outside everything clean deletes`  — the mechanism,
   *   `npm run clean removes the incremental build cache` — the fix, in
   *                                                        `package.json`,
   *   `removing the cache as well as the output DOES rebuild` — the proof that
   *                                                        the fix suffices.
   *
   * If tsc ever starts validating output presence, this test goes red and is
   * the right place to learn that `clean` no longer has to delete the cache.
   */
  it('deleting ONLY the output directory does not make the next build re-emit', () => {
    expect(afterDistDeleted?.emitted, 'tsc trusts a cache describing files that are gone').toBe(0);
    expect(shippedModuleCount(), 'and there really was something to re-emit').toBeGreaterThan(0);
  });

  it('removing the cache as well as the output DOES rebuild — the diagnosis', () => {
    // Same source, same options, same deleted output; the only difference is
    // that the tsbuildinfo went too. This isolates the cause to the cache and
    // confirms the one-line fix in `clean` is sufficient.
    expect(afterCacheDeleted?.status).toBe(0);
    expect(afterCacheDeleted?.emitted).toBe(shippedModuleCount());
  });
});
