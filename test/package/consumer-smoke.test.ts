/**
 * A REAL CONSUMER'S FIRST FIVE MINUTES.
 *
 * Everything else in this directory inspects the package. This one USES it:
 * `test/package/fixtures/consumer.mjs` is spawned under plain `node`, with no
 * flags, no loader, no transpiler, and imports `interlayer` BY NAME so Node's
 * own `exports` resolution is in the path. It declares a contract, registers a
 * broken provider and a working one, and makes calls.
 *
 * This is the only test in the repository that exercises the artifact a user
 * installs rather than the source a developer edits. If `dist/` is subtly wrong
 * — an unrewritten specifier, a missing file, a barrel that re-exports
 * something the emit dropped — nothing else notices and this fails on import.
 *
 * SKIPS cleanly when `dist/` is absent so the shared suite never depends on a
 * build having been run.
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { beforeAll, describe, expect, it } from 'vitest';
import { REPO_ROOT } from './reflect.ts';

const FIXTURE = path.join(REPO_ROOT, 'test', 'package', 'fixtures', 'consumer.mjs');
const distBuilt = existsSync(path.join(REPO_ROOT, 'dist', 'index.js'));

interface Report {
  readonly reply: { readonly text: string };
  readonly flakyCalls: number;
  readonly metaProviderId: string;
  readonly metaProvider: string;
  readonly metaAttempts: number;
  readonly tryOk: boolean;
  readonly tryValue: { readonly text: string } | null;
  readonly introspection: {
    readonly providers: readonly string[];
    readonly supportsChat: boolean;
    readonly supportsNope: boolean;
    readonly contractKeys: readonly string[];
  };
  readonly pinnedIds: readonly string[];
  readonly pinnedReply: { readonly text: string };
  readonly unsupported: {
    readonly className: string;
    readonly code: string;
    readonly isInterlayerError: boolean;
    readonly hasCode: boolean;
    readonly instanceOfError: boolean;
  } | null;
  readonly successes: readonly string[];
  readonly validGood: boolean;
  readonly validBad: boolean;
  readonly afterClose: { readonly providers: number; readonly supportsChat: boolean };
  readonly hasAsyncDispose: boolean;
  readonly scopedClosed: boolean;
}

let report: Report | undefined;
let failure: string | undefined;

beforeAll(() => {
  if (!distBuilt) return;
  try {
    const stdout = execFileSync('node', [FIXTURE], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      timeout: 30_000,
    });
    report = JSON.parse(stdout) as Report;
  } catch (error) {
    const shape = error as { stderr?: string; message?: string };
    failure = shape.stderr ?? shape.message ?? String(error);
  }
});

function result(): Report {
  if (report === undefined) throw new Error(`consumer.mjs did not produce a report:\n${failure}`);
  return report;
}

describe.skipIf(!distBuilt)('a consumer importing the built package', () => {
  it('runs to completion under plain node, no flags', () => {
    expect(failure).toBeUndefined();
    expect(report).toBeDefined();
  });

  it('resolves `interlayer` through the exports map, not a relative path', () => {
    // The fixture's import specifier is the bare package name. Reaching this
    // assertion at all means Node self-resolved it to `dist/index.js`.
    expect(result().reply).toEqual({ text: 'echo:hi' });
  });

  it('falls back past a failing provider to a working one', () => {
    const r = result();
    expect(r.flakyCalls).toBe(2); // one per `chat` call: `call` and `tryCall`
    expect(r.reply.text).toBe('echo:hi');
    expect(r.metaProviderId).toBe('steady');
  });

  it('reports which provider answered, under both names', () => {
    const r = result();
    expect(r.metaProvider).toBe(r.metaProviderId);
    expect(r.metaAttempts).toBe(1);
  });

  it('returns a Result from tryCall that the exported guards narrow', () => {
    const r = result();
    expect(r.tryOk).toBe(true);
    expect(r.tryValue).toEqual({ text: 'echo:again' });
  });

  it('introspects the live layer', () => {
    expect(result().introspection).toEqual({
      providers: ['flaky', 'steady'],
      supportsChat: true,
      supportsNope: false,
      contractKeys: ['chat', 'embed'],
    });
  });

  it('narrows the chain with only()', () => {
    const r = result();
    expect(r.pinnedIds).toEqual(['steady']);
    expect(r.pinnedReply).toEqual({ text: 'echo:pinned' });
  });

  it('emits events to a subscriber added through the public `on()`', () => {
    // chat, embed, chat (tryCall), chat (pinned) — the fifth call is on a
    // different layer, and the unsubscribe runs before teardown.
    expect(result().successes).toEqual(['chat', 'embed', 'chat', 'chat']);
  });

  it('throws the exported error classes, with `code` and `instanceof` both intact', () => {
    expect(result().unsupported).toEqual({
      className: 'UnsupportedCapabilityError',
      code: 'UNSUPPORTED_CAPABILITY',
      isInterlayerError: true,
      hasCode: true,
      instanceOfError: true,
    });
  });

  it('survives `export * as v` across the emit', () => {
    // A namespace re-export is the one barrel form that can arrive empty at
    // runtime if the emit goes wrong; `v.object`/`v.string` prove it did not.
    const r = result();
    expect(r.validGood).toBe(true);
    expect(r.validBad).toBe(false);
  });

  it('tears down through close()', () => {
    expect(result().afterClose).toEqual({ providers: 0, supportsChat: false });
  });

  it('implements AsyncDisposable in the emitted JS', () => {
    // NOTE: the `await using` DECLARATION does not parse on Node 22, this
    // package's own `engines` floor — it needs V8 13 / Node 24. The fixture
    // therefore calls `[Symbol.asyncDispose]()` directly. The interface is
    // honoured on the floor; only the syntax sugar is not available there.
    const r = result();
    expect(r.hasAsyncDispose).toBe(true);
    expect(r.scopedClosed).toBe(true);
  });
});

describe.skipIf(distBuilt)('dist/ is absent', () => {
  it('skips the consumer smoke test instead of failing it', () => {
    expect(distBuilt).toBe(false);
  });
});
