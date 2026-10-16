import { expect, test } from '@playwright/test';

test('new manual accounts use the workspace currency and keep an explicit choice during refresh', async ({ page }, info) => {
  let preferenceReads = 0;
  await page.route('**/api/workspace/preferences', route => {
    preferenceReads++;
    return route.fulfill({ json: { reporting_currency: 'EUR', locale: 'de-DE', timezone: 'Europe/Berlin' } });
  });
  await page.goto('/#settings');
  await page.getByText('Account settings', { exact: true }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('EUR');
  const name = `Currency choice ${info.project.name}`;
  await page.getByLabel('Account name', { exact: true }).fill(name);
  await page.getByLabel('Account currency', { exact: true }).fill('USD');
  const beforeRefresh = preferenceReads;
  await page.evaluate(() => {
    const channel = new BroadcastChannel('ledger-data');
    channel.postMessage('changed');
    channel.close();
  });
  await expect.poll(() => preferenceReads).toBeGreaterThan(beforeRefresh);
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('USD');
  await expect(page.getByLabel('Account name', { exact: true })).toHaveValue(name);
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  const account = page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) });
  await expect(account).toContainText('USD');
  await account.getByRole('button', { name: `Edit account ${name}`, exact: true }).click();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('USD');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('EUR');
});

test('a missing currency default can be retried without silently creating a GBP account', async ({ page }) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let valid = false;
  await page.route('**/api/workspace/preferences', async route => {
    await pending;
    await route.fulfill({ json: valid
      ? { reporting_currency: 'CAD', locale: 'en-CA', timezone: 'America/Toronto' }
      : { locale: 'en-CA', timezone: 'America/Toronto' } });
  });
  await page.goto('/#settings');
  await page.getByText('Account settings', { exact: true }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await expect(page.getByRole('status', { name: 'Loading your account defaults…', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create manual account', exact: true })).toHaveCount(0);
  release();
  const error = page.getByRole('alert').filter({ hasText: 'Your workspace currency could not be loaded.' });
  await expect(error).toBeVisible();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveCount(0);
  valid = true;
  await error.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(page.getByLabel('Account currency', { exact: true })).toHaveValue('CAD');
});
