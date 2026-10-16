import { revealMobileControls } from './navigation-helpers';
import { test, expect } from '@playwright/test';

test('an untyped bank label can be taught as an own transfer after explicit confirmation', async ({ page, request }, info) => {
  const source = `QA Own Transfer ${info.project.name}`;
  const name = `QA Own Account ${info.project.name}`;
  const response = await request.post('/api/merchants', {
    headers: { 'X-Ledger-Request': '1' },
    data: { canonical_name: name, kind: 'self', category: 'Transfers & payments', spending_treatment: 'self_transfer',
      aliases: [{ pattern: `QA Existing Owned ${info.project.name}`, match_mode: 'exact', context: 'transfer' }] },
  });
  expect(response.ok()).toBe(true);
  const own = await response.json();
  const before = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(before).toHaveLength(2);

  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await page.goto('/#merchants');
  await expect(page.getByRole('heading', { level: 1, name: 'People & merchants' })).toBeVisible();
  await revealMobileControls(page, 'Inbox filters & sort');
  await page.getByLabel('Classification inbox filter', { exact: true }).selectOption('all');
  await page.getByLabel('Search classification inbox', { exact: true }).fill(source);
  await page.getByRole('button', { name: `Classify ${source}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  const savedName = dialog.getByRole('combobox', { name: 'Saved name', exact: true });
  await savedName.selectOption(String(own.id));
  const confirmation = dialog.getByRole('region', { name: 'Confirm own transfer', exact: true });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText(source);
  await expect(confirmation).toContainText('2 matching transactions');
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  const save = dialog.getByRole('button', { name: 'Save exact rule', exact: true });
  const checkbox = confirmation.getByRole('checkbox');
  await expect(save).toBeDisabled();
  await checkbox.check();
  await expect(save).toBeEnabled();

  // Changing identity cannot reuse a previous confirmation.
  await savedName.selectOption('');
  await expect(confirmation).toHaveCount(0);
  await savedName.selectOption(String(own.id));
  await expect(checkbox).not.toBeChecked();
  await expect(save).toBeDisabled();
  await checkbox.check();
  await expect(save).toBeEnabled();
  await save.click();
  await expect(dialog).toHaveCount(0);

  const after = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(after).toHaveLength(2);
  for (const row of after) {
    expect(row.merchant).toBe(name);
    expect(row.spending_treatment).toBe('self_transfer');
    const original = before.find((item: { id: number }) => item.id === row.id);
    for (const field of ['description', 'amount', 'account_id', 'txn_date']) expect(row[field]).toBe(original[field]);
  }
  const directory = (await (await request.get('/api/merchants')).json()).items;
  expect(directory.find((item: { id: number }) => item.id === own.id).aliases).toContainEqual(
    expect.objectContaining({ pattern: source, match_mode: 'exact', context: 'spending_bank' }),
  );
});
