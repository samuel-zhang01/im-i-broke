import { expect, test } from '@playwright/test';

test('a first manual card leads from confirmed creation through a saved balance to Net assets', async ({ page, request }, info) => {
  const name = `First-use card ${info.project.name}`;
  await page.goto('/#settings');
  const accounts = page.locator('#settings-accounts');
  await accounts.locator(':scope > summary').click();
  await accounts.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await accounts.getByLabel('Account name', { exact: true }).fill(name);
  await accounts.getByLabel('Institution', { exact: true }).fill('Synthetic First Bank');
  await accounts.getByRole('combobox', { name: 'Account type', exact: true }).selectOption('credit');
  await accounts.getByLabel('Account currency', { exact: true }).fill('GBP');
  await accounts.getByRole('button', { name: 'Create manual account', exact: true }).click();
  await expect(accounts.getByRole('status').filter({ hasText: `${name} was added.` })).toBeVisible();
  await expect(accounts).toContainText('A CSV adds transactions; it does not set today’s balance.');
  await accounts.getByRole('button', { name: 'Record first balance', exact: true }).click();
  await expect(accounts.getByLabel('Balance (GBP)', { exact: true })).toBeEnabled();
  await accounts.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(accounts.getByRole('status').filter({ hasText: 'Balance saved for' })).toHaveCount(0);
  await accounts.getByRole('button', { name: 'Record first balance', exact: true }).click();
  await accounts.getByLabel('Balance (GBP)', { exact: true }).fill('-250');
  await accounts.getByRole('button', { name: 'Save balance', exact: true }).click();
  await expect(accounts.getByRole('status').filter({ hasText: `Balance saved for ${name}.` })).toBeVisible();
  const response = await request.get('/api/workspace/accounts');
  expect(response.ok()).toBe(true);
  const data = await response.json();
  expect(data.accounts.find((account: { display_name: string }) => account.display_name === name).manual_balance.amount).toBe(-250);
  await page.screenshot({ path: info.outputPath('saved-first-balance.png') });
  await accounts.getByRole('link', { name: 'View Net assets', exact: true }).click();
  await expect(page).toHaveURL(/#net-worth$/);
  await expect(page.getByLabel('Net assets value', { exact: true })).toBeVisible();
  if (info.project.use.isMobile || info.project.name.includes('narrow')) {
    await expect(page.getByRole('button', { name: `Open ${name}`, exact: true })).toContainText('-£250.00');
  } else {
    await expect(page.getByRole('row').filter({ has: page.getByRole('button', { name, exact: true }) })).toContainText('-£250.00');
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('manual card settings lead to the account balance controls without a provider connection', async ({ page, request }, info) => {
  const name = `Manual shortcut card ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers: { 'X-Ledger-Request': '1' }, data: {
    display_name: name, institution: 'Synthetic First Bank', account_type: 'credit', currency: 'GBP',
  } });
  expect(created.ok()).toBe(true);
  await page.goto('/#settings');
  const cards = page.locator('#settings-cards');
  await cards.locator(':scope > summary').click();
  await cards.getByRole('button', { name: 'Manage manual card balances', exact: true }).click();
  await expect(page.locator('#settings-accounts > summary')).toBeFocused();
  const accounts = page.locator('#settings-accounts');
  await accounts.getByRole('button', { name: `Record balance for ${name}`, exact: true }).click();
  await expect(accounts).toContainText('Enter the amount owed as a negative balance');
  await expect(accounts.getByLabel('Balance (GBP)', { exact: true })).toBeEnabled();
  await page.screenshot({ path: info.outputPath('manual-card-shortcut.png') });
});

test('a failed first balance stays retryable and never announces a saved result', async ({ page, request }, info) => {
  const name = `Retry first balance ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers: { 'X-Ledger-Request': '1' }, data: {
    display_name: name, institution: 'Synthetic First Bank', account_type: 'cash', currency: 'GBP',
  } });
  expect(created.ok()).toBe(true);
  let fail = true;
  await page.route('**/api/workspace/accounts/*/balances', route => fail && route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Synthetic balance save unavailable.' } }) : route.continue());
  await page.goto('/#settings');
  const accounts = page.locator('#settings-accounts');
  await accounts.locator(':scope > summary').click();
  await accounts.getByRole('button', { name: `Record balance for ${name}`, exact: true }).click();
  const field = accounts.getByLabel('Balance (GBP)', { exact: true });
  await field.fill('100');
  await accounts.getByRole('button', { name: 'Save balance', exact: true }).click();
  await expect(accounts.getByRole('alert')).toContainText('Synthetic balance save unavailable.');
  await expect(accounts.getByRole('status').filter({ hasText: 'Balance saved for' })).toHaveCount(0);
  await expect(field).toHaveValue('100');
  fail = false;
  await accounts.getByRole('button', { name: 'Save balance', exact: true }).click();
  await expect(accounts.getByRole('status').filter({ hasText: `Balance saved for ${name}.` })).toBeVisible();
});
