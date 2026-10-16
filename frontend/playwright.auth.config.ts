import { defineConfig } from '@playwright/test';

const testPort = Number(process.env.FINANCE_AUTH_E2E_PORT || 8019);
if (!Number.isInteger(testPort) || testPort < 1024 || testPort > 65535) throw new Error('FINANCE_AUTH_E2E_PORT must be a port between 1024 and 65535');

export default defineConfig({
  testDir: './e2e-auth',
  outputDir: '../output/playwright-auth',
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${testPort}`,
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] },
  },
  webServer: {
    command: `${process.env.FINANCE_TEST_PYTHON || '../.venv/bin/python'} ../scripts/e2e_server.py --auth --port ${testPort}`,
    url: `http://127.0.0.1:${testPort}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
