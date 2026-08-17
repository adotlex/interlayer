import { describe, expect, it } from 'vitest';
import { hasCode, TransportError } from '../../src/core/errors.ts';
import { testContext } from './context.ts';
import { fakeProvider } from './fake-provider.ts';

describe('fakeProvider', () => {
  it('exposes a type-erased ProviderRecord with the declared capabilities', () => {
    const p = fakeProvider('alpha', { capabilities: ['chat', 'embed'], priority: 3 });
    expect(p.record.id).toBe('alpha');
    expect(p.record.priority).toBe(3);
    expect(p.record.enabled).toBe(true);
    expect([...p.record.capabilities.keys()]).toEqual(['chat', 'embed']);
  });

  it('plays scripted outcomes in order and logs every call', async () => {
    const boom = new TransportError('boom');
    const p = fakeProvider('flaky', { capabilities: ['chat'] })
      .failWith(boom, 2)
      .alwaysSucceed({ text: 'ok' });
    const h = testContext({ capability: 'chat', provider: p });
    const handler = p.handler('chat');

    await expect(handler({ prompt: 'a' }, h.attempt)).rejects.toBe(boom);
    await expect(handler({ prompt: 'b' }, h.attempt)).rejects.toBe(boom);
    await expect(handler({ prompt: 'c' }, h.attempt)).resolves.toEqual({ text: 'ok' });
    await expect(handler({ prompt: 'd' }, h.attempt)).resolves.toEqual({ text: 'ok' });

    expect(p.callCount).toBe(4);
    expect(p.calls.map((c) => c.input)).toEqual([
      { prompt: 'a' },
      { prompt: 'b' },
      { prompt: 'c' },
      { prompt: 'd' },
    ]);
    expect(p.calls[0]?.capability).toBe('chat');
    expect(p.calls[0]?.attempt).toBe(1);
  });

  it('spends delayMs on the virtual clock', async () => {
    const p = fakeProvider('slow').succeed('done', 1, 30_000);
    const h = testContext({ provider: p });
    const promise = p.handler()('in', h.attempt);
    await h.runtime.advance(30_000);
    await expect(promise).resolves.toBe('done');
    expect(p.calls[0]?.at).toBe(0);
    expect(h.runtime.now()).toBe(30_000);
    expect(h.runtime.pendingTimers).toBe(0);
  });

  it('reports script exhaustion as a ConfigError rather than passing silently', async () => {
    const p = fakeProvider('short').succeed('once');
    const h = testContext({ provider: p });
    await expect(p.handler()('in', h.attempt)).resolves.toBe('once');
    await expect(p.handler()('in', h.attempt)).rejects.toSatisfy((e: unknown) =>
      hasCode(e, 'CONFIG'),
    );
  });

  it('refuses a capability it does not declare', () => {
    const p = fakeProvider('alpha', { capabilities: ['chat'] });
    expect(() => p.handler('embed')).toThrow(/does not declare "embed"/);
  });

  it('throws CancelledError when the attempt signal is already aborted', async () => {
    const p = fakeProvider('alpha').alwaysSucceed('ok');
    const h = testContext({ provider: p });
    h.abort();
    await expect(p.handler()('in', h.attempt)).rejects.toSatisfy((e: unknown) =>
      hasCode(e, 'CANCELLED'),
    );
    expect(p.callCount).toBe(0);
  });
});

describe('testContext', () => {
  it('shares stats and failures by reference across a {...ctx} derivation', () => {
    const h = testContext();
    const derived = h.withAttempt({ attempt: 2 });
    derived.stats.attempts++;
    derived.failures.push(new TransportError('x'));
    expect(h.stats.attempts).toBe(1);
    expect(h.failures).toHaveLength(1);
    expect(derived.attempt).toBe(2);
    expect(h.attempt.attempt).toBe(1);
  });

  it('records emitted events in order', () => {
    const h = testContext();
    h.events.emit('attempt:start', { callId: 'c1', providerId: 'p', attempt: 1, at: 0 });
    h.events.emit('retry:scheduled', {
      callId: 'c1',
      providerId: 'p',
      attempt: 1,
      delayMs: 100,
      at: 0,
    });
    expect(h.recorded.map((e) => e.name)).toEqual(['attempt:start', 'retry:scheduled']);
    expect(h.emitted('retry:scheduled')[0]?.delayMs).toBe(100);
  });
});
