import { expect, test } from '@playwright/test';

test('a manual card observation has a usable balance and accurate source labels', async ({ page, request }, info) => {
  const headers = { 'X-Ledger-Request': '1' };
  const name = `Manual card regression ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers, data: {
    display_name: name, institution: 'Synthetic Cards', account_type: 'credit', currency: 'GBP',
  } });
  expect(created.ok()).toBeTruthy();
  const account = await created.json();
  const saved = await request.post(`/api/workspace/accounts/${account.id}/balances`, { headers, data: {
    amount: -250, currency: 'GBP', as_of: new Date().toISOString().slice(0, 10),
  } });
  expect(saved.ok()).toBeTruthy();
  const actual = await request.get('/api/banking');
  expect(actual.ok()).toBeTruthy();
  const data = await actual.json();
  const local = data.local_accounts.find((item: { id: string }) => item.id === account.id);
  expect(local.balance).toBe(-250);
  expect(local.valuation.current_unknown).toBe(false);
  expect(local.valuation.reported_source).toBe('manual');
  expect(data.balance_checks.some((item: { account_id: string }) => item.account_id === account.id)).toBe(false);
  await page.goto('/#banking');
  const button = page.getByRole('button', { name: `Open ${name}, GBP`, exact: true });
  await expect(button).toContainText('-£250.00');
  await expect(button).toContainText('Manual observation');
  await expect(button).toContainText('Manual balance');
  await expect(button).not.toContainText('Unknown');
  await button.click();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(page.getByText('Observation dated', { exact: false })).toBeVisible();
  const source = page.getByLabel('Connection & history', { exact: true });
  await expect(source).toContainText('Manual balance observation');
  await expect(source).toContainText('Manually tracked');
  await expect(source).not.toContainText('Imported statements');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('manual-card-observation.png') });
});
