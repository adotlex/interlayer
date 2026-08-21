/**
 * BUILD OUTPUT — what `npm run build` actually writes.
 *
 * The source imports `./core/result.ts`. Node cannot load that. The whole
 * package hinges on `rewriteRelativeImportExtensions` turning every `.ts`
 * specifier into `.js` on the way out, and on that rewrite being TOTAL: one
 * missed specifier is an `ERR_MODULE_NOT_FOUND` on the consumer's first import,
 * and no test that runs against `src/` can ever see it.
 *
 * The check is AST-based, via `ts.preProcessFile`, NOT a grep. `dist/` is built
 * with `removeComments: false`, and a doc comment in
 * `resilience/timeout/index.js` contains a `.ts` specifier in an EXAMPLE —
 * `grep "\.ts'" dist --include=*.js` reports it and looks like a real finding.
 * The parser sees module specifiers only.
 *
 * SKIPS, never fails, when `dist/` is absent: five other agents run this suite
 * and none of them should have to build first.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { REPO_ROOT } from './reflect.ts';

const SRC = path.join(REPO_ROOT, 'src');
const DIST = path.join(REPO_ROOT, 'dist');
const distBuilt = existsSync(path.join(DIST, 'index.js'));

function walk(dir: string): readonly string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out.sort();
}

/** Module specifiers only — comments and strings in code are not specifiers. */
function specifiersOf(file: string): readonly string[] {
  const info = ts.preProcessFile(readFileSync(file, 'utf8'), true, true);
  return info.importedFiles.map((ref) => ref.fileName);
}

const srcModules = walk(SRC)
  .filter((f) => f.endsWith('.ts') && !f.endsWith('.test.ts'))
  .map((f) => path.relative(SRC, f).replace(/\.ts$/, ''));

const distFiles = walk(DIST).map((f) => path.relative(DIST, f));
const distJs = distFiles.filter((f) => f.endsWith('.js'));
const distDts = distFiles.filter((f) => f.endsWith('.d.ts'));

describe.skipIf(!distBuilt)('dist/ layout', () => {
  it('emits one .js per shipped source module', () => {
    const expected = srcModules.map((m) => `${m}.js`).sort();
    expect(distJs.sort()).toEqual(expected);
  });

  it('emits a .d.ts for every one of them — the whole public surface is typed', () => {
    const expected = srcModules.map((m) => `${m}.d.ts`).sort();
    expect(distDts.sort()).toEqual(expected);
  });

  it('emits both sourcemap kinds', () => {
    expect(distFiles.filter((f) => f.endsWith('.js.map')).length).toBe(srcModules.length);
    expect(distFiles.filter((f) => f.endsWith('.d.ts.map')).length).toBe(srcModules.length);
  });

  it('ships the barrel at the documented path', () => {
    expect(distFiles).toContain('index.js');
    expect(distFiles).toContain('index.d.ts');
  });

  it('ships no test files', () => {
    // `tsconfig.build.json` excludes `src/**/*.test.ts`. Wave 2 wrote 460 of
    // them; shipping any would double the package and import `vitest` at
    // runtime.
    expect(distFiles.filter((f) => /\.test\.(js|d\.ts)$/.test(f))).toEqual([]);
  });

  it('ships nothing but js, d.ts and maps', () => {
    const unexpected = distFiles.filter(
      (f) => !/\.(js|d\.ts|js\.map|d\.ts\.map)$/.test(f) && !f.endsWith('.d.ts'),
    );
    expect(unexpected).toEqual([]);
  });
});

describe.skipIf(!distBuilt)('emitted .js specifiers', () => {
  it('rewrote every relative `.ts` specifier to `.js`', () => {
    const offenders: string[] = [];
    for (const rel of distJs) {
      for (const spec of specifiersOf(path.join(DIST, rel))) {
        if (spec.startsWith('.') && spec.endsWith('.ts')) offenders.push(`${rel} -> ${spec}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('gives every relative specifier an explicit .js extension', () => {
    // ESM has no extension resolution. A bare `./result` would resolve under
    // the bundler-flavoured configs people test with and fail under node.
    const offenders: string[] = [];
    for (const rel of distJs) {
      for (const spec of specifiersOf(path.join(DIST, rel))) {
        if (spec.startsWith('.') && !spec.endsWith('.js')) offenders.push(`${rel} -> ${spec}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('resolves every relative specifier to a file that exists', () => {
    const missing: string[] = [];
    for (const rel of distJs) {
      const from = path.join(DIST, rel);
      for (const spec of specifiersOf(from)) {
        if (!spec.startsWith('.')) continue;
        const target = path.resolve(path.dirname(from), spec);
        if (!existsSync(target)) missing.push(`${rel} -> ${spec}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it('imports nothing from outside the package', () => {
    // The zero-dependency claim, checked against the artifact rather than the
    // manifest: no bare specifiers at all, `node:` builtins included.
    const external: string[] = [];
    for (const rel of distJs) {
      for (const spec of specifiersOf(path.join(DIST, rel))) {
        if (!spec.startsWith('.')) external.push(`${rel} -> ${spec}`);
      }
    }
    expect(external).toEqual([]);
  });

  it('leaves `.ts` text in comments only, which is why this is not a grep', () => {
    // Documents the trap. `removeComments: false` keeps the doc example in
    // `resilience/timeout/index.ts`, so the naive grep the plan suggests finds
    // exactly one hit and it is not a defect.
    const grepHits = distJs.filter((rel) =>
      /\.ts['"]/.test(readFileSync(path.join(DIST, rel), 'utf8')),
    );
    const realHits = distJs.filter((rel) =>
      specifiersOf(path.join(DIST, rel)).some((s) => s.startsWith('.') && s.endsWith('.ts')),
    );
    expect(realHits).toEqual([]);
    expect(grepHits).toEqual(['resilience/timeout/index.js']);
  });
});

describe.skipIf(!distBuilt)('emitted .d.ts specifiers', () => {
  it('keeps `.ts` specifiers — documented behaviour, and correct', () => {
    // `rewriteRelativeImportExtensions` does NOT rewrite declaration files, on
    // purpose: TypeScript resolves `./core/result.ts` inside a `.d.ts` to
    // `./core/result.d.ts`. Verified green on consumer TS 5.7 through 7.0
    // (R1 §5.1). Pinned here so a future "fix" that rewrites them — which would
    // make them resolve to the JS instead — shows up as a failure.
    const specs = specifiersOf(path.join(DIST, 'index.d.ts')).filter((s) => s.startsWith('.'));
    expect(specs.length).toBeGreaterThan(0);
    expect(specs.every((s) => s.endsWith('.ts'))).toBe(true);
  });

  it('resolves every declaration specifier to a real .d.ts', () => {
    const missing: string[] = [];
    for (const rel of distDts) {
      const from = path.join(DIST, rel);
      for (const spec of specifiersOf(from)) {
        if (!spec.startsWith('.')) continue;
        const target = path.resolve(path.dirname(from), spec.replace(/\.ts$/, '.d.ts'));
        if (!existsSync(target)) missing.push(`${rel} -> ${spec}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it('points its declaration map back at shipped source', () => {
    const map = JSON.parse(readFileSync(path.join(DIST, 'index.d.ts.map'), 'utf8')) as {
      sources: readonly string[];
    };
    // This is why `files` must include `src`.
    expect(map.sources).toEqual(['../src/index.ts']);
    for (const source of map.sources) {
      expect(existsSync(path.resolve(DIST, source))).toBe(true);
    }
  });
});

describe.skipIf(!distBuilt)('the built barrel under plain node', () => {
  it('exposes exactly the 61 runtime values, and no default', () => {
    const stdout = execFileSync(
      'node',
      [
        '--input-type=module',
        '-e',
        `const m = await import('interlayer');
         process.stdout.write(JSON.stringify(Object.keys(m).sort()));`,
      ],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
    const keys = JSON.parse(stdout) as readonly string[];
    expect(keys).not.toContain('default');
    expect(keys).toEqual([
      'AllProvidersFailedError',
      'BulkheadFullError',
      'CancelledError',
      'CircuitOpenError',
      'ConfigError',
      'DEFAULTS',
      'InterlayerError',
      'NoProviderError',
      'POLICY_ORDER',
      'POLICY_SCOPE',
      'ProviderError',
      'RateLimitedError',
      'RetryExhaustedError',
      'TimeoutError',
      'TransportError',
      'UnsupportedCapabilityError',
      'ValidationError',
      'attemptTimeout',
      'capability',
      'capabilityMeta',
      'capabilityNames',
      'chainSelectors',
      'circuitBreaker',
      'createEmitter',
      'createLayer',
      'createRegistry',
      'createSystemRuntime',
      'declaresCapability',
      'defaultSelector',
      'defaultShouldFallback',
      'defineContract',
      'definePolicy',
      'defineProvider',
      'err',
      'filterByTags',
      'formatErrorChain',
      'hasCode',
      'implementedCapabilities',
      'inOrder',
      'isCancellation',
      'isErr',
      'isInterlayerError',
      'isOk',
      'isTransient',
      'ok',
      'orderPolicies',
      'policyStack',
      'rateLimit',
      'retryPolicy',
      'roundRobin',
      'sticky',
      'systemClock',
      'systemRandom',
      'systemRuntime',
      'systemTimers',
      'toInterlayerError',
      'totalTimeout',
      'unwrap',
      'unwrapOr',
      'v',
      'weighted',
    ]);
  });

  it('has no import side effects, as `sideEffects: false` promises', () => {
    // Importing the barrel must not open a handle, start a timer or touch a
    // global. If it did, `sideEffects: false` would let a bundler drop code the
    // package actually depends on.
    const stdout = execFileSync(
      'node',
      [
        '--input-type=module',
        '-e',
        `const before = process.getActiveResourcesInfo().sort();
         await import('interlayer');
         const after = process.getActiveResourcesInfo().sort();
         process.stdout.write(JSON.stringify({ before, after }));`,
      ],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
    const { before, after } = JSON.parse(stdout) as { before: string[]; after: string[] };
    expect(after).toEqual(before);
  });
});

describe.skipIf(distBuilt)('dist/ is absent', () => {
  it('skips the build-output suite instead of failing it', () => {
    // Placeholder so an unbuilt tree reports "skipped", not "0 tests".
    expect(distBuilt).toBe(false);
  });
});
