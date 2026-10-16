import { expect, test } from '@playwright/test';
import { revealMobileControls } from './navigation-helpers';

// No route mocks: this protects the preferences → wealth reader boundary.
test('an absent native reporting currency stays readable across cold loads and can be changed locally', async ({ page, request }, info) => {
  const headers = { 'X-Ledger-Request': '1' };
  const original = await (await request.get('/api/workspace/preferences')).json();
  const created = await request.post('/api/workspace/accounts', { headers, data: {
    display_name: `Currency boundary ${info.project.name}`, currency: 'GBP',
  } });
  expect(created.ok()).toBeTruthy();
  const account = await created.json();
  const balance = await request.post(`/api/workspace/accounts/${account.id}/balances`, { headers, data: {
    amount: 100, currency: 'GBP', as_of: new Date().toISOString().slice(0, 10),
  } });
  expect(balance.ok()).toBeTruthy();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    const saved = await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'JPY' } });
    expect(saved.ok()).toBeTruthy();
    const actual = await request.get('/api/wealth?currency=JPY&range=1m');
    expect(actual.ok()).toBeTruthy();
    const data = await actual.json();
    expect(data.currency).toBe('JPY');
    expect(data.summary.missing_accounts).toBeGreaterThan(0);
    expect(data.accounts.find((item: { id: string }) => item.id === `statement:${account.id}`).native_value).toBe(100);
    const currency = page.getByRole('combobox', { name: 'Reporting currency', exact: true });
    for (let load = 0; load < 2; load++) {
      if (load === 0) await page.goto('/#net-worth');
      else await page.reload();
      await revealMobileControls(page, 'Accounts & currency');
      await expect(currency).toHaveValue('JPY');
      await expect(page.getByLabel('Net assets value', { exact: true })).toBeVisible();
      await expect(page.getByText('Net assets could not be loaded.', { exact: false })).toHaveCount(0);
    }
    await currency.selectOption('GBP');
    await expect(page.getByLabel('Net assets value', { exact: true })).toContainText('£');
    await expect(currency).toHaveValue('GBP');
    expect(errors).toEqual([]);
    await page.screenshot({ path: info.outputPath('reporting-default-recovery.png') });
  } finally {
    const restored = await request.patch('/api/workspace/preferences', { headers, data: original });
    expect(restored.ok()).toBeTruthy();
  }
});
