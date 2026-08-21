/**
 * THE ERROR CONTRACT: every `ErrorCode` member, reached through the PUBLIC API.
 *
 * `ErrorCode` has thirteen members. A code nobody can produce is dead weight in
 * an exhaustive `switch`; a code that can be produced but arrives mislabelled is
 * worse, because every consumer's error handling is then wrong in a way no type
 * checker can see. This file drives one call per code through `createLayer` and
 * asserts what the caller actually catches.
 *
 * The reachability table at the bottom is typed `Record<ErrorCode, …>`, so
 * adding a fourteenth code to the union fails THIS FILE to compile until
 * somebody says how it is reached.
 *
 * Determinism: `createFakeRuntime()` everywhere, `settle()` as the crank. No
 * test in this file sleeps.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { hasCode, ProviderError, TransportError } from '../../src/core/errors.ts';
import type { CallMiddleware, ErrorCode } from '../../src/core/types.ts';
import { object, parse, string } from '../../src/core/validate.ts';
import { createLayer } from '../../src/layer.ts';
import { defineProvider } from '../../src/registry/index.ts';
import { createFakeRuntime } from '../support/index.ts';
import {
  type Ask,
  NO_POLICIES,
  neverSettles,
  probe,
  rejectionOf,
  type Say,
  settle,
  stripCapability,
} from './harness.ts';

/* ------------------------------------------------------------------ *
 * VALIDATION
 * ------------------------------------------------------------------ */

describe('ErrorCode: VALIDATION', () => {
  it('a handler that rejects its input surfaces VALIDATION with the issues intact', async () => {
    const runtime = createFakeRuntime();
    const strict = defineProvider(probe, {
      id: 'strict',
      capabilities: {
        run: async (input): Promise<Say> => {
          const shape = object({ q: string() });
          const parsed = parse(input, shape, 'input');
          return { a: parsed.q, by: 'strict' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [strict], runtime });

    // `q` is a number, not a string. The cast is the point: a real caller can
    // hand a validated boundary anything at runtime.
    const bad = { q: 7 } as unknown as Ask;
    const error = await rejectionOf(runtime, layer.call('run', bad));

    expect(hasCode(error, 'VALIDATION')).toBe(true);
    if (!hasCode(error, 'VALIDATION')) return;
    expect(error.issues).toHaveLength(1);
    expect(error.issues[0]?.path).toEqual(['q']);
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  it('VALIDATION never advances the fallback chain — one caller mistake, one hop', async () => {
    const runtime = createFakeRuntime();
    let healthyCalls = 0;
    const picky = defineProvider(probe, {
      id: 'picky',
      capabilities: {
        run: async (input): Promise<Say> => ({
          a: parse(input, object({ q: string() })).q,
          by: 'p',
        }),
      },
    });
    const healthy = defineProvider(probe, {
      id: 'healthy',
      capabilities: {
        run: async (): Promise<Say> => {
          healthyCalls++;
          return { a: 'ok', by: 'healthy' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [picky, healthy], runtime });

    const error = await rejectionOf(runtime, layer.call('run', { q: 7 } as unknown as Ask));

    expect(hasCode(error, 'VALIDATION')).toBe(true);
    // Walking the chain would just multiply one caller mistake by N providers.
    expect(healthyCalls).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * CONFIG
 * ------------------------------------------------------------------ */

describe('ErrorCode: CONFIG', () => {
  it('a duplicate provider id is a CONFIG error at construction', () => {
    const runtime = createFakeRuntime();
    const one = defineProvider(probe, {
      id: 'same',
      capabilities: { run: async (): Promise<Say> => ({ a: '', by: 'one' }) },
    });
    const two = defineProvider(probe, {
      id: 'same',
      capabilities: { run: async (): Promise<Say> => ({ a: '', by: 'two' }) },
    });

    try {
      createLayer({ contract: probe, providers: [one, two], runtime });
      expect.unreachable('expected a duplicate id to be rejected');
    } catch (error) {
      expect(hasCode(error, 'CONFIG')).toBe(true);
      if (!hasCode(error, 'CONFIG')) return;
      expect(error.message).toContain('duplicate provider id');
      expect(error.providerId).toBe('same');
      expect(error.retryable).toBe(false);
    }
  });

  it('middleware that re-enters next() concurrently surfaces CONFIG on the call path', async () => {
    const runtime = createFakeRuntime();
    const ok = defineProvider(probe, {
      id: 'ok',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'ok' }) },
    });
    const layer = createLayer({ contract: probe, providers: [ok], runtime });

    // koa's classic "missing await": two overlapping downstream chains.
    const doubleNext: CallMiddleware = async (_ctx, next) => {
      const inFlight = next();
      try {
        return await next();
      } finally {
        await inFlight.catch(() => undefined);
      }
    };
    layer.use(doubleNext);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'CONFIG')).toBe(true);
    expect(error.message).toContain('called next() again');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * UNSUPPORTED_CAPABILITY
 * ------------------------------------------------------------------ */

describe('ErrorCode: UNSUPPORTED_CAPABILITY', () => {
  it('nothing declares the capability at all -> UNSUPPORTED_CAPABILITY, before any provider', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const only = defineProvider(probe, {
      id: 'only',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: 'ok', by: 'only' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [only], runtime });

    // The last implementer leaves; the capability is no longer declared by
    // anyone, which is a different fact from "declared but none available".
    expect(layer.registry.unregister('only')).toBe(true);
    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'UNSUPPORTED_CAPABILITY')).toBe(true);
    expect(error.capability).toBe('run');
    expect(error.providerId).toBeUndefined();
    expect(calls).toBe(0);
    await layer.close();
  });

  it('a provider that loses its handler mid-flight -> UNSUPPORTED_CAPABILITY stamped with its id', async () => {
    const runtime = createFakeRuntime();
    const vanishing = defineProvider(probe, {
      id: 'vanishing',
      capabilities: { run: async (): Promise<Say> => ({ a: 'never', by: 'vanishing' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [vanishing],
      runtime,
      resilience: NO_POLICIES,
    });

    // The registry's capability INDEX still lists it — `supports()` passes and
    // `candidatesFor()` returns the record — but the record's handler map has
    // been emptied. This is the branch `runHandler` keeps for exactly this case.
    layer.use(async (_ctx, next) => {
      const record = layer.registry.get('vanishing');
      if (record !== undefined) stripCapability(record, 'run');
      return await next();
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'UNSUPPORTED_CAPABILITY')).toBe(true);
    expect(error.providerId).toBe('vanishing');
    expect(error.capability).toBe('run');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * NO_PROVIDER
 * ------------------------------------------------------------------ */

describe('ErrorCode: NO_PROVIDER', () => {
  it('declared but every implementer disabled -> NO_PROVIDER (not UNSUPPORTED_CAPABILITY)', async () => {
    const runtime = createFakeRuntime();
    const sleeping = defineProvider(probe, {
      id: 'sleeping',
      capabilities: { run: async (): Promise<Say> => ({ a: '', by: 'sleeping' }) },
    });
    const layer = createLayer({ contract: probe, providers: [sleeping], runtime });

    expect(layer.registry.setEnabled('sleeping', false)).toBe(true);
    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    // Two facts, two errors: "nobody implements it" vs "none available now".
    expect(hasCode(error, 'NO_PROVIDER')).toBe(true);
    expect(error.capability).toBe('run');
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  it('a providers hint matching nothing -> NO_PROVIDER naming the candidate count', async () => {
    const runtime = createFakeRuntime();
    const real = defineProvider(probe, {
      id: 'real',
      capabilities: { run: async (): Promise<Say> => ({ a: '', by: 'real' }) },
    });
    const layer = createLayer({ contract: probe, providers: [real], runtime });

    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { providers: ['ghost'] }),
    );

    expect(hasCode(error, 'NO_PROVIDER')).toBe(true);
    expect(error.message).toContain('routing hints');
    await layer.close();
  });

  it('maxProviders: 0 -> NO_PROVIDER, and no handler is entered', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const real = defineProvider(probe, {
      id: 'real',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: '', by: 'real' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [real], runtime, maxProviders: 0 });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'NO_PROVIDER')).toBe(true);
    expect(error.message).toContain('maxProviders=0');
    expect(calls).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * TRANSPORT
 * ------------------------------------------------------------------ */

describe('ErrorCode: TRANSPORT', () => {
  it('a provider throwing TransportError surfaces TRANSPORT, retryable, id stamped', async () => {
    const runtime = createFakeRuntime();
    const down = defineProvider(probe, {
      id: 'down',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new TransportError('socket hang up');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [down],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TRANSPORT')).toBe(true);
    expect(error.message).toBe('socket hang up');
    // The thrower had no idea of its own id; the router stamped it on the way out.
    expect(error.providerId).toBe('down');
    expect(error.capability).toBe('run');
    expect(error.retryable).toBe(true);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * TIMEOUT — both scopes
 * ------------------------------------------------------------------ */

describe('ErrorCode: TIMEOUT', () => {
  it('a handler that never settles trips the ATTEMPT timeout', async () => {
    const runtime = createFakeRuntime();
    const hung = defineProvider(probe, {
      id: 'hung',
      capabilities: { run: neverSettles() },
    });
    const layer = createLayer({
      contract: probe,
      providers: [hung],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        rateLimit: false,
        timeout: { attemptTimeoutMs: 200 },
      },
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('attempt');
    expect(error.timeoutMs).toBe(200);
    expect(runtime.now()).toBe(200); // exact virtual instant, not a range
    // Attempt-scoped timeouts ARE retryable: a different try may be quicker.
    expect(error.retryable).toBe(true);
    await layer.close();
  });

  it('an absolute deadlineAt breach trips the CALL timeout even with every policy off', async () => {
    const runtime = createFakeRuntime();
    const hung = defineProvider(probe, { id: 'hung', capabilities: { run: neverSettles() } });
    const layer = createLayer({
      contract: probe,
      providers: [hung],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }, { deadlineAt: 750 }));

    // With no timeout policy the facade is the only thing watching the signal.
    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('call');
    expect(runtime.now()).toBe(750);
    expect(error.retryable).toBe(false); // call-scoped: the whole budget is gone
    await layer.close();
  });

  it('a deadlineAt already in the past fails immediately, without entering a provider', async () => {
    const runtime = createFakeRuntime({ startTime: 1_000 });
    let calls = 0;
    const fine = defineProvider(probe, {
      id: 'fine',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: 'ok', by: 'fine' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [fine], runtime });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }, { deadlineAt: 500 }));

    expect(hasCode(error, 'TIMEOUT')).toBe(true);
    if (!hasCode(error, 'TIMEOUT')) return;
    expect(error.scope).toBe('call');
    expect(error.timeoutMs).toBe(0);
    expect(calls).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * CANCELLED
 * ------------------------------------------------------------------ */

describe('ErrorCode: CANCELLED', () => {
  it('an already-aborted caller signal surfaces CANCELLED with zero invocations', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const fine = defineProvider(probe, {
      id: 'fine',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          return { a: 'ok', by: 'fine' };
        },
      },
    });
    const layer = createLayer({ contract: probe, providers: [fine], runtime });

    const controller = new AbortController();
    controller.abort();
    const error = await rejectionOf(
      runtime,
      layer.call('run', { q: 'x' }, { signal: controller.signal }),
    );

    expect(hasCode(error, 'CANCELLED')).toBe(true);
    expect(error.retryable).toBe(false);
    expect(calls).toBe(0);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * RATE_LIMITED
 * ------------------------------------------------------------------ */

describe('ErrorCode: RATE_LIMITED', () => {
  it('an exhausted bucket in reject mode surfaces RATE_LIMITED with a usable retryAfterMs', async () => {
    const runtime = createFakeRuntime();
    const paced = defineProvider(probe, {
      id: 'paced',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'paced' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [paced],
      runtime,
      resilience: {
        retry: false,
        breaker: false,
        timeout: false,
        // One token, refilled once a second: the second call in the same
        // instant cannot be paid for.
        rateLimit: { capacity: 1, refillPerSec: 1, onExhaustion: 'reject' },
      },
    });

    const first = await settle(runtime, layer.call('run', { q: 'x' }));
    expect(first.by).toBe('paced');

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'RATE_LIMITED')).toBe(true);
    if (!hasCode(error, 'RATE_LIMITED')) return;
    expect(error.retryAfterMs).toBe(1_000); // one token at 1/s
    expect(error.retryable).toBe(true);
    expect(error.providerId).toBe('paced');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * CIRCUIT_OPEN
 * ------------------------------------------------------------------ */

describe('ErrorCode: CIRCUIT_OPEN', () => {
  it('a tripped breaker fails the next call fast, with retryAfterMs counted from NOW', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const dying = defineProvider(probe, {
      id: 'dying',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          throw new TransportError('refused');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      resilience: {
        retry: false,
        rateLimit: false,
        timeout: false,
        breaker: { minimumThroughput: 2, failureRatio: 0.5, resetMs: 10_000 },
      },
    });

    await rejectionOf(runtime, layer.call('run', { q: 'x' })); // 1st failure
    await rejectionOf(runtime, layer.call('run', { q: 'x' })); // 2nd -> trips
    expect(calls).toBe(2);

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'CIRCUIT_OPEN')).toBe(true);
    if (!hasCode(error, 'CIRCUIT_OPEN')) return;
    expect(error.key).toBe('dying');
    expect(error.retryAfterMs).toBe(10_000);
    // Fail FAST: the third call never reached the provider.
    expect(calls).toBe(2);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * BULKHEAD_FULL — reserved
 * ------------------------------------------------------------------ */

/** Every non-test `.ts` file under `src/`, as source text. */
function readSourceFiles(dir: string): readonly { readonly path: string; readonly text: string }[] {
  const out: { path: string; text: string }[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...readSourceFiles(full));
    } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.test.ts')) {
      out.push({ path: full, text: readFileSync(full, 'utf8') });
    }
  }
  return out;
}

describe('ErrorCode: BULKHEAD_FULL (reserved)', () => {
  const SRC = join(import.meta.dirname, '..', '..', 'src');

  it('is genuinely unreachable: no policy in src/ ever constructs a BulkheadFullError', () => {
    const constructors = readSourceFiles(SRC)
      .filter(
        (file) =>
          // `errors.ts` DECLARES the class; that is not a construction site.
          !file.path.endsWith(`${join('core', 'errors.ts')}`) &&
          file.text.includes('new BulkheadFullError'),
      )
      .map((file) => file.path);

    // If this ever fails, BULKHEAD_FULL is reachable and the row in the
    // reachability table below is a lie — go and test the real path.
    expect(constructors).toEqual([]);
  });

  it('no bulkhead policy kind exists in the canonical ordering', async () => {
    const { POLICY_ORDER } = await import('../../src/core/policy.ts');
    expect(POLICY_ORDER).not.toContain('bulkhead');
    // Reserved, not forgotten: the code, the class and the union member exist so
    // a future bulkhead unit slots in without a breaking change to `ErrorCode`.
    const { BulkheadFullError } = await import('../../src/core/errors.ts');
    const sample = new BulkheadFullError('pool', 4);
    expect(sample.code).toBe('BULKHEAD_FULL');
    expect(sample.retryable).toBe(true);
  });

  it('reserved, but the plumbing would carry it: a handler throwing one still falls back', async () => {
    const runtime = createFakeRuntime();
    const { BulkheadFullError } = await import('../../src/core/errors.ts');
    const full = defineProvider(probe, {
      id: 'full',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new BulkheadFullError('pool', 1);
        },
      },
    });
    const spare = defineProvider(probe, {
      id: 'spare',
      capabilities: { run: async (): Promise<Say> => ({ a: 'ok', by: 'spare' }) },
    });
    const layer = createLayer({
      contract: probe,
      providers: [full, spare],
      runtime,
      resilience: NO_POLICIES,
    });

    const reply = await settle(runtime, layer.call('run', { q: 'x' }));

    // Proves the code is unreachable because nothing MAKES one, not because the
    // taxonomy would mishandle it if something did.
    expect(reply.by).toBe('spare');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * PROVIDER_ERROR
 * ------------------------------------------------------------------ */

describe('ErrorCode: PROVIDER_ERROR', () => {
  it('an unclassified handler throw becomes PROVIDER_ERROR with the original as cause', async () => {
    const runtime = createFakeRuntime();
    const root = new Error('upstream said no');
    const buggy = defineProvider(probe, {
      id: 'buggy',
      capabilities: {
        run: async (): Promise<Say> => {
          throw root;
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [buggy],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    expect(error.message).toBe('upstream said no');
    expect(error.cause).toBe(root); // identity, not a copy
    // No status to classify from, so a repeat against the SAME provider is futile.
    expect(error.retryable).toBe(false);
    await layer.close();
  });

  it('an HTTP-ish status on the throw is lifted onto ProviderError.status', async () => {
    const runtime = createFakeRuntime();
    const flaky = defineProvider(probe, {
      id: 'flaky',
      capabilities: {
        run: async (): Promise<Say> => {
          throw Object.assign(new Error('bad gateway'), { status: 502 });
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [flaky],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'PROVIDER_ERROR')).toBe(true);
    if (!hasCode(error, 'PROVIDER_ERROR')) return;
    expect(error.status).toBe(502);
    expect(error.retryable).toBe(true);
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * RETRY_EXHAUSTED
 * ------------------------------------------------------------------ */

describe('ErrorCode: RETRY_EXHAUSTED', () => {
  it('two failed attempts against one provider aggregate into RETRY_EXHAUSTED', async () => {
    const runtime = createFakeRuntime();
    let calls = 0;
    const dying = defineProvider(probe, {
      id: 'dying',
      capabilities: {
        run: async (): Promise<Say> => {
          calls++;
          throw new TransportError(`attempt ${String(calls)}`);
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [dying],
      runtime,
      resilience: {
        breaker: false,
        rateLimit: false,
        timeout: false,
        retry: { maxAttempts: 2, strategy: 'fixed', baseDelayMs: 100 },
      },
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'RETRY_EXHAUSTED')).toBe(true);
    if (!hasCode(error, 'RETRY_EXHAUSTED')) return;
    expect(error.attempts).toBe(2);
    expect(calls).toBe(2);
    expect(runtime.now()).toBe(100); // exactly one fixed backoff
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * ALL_FAILED
 * ------------------------------------------------------------------ */

describe('ErrorCode: ALL_FAILED', () => {
  it('two failed providers aggregate into ALL_FAILED naming both', async () => {
    const runtime = createFakeRuntime();
    const a = defineProvider(probe, {
      id: 'a',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new TransportError('a down');
        },
      },
    });
    const b = defineProvider(probe, {
      id: 'b',
      capabilities: {
        run: async (): Promise<Say> => {
          throw new ProviderError('b refused');
        },
      },
    });
    const layer = createLayer({
      contract: probe,
      providers: [a, b],
      runtime,
      resilience: NO_POLICIES,
    });

    const error = await rejectionOf(runtime, layer.call('run', { q: 'x' }));

    expect(hasCode(error, 'ALL_FAILED')).toBe(true);
    if (!hasCode(error, 'ALL_FAILED')) return;
    expect(error.triedProviderIds).toEqual(['a', 'b']);
    expect(error.failures.map((f) => f.code)).toEqual(['TRANSPORT', 'PROVIDER_ERROR']);
    expect(error.message).toContain('a=TRANSPORT');
    expect(error.message).toContain('b=PROVIDER_ERROR');
    await layer.close();
  });
});

/* ------------------------------------------------------------------ *
 * The table — exhaustive BY CONSTRUCTION
 * ------------------------------------------------------------------ */

type Reachability =
  | { readonly kind: 'reachable'; readonly how: string }
  | { readonly kind: 'reserved'; readonly why: string };

/**
 * Typed `Record<ErrorCode, …>`, so a fourteenth member of the union is a
 * COMPILE error here until somebody records how it is produced. This is the
 * only mechanism in the repo that makes the taxonomy's completeness checkable.
 */
const REACHABILITY: Record<ErrorCode, Reachability> = {
  VALIDATION: { kind: 'reachable', how: 'handler rejects its input via v.parse' },
  CONFIG: { kind: 'reachable', how: 'duplicate provider id; middleware re-entering next()' },
  UNSUPPORTED_CAPABILITY: {
    kind: 'reachable',
    how: 'no provider declares it, or a handler disappears mid-flight',
  },
  NO_PROVIDER: {
    kind: 'reachable',
    how: 'all implementers disabled; hint matches none; maxProviders 0',
  },
  TRANSPORT: { kind: 'reachable', how: 'handler throws TransportError; passes through unchanged' },
  TIMEOUT: { kind: 'reachable', how: 'attempt timeout, total timeout, or a deadlineAt breach' },
  CANCELLED: { kind: 'reachable', how: 'caller aborts the signal at any layer of the stack' },
  RATE_LIMITED: {
    kind: 'reachable',
    how: 'exhausted token bucket in reject mode, or a full queue',
  },
  CIRCUIT_OPEN: { kind: 'reachable', how: 'breaker trips and fails the next call fast' },
  BULKHEAD_FULL: {
    kind: 'reserved',
    why: 'no bulkhead unit was built; nothing in src/ constructs one and POLICY_ORDER has no such kind',
  },
  PROVIDER_ERROR: {
    kind: 'reachable',
    how: 'any unclassified handler throw, via toInterlayerError',
  },
  RETRY_EXHAUSTED: { kind: 'reachable', how: '>= 2 failed attempts against one provider' },
  ALL_FAILED: { kind: 'reachable', how: '>= 2 failed providers in one fallback chain' },
};

describe('the taxonomy as a whole', () => {
  it('every ErrorCode is either reached by a test above or documented as reserved', () => {
    const reserved = Object.entries(REACHABILITY)
      .filter(([, value]) => value.kind === 'reserved')
      .map(([code]) => code);

    expect(Object.keys(REACHABILITY)).toHaveLength(13);
    // Exactly one reserved code, and it is the one the brief names.
    expect(reserved).toEqual(['BULKHEAD_FULL']);
  });

  it('every reserved entry carries a reason and every reachable entry a mechanism', () => {
    for (const [code, value] of Object.entries(REACHABILITY)) {
      const text = value.kind === 'reserved' ? value.why : value.how;
      expect(text.length, `${code} has an empty explanation`).toBeGreaterThan(10);
    }
  });
});
