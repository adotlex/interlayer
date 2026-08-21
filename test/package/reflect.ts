/**
 * Static reflection over the barrels, via the TypeScript compiler API.
 *
 * WHY NOT just `import * as m from '../../src/index.ts'` and read
 * `Object.keys(m)`: that sees only the 61 runtime VALUES. Four fifths of a
 * typed library's published surface is types, and a type that silently stops
 * being exported breaks every consumer while every runtime assertion stays
 * green. The checker sees both, and it resolves `export *` the way a consumer's
 * compiler will.
 *
 * `typescript` is a devDependency. Nothing here is imported by `src/**`, so the
 * package's zero-runtime-dependency guarantee is untouched — `manifest.test.ts`
 * asserts that separately.
 */

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

/** Absolute path to the repository root. */
export const REPO_ROOT: string = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../..',
);

/** The published barrel. Everything a consumer can import is named here. */
export const ROOT_BARREL = 'src/index.ts';

/** The seven unit barrels the root barrel is assembled from. */
export const UNIT_BARRELS: readonly string[] = [
  'src/core/index.ts',
  'src/registry/index.ts',
  'src/resilience/circuit-breaker/index.ts',
  'src/resilience/rate-limit/index.ts',
  'src/resilience/retry/index.ts',
  'src/resilience/timeout/index.ts',
  'src/routing/index.ts',
];

const SRC_DIR = `${path.join(REPO_ROOT, 'src')}${path.sep}`;

interface Reflection {
  readonly program: ts.Program;
  readonly checker: ts.TypeChecker;
}

let cached: Reflection | undefined;

function reflection(): Reflection {
  if (cached !== undefined) return cached;
  const configPath = path.join(REPO_ROOT, 'tsconfig.json');
  const raw = ts.readConfigFile(configPath, ts.sys.readFile);
  if (raw.error !== undefined) {
    throw new Error(
      `cannot read tsconfig.json: ${ts.flattenDiagnosticMessageText(raw.error.messageText, ' ')}`,
    );
  }
  const parsed = ts.parseJsonConfigFileContent(raw.config, ts.sys, REPO_ROOT);
  const roots = [ROOT_BARREL, ...UNIT_BARRELS].map((rel) => path.join(REPO_ROOT, rel));
  const program = ts.createProgram(roots, { ...parsed.options, noEmit: true });
  cached = { program, checker: program.getTypeChecker() };
  return cached;
}

function moduleSymbol(relativePath: string): { checker: ts.TypeChecker; symbol: ts.Symbol } {
  const { program, checker } = reflection();
  const file = program.getSourceFile(path.join(REPO_ROOT, relativePath));
  if (file === undefined) throw new Error(`not in program: ${relativePath}`);
  const symbol = checker.getSymbolAtLocation(file);
  if (symbol === undefined) throw new Error(`${relativePath} is not a module (no exports at all?)`);
  return { checker, symbol };
}

/**
 * Every name a consumer can import from `relativePath` — values AND types,
 * with `export *` and `export * as ns` resolved. Sorted, so a diff is readable.
 */
export function exportsOf(relativePath: string): readonly string[] {
  const { checker, symbol } = moduleSymbol(relativePath);
  return checker
    .getExportsOfModule(symbol)
    .map((s) => s.getName())
    .sort();
}

/** Whether `relativePath` has a default export. The barrel must not (R6 §5.3). */
export function hasDefaultExport(relativePath: string): boolean {
  return exportsOf(relativePath).includes('default');
}

function resolveAlias(checker: ts.TypeChecker, symbol: ts.Symbol): ts.Symbol {
  return (symbol.flags & ts.SymbolFlags.Alias) !== 0 ? checker.getAliasedSymbol(symbol) : symbol;
}

function declaredInSrc(symbol: ts.Symbol): boolean {
  const declarations = symbol.getDeclarations() ?? [];
  if (declarations.length === 0) return false;
  return declarations.every((d) => {
    const file = d.getSourceFile().fileName;
    return file.startsWith(SRC_DIR) && !file.endsWith('.test.ts');
  });
}

/**
 * Walks the SIGNATURE of a declaration and nothing else.
 *
 * Function bodies and initializers name plenty of internal types — that is what
 * internal types are for, and flagging them would drown the real finding. What
 * matters is the part a consumer has to be able to WRITE DOWN: parameter types,
 * return types, heritage clauses, interface members, alias right-hand sides.
 */
function forEachSignatureChild(node: ts.Node, visit: (child: ts.Node) => void): void {
  ts.forEachChild(node, (child) => {
    if (ts.isBlock(child)) return;
    if (
      (ts.isVariableDeclaration(node) ||
        ts.isPropertyDeclaration(node) ||
        ts.isPropertyAssignment(node) ||
        ts.isParameter(node)) &&
      child === node.initializer
    ) {
      return;
    }
    if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && child === node.body) return;
    visit(child);
  });
}

function typeParameterNames(declaration: ts.Node): ReadonlySet<string> {
  const names = new Set<string>();
  const walk = (node: ts.Node): void => {
    const holder = node as { typeParameters?: ts.NodeArray<ts.TypeParameterDeclaration> };
    if (holder.typeParameters !== undefined) {
      for (const parameter of holder.typeParameters) names.add(parameter.name.text);
    }
    forEachSignatureChild(node, walk);
  };
  walk(declaration);
  return names;
}

function referencedIdentifier(node: ts.Node): ts.Identifier | undefined {
  if (ts.isTypeReferenceNode(node)) {
    if (ts.isIdentifier(node.typeName)) return node.typeName;
    return ts.isIdentifier(node.typeName.left) ? node.typeName.left : undefined;
  }
  if (ts.isExpressionWithTypeArguments(node) && ts.isIdentifier(node.expression)) {
    return node.expression;
  }
  return undefined;
}

/** A type named in a public signature that a consumer cannot import. */
export interface SurfaceLeak {
  /** The unreachable type's name. */
  readonly type: string;
  /** Public exports whose signature mentions it, sorted. */
  readonly referencedBy: readonly string[];
}

/**
 * Types that the published surface names but does not export.
 *
 * Each one is a hole: the consumer is handed a value they cannot annotate. With
 * `isolatedDeclarations` on their side too, "cannot annotate" is "cannot
 * re-export", which is fatal rather than merely annoying.
 */
export function unreachablePublicTypes(): readonly SurfaceLeak[] {
  const { checker, symbol } = moduleSymbol(ROOT_BARREL);
  const exported = checker.getExportsOfModule(symbol);
  const publicNames = new Set(exported.map((s) => s.getName()));
  const found = new Map<string, Set<string>>();

  for (const entry of exported) {
    const target = resolveAlias(checker, entry);
    // `export * as v` — a whole module, not a type. Its members are namespaced
    // and reachable as `v.Foo`, so it has no reachability hole to report.
    if ((target.flags & (ts.SymbolFlags.Module | ts.SymbolFlags.ValueModule)) !== 0) continue;

    for (const declaration of target.getDeclarations() ?? []) {
      if (!declaration.getSourceFile().fileName.startsWith(SRC_DIR)) continue;
      const parameters = typeParameterNames(declaration);
      const walk = (node: ts.Node): void => {
        const identifier = referencedIdentifier(node);
        if (identifier !== undefined && !parameters.has(identifier.text)) {
          const raw = checker.getSymbolAtLocation(identifier);
          if (raw !== undefined) {
            const referenced = resolveAlias(checker, raw);
            const isTypeParameter = (referenced.flags & ts.SymbolFlags.TypeParameter) !== 0;
            const name = referenced.getName();
            if (!isTypeParameter && declaredInSrc(referenced) && !publicNames.has(name)) {
              const referrers = found.get(name) ?? new Set<string>();
              referrers.add(entry.getName());
              found.set(name, referrers);
            }
          }
        }
        forEachSignatureChild(node, walk);
      };
      walk(declaration);
    }
  }

  return [...found]
    .map(([type, referrers]): SurfaceLeak => ({ type, referencedBy: [...referrers].sort() }))
    .sort((a, b) => a.type.localeCompare(b.type));
}
