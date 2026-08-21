/**
 * `package.json` — the half of the published surface that is not code.
 *
 * A barrel can be perfect and the package still be unusable: an `exports` map
 * that does not resolve, a `files` list that ships the wrong tree, a stray
 * runtime dependency, an `engines` floor below what the code actually calls.
 * None of that is caught by any test that imports from `src/`.
 *
 * The resolution tests deliberately run in a CHILD `node`, never in-process:
 * inside vitest, `import.meta.resolve` is Vite's resolver, which happily
 * resolves paths Node would refuse. Only Node's own loader can testify about
 * Node's own `exports` semantics.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { beforeAll, describe, expect, it } from 'vitest';
import { REPO_ROOT } from './reflect.ts';

interface Manifest {
  readonly name: string;
  readonly type?: string;
  readonly main?: string;
  readonly types?: string;
  readonly files?: readonly string[];
  readonly sideEffects?: boolean;
  readonly engines?: { readonly node?: string };
  readonly exports?: Readonly<Record<string, unknown>>;
  readonly dependencies?: Readonly<Record<string, string>>;
  readonly peerDependencies?: Readonly<Record<string, string>>;
  readonly optionalDependencies?: Readonly<Record<string, string>>;
  readonly bundledDependencies?: readonly string[];
  readonly bundleDependencies?: readonly string[];
  readonly devDependencies?: Readonly<Record<string, string>>;
}

const MANIFEST_PATH = path.join(REPO_ROOT, 'package.json');
const pkg = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8')) as Manifest;

const DIST_ENTRY = path.join(REPO_ROOT, 'dist', 'index.js');
const distBuilt = existsSync(DIST_ENTRY);

/* ------------------------------------------------------------------ *
 * Zero runtime dependencies — constraint C4
 * ------------------------------------------------------------------ */

describe('dependencies', () => {
  it('declares an empty `dependencies` object', () => {
    // Present-and-empty rather than absent: an explicit `{}` is a statement,
    // and `npm install <pkg>` mutates it in place where it can be seen in
    // review. This assertion is the guard that keeps it that way.
    expect(pkg.dependencies).toEqual({});
  });

  it.each(['peerDependencies', 'optionalDependencies'] as const)(
    'declares no %s either',
    (field) => {
      expect(pkg[field] ?? {}).toEqual({});
    },
  );

  it('bundles nothing', () => {
    expect(pkg.bundledDependencies ?? []).toEqual([]);
    expect(pkg.bundleDependencies ?? []).toEqual([]);
  });

  it('keeps the compiler and the test runner in devDependencies only', () => {
    const dev = Object.keys(pkg.devDependencies ?? {});
    expect(dev).toEqual(expect.arrayContaining(['typescript', 'vitest', '@biomejs/biome']));
    for (const name of dev) expect(pkg.dependencies ?? {}).not.toHaveProperty(name);
  });

  it('imports nothing but relative paths in shipped source', () => {
    // The manifest can say `{}` while the code still imports `node:fs`. Every
    // specifier in a file that reaches `dist/` must start with `.`; the only
    // bare imports in `src/**` live in `*.test.ts`, which the build excludes.
    const output = execFileSync(
      'node',
      [
        '-e',
        `const {readdirSync,readFileSync,statSync}=require('node:fs');
         const {join}=require('node:path');
         const bad=[];
         const walk=(d)=>{for(const e of readdirSync(d)){const p=join(d,e);
           if(statSync(p).isDirectory()){walk(p);continue;}
           if(!p.endsWith('.ts')||p.endsWith('.test.ts'))continue;
           for(const m of readFileSync(p,'utf8').matchAll(/^\\s*(?:import|export)[^'"\\n]*from\\s*['"]([^'"]+)['"]/gm))
             if(!m[1].startsWith('.'))bad.push(p+' -> '+m[1]);
         }};
         walk(join(process.cwd(),'src'));
         process.stdout.write(JSON.stringify(bad));`,
      ],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
    expect(JSON.parse(output)).toEqual([]);
  });
});

/* ------------------------------------------------------------------ *
 * Shape
 * ------------------------------------------------------------------ */

describe('manifest shape', () => {
  it('is an ESM package', () => {
    expect(pkg.type).toBe('module');
  });

  it('is side-effect free, so a bundler can tree-shake it', () => {
    expect(pkg.sideEffects).toBe(false);
  });

  it('points `main` and `types` at the built entry', () => {
    expect(pkg.main).toBe('./dist/index.js');
    expect(pkg.types).toBe('./dist/index.d.ts');
  });

  it('ships `dist` AND `src`', () => {
    // `src` is not optional: `dist/index.d.ts.map` records
    // `"sources": ["../src/index.ts"]`, so go-to-definition lands on nothing
    // without it (R1 §5.1).
    expect(pkg.files).toEqual(['dist', 'src']);
  });

  it('declares the `exports` map documented in R1 §5.1', () => {
    expect(pkg.exports).toEqual({
      '.': {
        types: './dist/index.d.ts',
        import: './dist/index.js',
        default: './dist/index.js',
      },
      './package.json': './package.json',
    });
  });

  it('puts `types` first in the entry condition object', () => {
    // Condition order is significant and `types` must precede the rest, or a
    // consumer's TypeScript resolves the JS and reports implicit `any`.
    const entry = pkg.exports?.['.'] as Record<string, unknown> | undefined;
    expect(Object.keys(entry ?? {})[0]).toBe('types');
  });

  it('exposes no subpath beyond `.` and `./package.json`', () => {
    expect(Object.keys(pkg.exports ?? {}).sort()).toEqual(['.', './package.json']);
  });
});

/* ------------------------------------------------------------------ *
 * engines.node vs what the code actually calls
 * ------------------------------------------------------------------ */

function floorOf(range: string): readonly [number, number, number] {
  const match = /^>=\s*(\d+)\.(\d+)\.(\d+)$/.exec(range);
  if (match === null) throw new Error(`engines.node is not a simple >= range: ${range}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function atLeast(floor: readonly [number, number, number], want: readonly number[]): boolean {
  for (let i = 0; i < 3; i += 1) {
    const have = floor[i] ?? 0;
    const need = want[i] ?? 0;
    if (have !== need) return have > need;
  }
  return true;
}

describe('engines.node', () => {
  const declared = pkg.engines?.node ?? '';

  it('is declared as a simple lower bound', () => {
    expect(declared).toBe('>=22.12.0');
  });

  it.each([
    // feature actually used in shipped src -> first Node release that has it
    ['Symbol.asyncDispose (src/layer.ts)', [20, 4, 0]],
    ['global crypto.randomUUID (src/core/runtime.ts)', [19, 0, 0]],
    ['the ES2024 library the build targets', [22, 0, 0]],
    ['require(ESM) unflagged — the stated rationale for the floor', [22, 12, 0]],
  ] as const)('is high enough for %s', (_feature, want) => {
    expect(atLeast(floorOf(declared), want)).toBe(true);
  });

  it('is satisfied by the Node running this suite', () => {
    const [major, minor, patch] = process.versions.node.split('.').map(Number);
    expect(atLeast([major ?? 0, minor ?? 0, patch ?? 0], floorOf(declared))).toBe(true);
  });

  it.each([
    ['Symbol.asyncDispose', typeof Symbol.asyncDispose],
    ['crypto.randomUUID', typeof globalThis.crypto?.randomUUID],
    ['AbortController', typeof globalThis.AbortController],
  ] as const)('%s really exists on this runtime', (_name, actual) => {
    expect(actual).not.toBe('undefined');
  });
});

/* ------------------------------------------------------------------ *
 * The exports map, resolved by Node itself
 * ------------------------------------------------------------------ */

interface Probe {
  readonly resolved?: string;
  readonly error?: string;
}

let probes = new Map<string, Probe>();

const SPECIFIERS: readonly string[] = [
  'interlayer',
  'interlayer/package.json',
  'interlayer/dist/index.js',
  'interlayer/dist/core/errors.js',
  'interlayer/src/index.ts',
  'interlayer/layer',
  'interlayer/core/errors',
];

beforeAll(() => {
  // Self-reference resolution: a package whose manifest has `name` + `exports`
  // resolves its own name through its own `exports` map, exactly as a copy
  // under a consumer's `node_modules` would. No install required.
  const script = `const out={};
    for (const s of ${JSON.stringify(SPECIFIERS)}) {
      try { out[s] = { resolved: import.meta.resolve(s) }; }
      catch (e) { out[s] = { error: e.code ?? String(e) }; }
    }
    process.stdout.write(JSON.stringify(out));`;
  const stdout = execFileSync('node', ['--input-type=module', '-e', script], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
  });
  probes = new Map(Object.entries(JSON.parse(stdout) as Record<string, Probe>));
});

describe('exports map, resolved by plain node', () => {
  it.runIf(distBuilt)('the documented entry resolves to the built barrel', () => {
    expect(probes.get('interlayer')?.resolved).toBe(
      new URL('dist/index.js', `file://${REPO_ROOT}/`).href,
    );
  });

  it('`./package.json` resolves — several tools read it', () => {
    expect(probes.get('interlayer/package.json')?.resolved).toBe(
      new URL('package.json', `file://${REPO_ROOT}/`).href,
    );
  });

  it.each([
    'interlayer/dist/index.js',
    'interlayer/dist/core/errors.js',
    'interlayer/src/index.ts',
    'interlayer/layer',
    'interlayer/core/errors',
  ])('%s is NOT resolvable — internals stay internal', (specifier) => {
    // With no wildcard subpath, `exports` is a closed set. Anything reaching
    // past it must fail at RESOLUTION, before a consumer can build a habit out
    // of it, or every file under `dist/` becomes API by accident.
    expect(probes.get(specifier)).toEqual({ error: 'ERR_PACKAGE_PATH_NOT_EXPORTED' });
  });
});
