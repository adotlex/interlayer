import { defineConfig, type ViteUserConfig } from 'vitest/config';

// The explicit annotation is required by `isolatedDeclarations` (TS9037:
// default exports cannot be inferred). Do not remove it.
const config: ViteUserConfig = defineConfig({
  test: {
    // Tests are CO-LOCATED inside each owned subtree under src/, plus the
    // shared harness smoke tests under test/support/.
    include: ['src/**/*.test.ts', 'test/**/*.test.ts'],
    environment: 'node',

    // Vitest 4 default is 'forks'. poolOptions was REMOVED in v4 —
    // these are all top-level now.
    pool: 'forks',
    maxWorkers: 4,
    fileParallelism: true,
    isolate: true,

    testTimeout: 5000,
    hookTimeout: 10000,
    teardownTimeout: 5000,

    includeTaskLocation: true, // populates assertionResults[].location {line,column}

    clearMocks: true,
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,

    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary', 'lcov'],
      reportsDirectory: './coverage',
      include: ['src/**/*.ts'],
      exclude: ['**/*.test.ts', '**/index.ts'],
      // Deliberately NO thresholds in build 1: gate on structure, not on a
      // percentage (setup guide §5.5). Coverage is reported, never gating.
    },
  },
});

export default config;
