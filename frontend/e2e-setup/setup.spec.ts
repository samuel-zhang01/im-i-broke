import { test as base, expect } from '@playwright/test';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';

const python = process.env.FINANCE_TEST_PYTHON || '../.venv/bin/python';
const test = base.extend<{ fresh: { url: string; token: string } }>({
  fresh: async ({}, use) => {
    const folder = await mkdtemp(join(tmpdir(), 'ledger-browser-setup-'));
    const fixture = join(folder, 'fixture.json');
    const listener = createServer();
    await new Promise<void>(resolve => listener.listen(0, '127.0.0.1', resolve));
    const port = (listener.address() as { port: number }).port;
    await new Promise<void>((resolve, reject) => listener.close(error => error ? reject(error) : resolve()));
    const url = `http://127.0.0.1:${port}`;
    const server = spawn(python, ['../scripts/e2e_server.py', '--fresh', '--fixture-file', fixture, '--port', String(port)], { stdio: ['ignore', 'pipe', 'pipe'] });
    let log = ''; server.stdout.on('data', chunk => { log += chunk; }); server.stderr.on('data', chunk => { log += chunk; });
    const closed = new Promise<void>(resolve => server.on('exit', () => resolve()));
    try {
      await expect.poll(async () => {
        if (server.exitCode !== null) throw new Error(`Isolated setup server exited: ${log}`);
        try { return (await fetch(`${url}/api/health`)).status; } catch { return 0; }
      }, { timeout: 20_000 }).toBe(200);
      const { token } = JSON.parse(await readFile(fixture, 'utf8'));
      await use({ url, token });
    } finally {
      server.kill('SIGTERM');
      const timer = setTimeout(() => server.kill('SIGKILL'), 5000);
      await closed; clearTimeout(timer);
      await rm(folder, { recursive: true, force: true });
    }
  },
});

test('fresh owner enrolls, adds a euro account, imports CSV and signs in again through the real UI', async ({ page, context, fresh }, info) => {
  const errors: string[] = []; const unexpectedRequests: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin !== fresh.url) { unexpectedRequests.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await page.goto(fresh.url);
  await expect(page.getByRole('heading', { name: 'Set up your workspace', exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('01-welcome.png'), fullPage: true });
  await page.getByLabel('Setup token', { exact: true }).fill(fresh.token);
  await page.getByRole('button', { name: 'Begin secure setup', exact: true }).click();
  await expect(page.getByRole('img', { name: 'Scan to add Am I Broke? to your authenticator' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Make it yours. Keep it private.' })).toBeFocused();
  await page.screenshot({ path: info.outputPath('02-authenticator.png'), fullPage: true });
  await page.getByText('Can’t scan? Use a setup key', { exact: true }).click();
  const secret = (await page.getByLabel('Authenticator setup key', { exact: true }).textContent())!;
  await page.getByRole('button', { name: 'Copy setup key', exact: true }).click();
  await expect(page.getByRole('status')).toContainText(/Setup key copied|Copy is unavailable/);
  const password = 'Synthetic walkthrough password!';
  await page.getByLabel('New password', { exact: true }).fill(password);
  await page.getByLabel('Confirm password', { exact: true }).fill(password);
  const secondsRemaining = 30 - Math.floor(Date.now() / 1000) % 30;
  if (secondsRemaining < 3) await new Promise(resolve => setTimeout(resolve, secondsRemaining * 1000));
  const code = execFileSync(python, ['-c', 'import pyotp,sys; print(pyotp.TOTP(sys.argv[1]).now())', secret], { encoding: 'utf8' }).trim();
  await page.getByLabel('Authenticator code', { exact: true }).fill(code);
  await page.getByRole('button', { name: 'Create owner account', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Save your recovery codes' })).toBeFocused();
  const recovery = (await page.locator('pre').innerText()).split('\n');
  expect(recovery).toHaveLength(10);
  await expect(page.getByRole('button', { name: 'Open your workspace' })).toBeDisabled();
  await page.screenshot({ path: info.outputPath('03-recovery.png'), fullPage: true, mask: [page.locator('pre')] });
  await page.getByRole('button', { name: 'Copy recovery codes' }).click();
  await expect(page.getByRole('status')).toContainText(/Recovery codes copied|Copy is unavailable/);
  await page.getByLabel('I saved these codes securely.').check();
  await page.getByRole('button', { name: 'Open your workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Welcome to your workspace', exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('04-starting-guide.png'), fullPage: true });
  await page.getByRole('button', { name: 'Import a CSV', exact: true }).click();
  await page.getByRole('button', { name: 'Go to account settings', exact: true }).click();
  await expect(page.locator('#settings-accounts > summary')).toBeFocused();
  await page.getByRole('button', { name: 'Reporting in GBP · Change preferences', exact: true }).click();
  await page.getByLabel('Reporting currency', { exact: true }).fill('EUR');
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Preferences saved.' })).toBeVisible();
  await page.getByRole('button', { name: 'Add an account yourself', exact: false }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('EUR');
  await page.getByLabel('Account name', { exact: true }).fill('Everyday account');
  await page.getByLabel('Institution', { exact: true }).fill('Example Bank');
  await page.getByRole('combobox', { name: 'Account type', exact: true }).selectOption('current');
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Edit account Everyday account', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Record balance for Everyday account', exact: true }).click();
  await page.getByLabel('Balance (EUR)', { exact: true }).fill('1500');
  await page.getByRole('button', { name: 'Save balance', exact: true }).click();
  await expect(page.getByLabel('Balance (EUR)', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Import a CSV', exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption({ label: 'Everyday account · EUR' });
  const csv = Buffer.from('Date,Description,Amount,Reference\n2026-08-01,Groceries,-12.34,first\n2026-08-02,Salary,50.00,second\n');
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'first-statement.csv', mimeType: 'text/csv', buffer: csv });
  await page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true }).selectOption('Reference');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(page.getByRole('region', { name: 'CSV import preview', exact: true })).toContainText('2 new');
  await page.screenshot({ path: info.outputPath('05-csv-preview.png'), fullPage: true });
  await page.getByRole('button', { name: 'Confirm CSV import', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Import complete.' })).toBeVisible();
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'renamed-statement.csv', mimeType: 'text/csv', buffer: csv });
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(page.getByRole('region', { name: 'CSV import preview', exact: true })).toContainText('2 already imported');
  await page.getByRole('button', { name: 'Open workspace reports', exact: true }).click();
  const report = page.locator('#settings-reports');
  await expect(report.locator('div').filter({ has: page.getByText('Spending · EUR', { exact: true }) }).last()).toContainText('€12.34');
  await expect(report.locator('div').filter({ has: page.getByText('Income · EUR', { exact: true }) }).last()).toContainText('€50.00');
  await page.screenshot({ path: info.outputPath('06-report.png'), fullPage: true });
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  for (const value of [fresh.token, secret, password, ...recovery]) expect(stored).not.toContain(value);
  await page.getByRole('button', { name: 'I’m ready to explore', exact: true }).click();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Sign in to Am I Broke?', exact: true })).toBeVisible();
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByLabel('Authenticator or recovery code', { exact: true }).fill(recovery[0]);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Account & security', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Welcome to your workspace', exact: true })).toHaveCount(0);
  await page.getByText('Account settings', { exact: true }).click();
  await expect(page.locator('li').filter({ has: page.getByRole('button', { name: 'Edit account Everyday account', exact: true }) })).toContainText('2 transactions');
  if (info.project.use.isMobile) await page.getByRole('combobox', { name: 'Go to page', exact: true }).selectOption('net-worth');
  else await page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('button', { name: 'Net assets', exact: true }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('€1,500.00');
  await page.screenshot({ path: info.outputPath('07-net-assets.png'), fullPage: true });
  await page.getByRole('region', { name: 'Latest transactions', exact: true }).getByRole('button', { name: 'Open workspace reports', exact: true }).click();
  await expect(page.locator('#settings-reports > summary')).toBeFocused();
  await expect(page.getByText('Spending · EUR', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]); expect(unexpectedRequests).toEqual([]);
});

test('setup retries a network failure and refreshes an unavailable installation without a reload', async ({ page, fresh }) => {
  let state = 'failed';
  await page.route('**/api/setup/status', async route => {
    if (state === 'failed') return route.fulfill({ status: 503, json: { detail: 'Setup is temporarily unavailable. Try again.' } });
    if (state === 'unavailable') return route.fulfill({ json: { available: false, configured: false } });
    return route.continue();
  });
  await page.goto(fresh.url);
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  state = 'unavailable';
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(page.getByText('Browser setup isn’t ready yet.', { exact: false })).toBeVisible();
  state = 'ready';
  await page.getByRole('button', { name: 'Check again', exact: true }).click();
  await expect(page.getByLabel('Setup token', { exact: true })).toBeVisible();
});
