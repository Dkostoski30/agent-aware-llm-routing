import { defineConfig } from 'vitest/config';

// The settings ng test adds to its own vitest setup (angular.json, runnerConfig; P3.13b).
export default defineConfig({
  test: {
    // vitest starts a worker per core but one, 15 on this laptop, each with its own jsdom. With the cluster up about
    // 1 GB is free and the workers were killed mid-run; 4 ran all 16 spec files (2026-11-18).
    maxWorkers: 4,
    // The on-disk module cache (node_modules/.vitest-cache) ran a spec against an earlier build when only the code it
    // imports had changed: clean code failed and a broken one passed, depending on the run before (2026-11-18).
    fsModuleCache: false,
  },
});
