import { defineConfig } from '@playwright/test';

const chromium = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } }
  : { channel: 'chrome' };

export default defineConfig({
  testDir: './e2e-setup',
  outputDir: '../output/playwright-setup',
  reporter: 'list', workers: 1, timeout: 60_000,
  expect: { timeout: 10_000 },
  use: { trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'desktop', use: { ...chromium, viewport: { width: 1440, height: 960 } } },
    { name: 'mobile', use: { ...chromium, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: 'webkit-desktop', use: { browserName: 'webkit', viewport: { width: 1440, height: 960 } } },
    { name: 'webkit-mobile', use: { browserName: 'webkit', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
