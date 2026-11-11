import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: 'tests', testMatch: '*.spec.ts', timeout: 90000,
  workers: 1, reporter: 'list', outputDir: 'test-results' });
