import { describe, expect, it } from 'vitest';
import { hasCode } from '../../src/core/errors.ts';
import { createFakeClock, createFakeRuntime } from './fake-clock.ts';
import { scriptedRandom } from './seeded-random.ts';

describe('fake clock', () => {
  it('starts at 0 and advances by exactly the requested amount', async () => {
    const clock = createFakeClock();
    expect(clock.now()).toBe(0);
    await clock.advance(1234);
    expect(clock.now()).toBe(1234);
  });

  it('fires timers in due order, ties broken by insertion order', async () => {
    const clock = createFakeClock();
    const fired: string[] = [];
    clock.setTimeout(() => fired.push('c'), 30);
    clock.setTimeout(() => fired.push('a'), 10);
    clock.setTimeout(() => fired.push('b'), 10);
    await clock.advance(30);
    expect(fired).toEqual(['a', 'b', 'c']);
  });

  it('does not fire a timer before it is due', async () => {
    const clock = createFakeClock();
    let fired = false;
    clock.setTimeout(() => {
      fired = true;
    }, 100);
    await clock.advance(99);
    expect(fired).toBe(false);
    await clock.advance(1);
    expect(fired).toBe(true);
  });

  it('clearTimeout removes a pending timer', async () => {
    const clock = createFakeClock();
    let fired = false;
    const h = clock.setTimeout(() => {
      fired = true;
    }, 10);
    expect(clock.pendingTimers).toBe(1);
    clock.clearTimeout(h);
    expect(clock.pendingTimers).toBe(0);
    await clock.advance(1000);
    expect(fired).toBe(false);
  });
});

describe('fake runtime', () => {
  it('completes a one-hour sleep in virtual time and records it', async () => {
    const rt = createFakeRuntime();
    const started = Date.now();
    const p = rt.sleep(3_600_000);
    await rt.advance(3_600_000);
    await p;
    expect(rt.now()).toBe(3_600_000);
    expect(rt.slept).toEqual([3_600_000]);
    // The whole "hour" cost single-digit milliseconds of wall time.
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it('flushes microtasks between timers so await-chained sleeps progress', async () => {
    const rt = createFakeRuntime();
    const seen: number[] = [];
    const chain = (async (): Promise<void> => {
      await rt.sleep(100);
      seen.push(rt.now());
      await rt.sleep(200); // only scheduled once the first sleep resolves
      seen.push(rt.now());
    })();
    await rt.advance(300);
    await chain;
    expect(seen).toEqual([100, 300]);
    expect(rt.pendingTimers).toBe(0);
  });

  it('leaves no pending timers after a settled sleep', async () => {
    const rt = createFakeRuntime();
    const p = rt.sleep(50);
    expect(rt.pendingTimers).toBe(1);
    await rt.advance(50);
    await p;
    expect(rt.pendingTimers).toBe(0);
  });

  it('rejects a sleep with CancelledError when aborted mid-flight, clearing the timer', async () => {
    const rt = createFakeRuntime();
    const ac = new AbortController();
    const p = rt.sleep(1000, ac.signal);
    const assertion = expect(p).rejects.toSatisfy((e: unknown) => hasCode(e, 'CANCELLED'));
    ac.abort();
    await assertion;
    expect(rt.pendingTimers).toBe(0);
  });

  it('deadline() aborts on the virtual clock and dispose() clears the timer', async () => {
    const rt = createFakeRuntime();
    const d = rt.deadline(500);
    expect(d.signal.aborted).toBe(false);
    await rt.advance(499);
    expect(d.signal.aborted).toBe(false);
    await rt.advance(1);
    expect(d.signal.aborted).toBe(true);
    expect(hasCode(d.signal.reason, 'CANCELLED')).toBe(true);

    const d2 = rt.deadline(500);
    expect(rt.pendingTimers).toBe(1);
    d2.dispose();
    expect(rt.pendingTimers).toBe(0);
  });

  it('issues stable, counter-based uuids', () => {
    const rt = createFakeRuntime();
    expect([rt.uuid(), rt.uuid()]).toEqual(['test-00000001', 'test-00000002']);
  });

  it('shares one clock between the Runtime and Clock/Timers views', async () => {
    const clock = createFakeClock({ startTime: 1_000 });
    const rt = createFakeRuntime({ clock });
    expect(rt.now()).toBe(1_000);
    await rt.advance(5);
    expect(clock.now()).toBe(1_005);
  });

  it('accepts an injected Random so jitter is exactly predictable', () => {
    const rt = createFakeRuntime({ random: scriptedRandom([0.5, 0.25]) });
    expect([rt.random(), rt.random(), rt.random()]).toEqual([0.5, 0.25, 0.25]);
  });
});
