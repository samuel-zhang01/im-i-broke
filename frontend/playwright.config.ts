import { defineConfig } from '@playwright/test';

const testPort = Number(process.env.FINANCE_E2E_PORT || 8018);
if (!Number.isInteger(testPort) || testPort < 1024 || testPort > 65535) throw new Error('FINANCE_E2E_PORT must be a port between 1024 and 65535');

export default defineConfig({
  testDir: './e2e',
  outputDir: '../output/playwright/results',
  reporter: [['list'], ['html', { outputFolder: '../output/playwright/report', open: 'never' }]],
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // A fresh, isolated browser profile; never attaches to personal browser tabs.
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } }
      : { channel: 'chrome' }),
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 960 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: `${process.env.FINANCE_TEST_PYTHON || '../.venv/bin/python'} ../scripts/e2e_server.py --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
