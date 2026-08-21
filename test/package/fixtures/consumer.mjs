/**
 * THE FIRST FIVE MINUTES — a real consumer, in plain `node`, with no flags.
 *
 * This file is deliberately NOT TypeScript and deliberately NOT run by vitest.
 * It is spawned as `node test/package/fixtures/consumer.mjs` by
 * `consumer-smoke.test.ts`, so that what gets exercised is the EMITTED
 * `dist/index.js` under Node's own ESM loader — no transpiler, no bundler, no
 * path aliasing, no `--experimental-*`. If this file runs, a user can install
 * the package and use it.
 *
 * It imports through the package NAME (`interlayer`), not a relative path, so
 * the `exports` map is exercised too: Node self-reference resolution reads
 * `exports["."]` exactly as a consumer's `node_modules` copy would.
 *
 * Determinism: `retry` and `rateLimit` are switched OFF. Both would otherwise
 * schedule REAL timers against the real system runtime, and a smoke test that
 * sleeps is a broken test. Everything below completes in microseconds.
 *
 * Contract with the test: exactly one JSON object on stdout, and nothing else.
 * Diagnostics go to stderr.
 */

import {
  capability,
  createLayer,
  defineContract,
  defineProvider,
  hasCode,
  isInterlayerError,
  isOk,
  ProviderError,
  unwrap,
  v,
} from 'interlayer';

/* 1. Declare a contract. */
const ai = defineContract({
  chat: capability({ idempotent: false, description: 'one turn' }),
  embed: capability({ idempotent: true }),
});

/* 2. Two fake providers: one that always fails, one that works. */
let flakyCalls = 0;
const flaky = defineProvider(ai, {
  id: 'flaky',
  capabilities: {
    chat: async () => {
      flakyCalls += 1;
      throw new ProviderError('flaky is down', { providerId: 'flaky' });
    },
  },
});

const steady = defineProvider(ai, {
  id: 'steady',
  traits: { tags: ['cheap'] },
  capabilities: {
    chat: async (input) => ({ text: `echo:${input.prompt}` }),
    embed: async () => ({ vector: [1, 2, 3] }),
  },
});

/* 3. Build the layer. */
const layer = createLayer({
  contract: ai,
  providers: [flaky, steady],
  resilience: { retry: false, rateLimit: false },
});

const successes = [];
const off = layer.on('call:success', (payload) => successes.push(payload.capability));

/* 4. Make calls. `flaky` fails, the fallback chain reaches `steady`. */
const reply = await layer.call('chat', { prompt: 'hi' });
const meta = await layer.callWithMeta('embed', {});
const tried = await layer.tryCall('chat', { prompt: 'again' });

/* 5. Introspection — BEFORE close(); close() disposes the registry. */
const introspection = {
  providers: layer.providers.map((p) => p.id),
  supportsChat: layer.supports('chat'),
  supportsNope: layer.supports('nope'),
  contractKeys: Object.keys(layer.contract).sort(),
};

/* 6. only() narrows the chain. */
const pinned = layer.only('steady');
const pinnedIds = pinned.providers.map((p) => p.id);
const pinnedReply = await pinned.call('chat', { prompt: 'pinned' });

/* 7. Errors reach the consumer as the exported classes. */
let unsupported = null;
try {
  await layer.call('nope', {});
} catch (error) {
  unsupported = {
    className: error.constructor.name,
    code: error.code,
    isInterlayerError: isInterlayerError(error),
    hasCode: hasCode(error, 'UNSUPPORTED_CAPABILITY'),
    instanceOfError: error instanceof Error,
  };
}

/* 8. The validation namespace survives `export * as v`. */
const schema = v.object({ prompt: v.string() });
const validGood = schema.validate({ prompt: 'x' }).ok;
const validBad = schema.validate({ prompt: 1 }).ok;

/* 9. Teardown. */
off();
await layer.close();
const afterClose = { providers: layer.providers.length, supportsChat: layer.supports('chat') };

/* 10. The async-dispose seam.
 *
 * NOTE: this calls `layer[Symbol.asyncDispose]()` by hand rather than using
 * `await using`. The `await using` DECLARATION is a V8 13 feature and does not
 * parse on Node 22 — the `engines.node` floor of this package. The interface
 * is honoured either way; only the sugar is unavailable on the floor.
 */
const scoped = createLayer({
  contract: ai,
  providers: [steady],
  resilience: { retry: false, rateLimit: false },
});
const hasAsyncDispose = typeof scoped[Symbol.asyncDispose] === 'function';
await scoped.call('chat', { prompt: 'scoped' });
await scoped[Symbol.asyncDispose]();
const scopedClosed = scoped.providers.length === 0;

process.stdout.write(
  JSON.stringify({
    reply,
    flakyCalls,
    metaProviderId: meta.providerId,
    metaProvider: meta.provider,
    metaAttempts: meta.attempts,
    tryOk: isOk(tried),
    tryValue: isOk(tried) ? unwrap(tried) : null,
    introspection,
    pinnedIds,
    pinnedReply,
    unsupported,
    successes,
    validGood,
    validBad,
    afterClose,
    hasAsyncDispose,
    scopedClosed,
  }),
);
