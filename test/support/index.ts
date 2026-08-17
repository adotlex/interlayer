/**
 * Shared test harness — READ-ONLY to Wave 2.
 *
 * Import from here, not from the individual files:
 *
 * ```ts
 * import { createFakeRuntime, fakeProvider, scriptedRandom, testContext }
 *   from '../../../test/support/index.ts';
 * ```
 *
 * If something here is missing or wrong, STOP AND REPORT to the orchestrator.
 * Do not edit it, and do not keep a local copy of a harness type.
 */

export * from './context.ts';
export * from './fake-clock.ts';
export * from './fake-provider.ts';
export * from './seeded-random.ts';
