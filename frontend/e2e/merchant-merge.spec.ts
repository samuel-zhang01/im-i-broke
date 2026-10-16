import { revealMobileControls } from './navigation-helpers';
import { test, expect, type APIRequestContext } from '@playwright/test';

async function createName(request: APIRequestContext, name: string, alias: string) {
  const response = await request.post('/api/merchants', {
    headers: { 'X-Ledger-Request': '1' },
    data: { canonical_name: name, kind: 'merchant', category: 'Eating out', spending_treatment: 'normal',
      aliases: [{ pattern: alias, match_mode: 'exact', context: 'spending' }] },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id as number;
}

test('duplicate names merge only after a current preview and confirmation', async ({ page, request }, info) => {
  const name = `QA Garden Cafe ${info.project.name}`;
  const source = `QA Merge ${info.project.name}`;
  const first = await createName(request, name, `QA Garden Existing ${info.project.name}`);
  const second = await createName(request, name, source);
  const before = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(before).toHaveLength(2);
  await page.goto('/#merchants');
  await page.getByRole('button', { name: /^Saved merchants & aliases/ }).click();
  const search = page.getByRole('searchbox', { name: 'Search people and merchants', exact: true });
  await search.fill(`QA Gardne Cafe ${info.project.name}`);
  await expect(page.getByRole('button', { name: `Merge ${name}`, exact: true })).toHaveCount(2);
  await page.getByRole('combobox', { name: 'Search people and merchants matching mode' }).selectOption('regex');
  await search.fill('[');
  await expect(page.getByRole('alert')).toContainText('Invalid or unsupported');
  await search.fill(`^QA Garden Cafe ${info.project.name}$`);
  await expect(page.getByRole('button', { name: `Merge ${name}`, exact: true })).toHaveCount(2);
  await page.getByRole('button', { name: `Review merge ${name} into ${name}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Merge saved names' });
  await expect(dialog.getByRole('region', { name: 'Merge preview' })).toBeVisible();
  const save = dialog.getByRole('button', { name: 'Merge names', exact: true });
  const confirm = dialog.getByRole('checkbox', { name: 'I confirm these names belong to the same person or business', exact: true });
  await expect(save).toBeDisabled();
  await confirm.check();
  await expect(save).toBeEnabled();
  // A different destination needs a new preview and confirmation.
  const destinations = dialog.getByRole('radio', { name: `Keep ${name}`, exact: true });
  const unselected = (await destinations.nth(0).isChecked()) ? 1 : 0;
  await destinations.nth(unselected).check();
  await expect(confirm).not.toBeChecked();
  await expect(save).toBeDisabled();
  await confirm.check();
  await save.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: `Merge ${name}`, exact: true })).toHaveCount(1);
  const directory = (await (await request.get('/api/merchants')).json()).items;
  const retained = directory.filter((item: { id: number }) => [first, second].includes(item.id));
  expect(retained).toHaveLength(1);
  expect(retained[0].aliases).toHaveLength(2);
  const after = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(after).toHaveLength(2);
  for (const row of after) {
    const original = before.find((item: { id: number }) => item.id === row.id);
    for (const field of ['description', 'amount', 'account_id', 'txn_date']) expect(row[field]).toBe(original[field]);
  }
  // A manual merge starts with its candidate search open; regex can find an
  // alias even when the saved business name is different.
  const extra = `QA Annex ${info.project.name}`;
  await createName(request, extra, `QA Garden Additional ${info.project.name}`);
  await page.reload();
  await page.getByRole('searchbox', { name: 'Search people and merchants', exact: true }).fill(name);
  await page.getByRole('button', { name: `Merge ${name}`, exact: true }).click();
  const manual = page.getByRole('dialog', { name: 'Merge saved names' });
  await manual.getByRole('combobox', { name: 'Find saved names to merge matching mode' }).selectOption('regex');
  await manual.getByRole('searchbox', { name: 'Find saved names to merge', exact: true }).fill(`^QA Garden Additional ${info.project.name}$`);
  // Adding removes the candidate from this list and displays it among the
  // selected names, so it no longer exists as a checked candidate checkbox.
  await manual.getByRole('checkbox', { name: `Add ${extra} to merge`, exact: true }).click();
  await expect(manual.getByRole('radio', { name: `Keep ${extra}`, exact: true })).toBeVisible();
  await expect(manual.getByRole('region', { name: 'Merge preview' })).toBeVisible();
  await expect(manual.getByRole('button', { name: 'Merge names', exact: true })).toBeDisabled();
  await manual.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect((await (await request.get('/api/merchants')).json()).items.filter((item: { canonical_name: string }) => [name, extra].includes(item.canonical_name))).toHaveLength(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('the teaching picker supports regex and preserves a selected name on invalid search', async ({ page, request }, info) => {
  const name = `QA Regex Bistro ${info.project.name}`;
  const id = await createName(request, name, `QA Regex Alias ${info.project.name}`);
  const source = `QA Review ${info.project.name}`;
  await page.goto('/#merchants');
  await page.getByRole('button', { name: 'Classification inbox', exact: true }).click();
  await revealMobileControls(page, 'Inbox filters & sort');
  await page.getByLabel('Classification inbox filter', { exact: true }).selectOption('all');
  await page.getByLabel('Search classification inbox', { exact: true }).fill(source);
  await page.getByRole('button', { name: `Classify ${source}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: /^Search saved names/ }).click();
  await dialog.getByRole('combobox', { name: 'Find a saved name matching mode' }).selectOption('regex');
  const search = dialog.getByRole('searchbox', { name: 'Find a saved name', exact: true });
  await search.fill(`^QA Regex (Bistro|Alias) ${info.project.name}$`);
  const picker = dialog.getByRole('combobox', { name: 'Saved name', exact: true });
  await picker.selectOption(String(id));
  await search.fill('[');
  await expect(dialog.getByRole('alert')).toContainText('Invalid or unsupported');
  await expect(picker).toHaveValue(String(id));
  await search.fill(`^QA Regex Alias ${info.project.name}$`);
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await expect(picker).toHaveValue(String(id));
});
