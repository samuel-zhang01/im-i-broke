import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.FINANCE_E2E_PORT || 8029);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('FINANCE_E2E_PORT must be a port between 1024 and 65535');

export default defineConfig({
  testDir: './e2e',
  testMatch: ['investment-workspace.spec.ts', 'planning-checks.spec.ts', 'ui-uniformity.spec.ts', 'heading-dialog.spec.ts', 'transaction-workflow.spec.ts', 'webkit-smoke.spec.ts', 'banking-keyboard.spec.ts', 'banking-workspaces.spec.ts', 'banking.spec.ts', 'performance-diagnostics.spec.ts', 'loading-stability.spec.ts', 'audit-experience.spec.ts', 'settings-configuration-audit.spec.ts', 'settings-background-recovery.spec.ts', 'settings-preference-helpers.spec.ts', 'novice-csv.spec.ts', 'first-use-account-guidance.spec.ts', 'reporting-defaults.spec.ts'],
  outputDir: '../output/playwright-webkit/results',
  reporter: [['list'], ['html', { outputFolder: '../output/playwright-webkit/report', open: 'never' }]],
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: { baseURL: `http://127.0.0.1:${port}`, browserName: 'webkit', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'webkit-desktop', use: { ...devices['Desktop Safari'], viewport: { width: 1440, height: 960 } } },
    { name: 'webkit-iphone', use: { ...devices['iPhone 13'] } },
  ],
  webServer: {
    command: `${process.env.FINANCE_TEST_PYTHON || '../.venv/bin/python'} ../scripts/e2e_server.py --port ${port}`,
    url: `http://127.0.0.1:${port}/api/health`, reuseExistingServer: false, timeout: 30_000,
  },
});
