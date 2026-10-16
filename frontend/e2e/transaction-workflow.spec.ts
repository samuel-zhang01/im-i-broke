import { revealMobileControls } from './navigation-helpers';
import { test, expect, type Page } from '@playwright/test';

async function openReview(page: Page, name: string) {
  await page.goto('/#transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill(name);
  await page.getByRole('button', { name: `Review ${name}`, exact: true }).click();
  return page.getByRole('dialog');
}

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('visible sorting orders the full result set, resets pagination and survives clearing filters', async ({ page }) => {
  await page.goto('/#transactions');
  const sort = page.getByLabel('Sort transactions', { exact: true });
  await expect(sort).toHaveValue('date:desc');
  await expect(page.getByLabel('From date', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Next →', exact: true }).click();
  await expect(page.getByRole('button', { name: '← Prev', exact: true })).toBeEnabled();
  const sorted = page.waitForResponse(r => r.url().includes('/api/transactions?') && new URL(r.url()).searchParams.get('sort') === 'amount');
  await sort.selectOption('amount:asc');
  const response = await sorted;
  const data = await response.json();
  expect(data.offset).toBe(0);
  expect(data.items[0].amount).toBe(-600);
  expect(data.items.every((row: any, i: number, rows: any[]) => i === 0 || rows[i - 1].amount <= row.amount)).toBe(true);
  await expect(page.getByRole('button', { name: /^Review QA/ }).first()).toHaveAccessibleName('Review QA Equipment');
  await expect(page.getByRole('button', { name: '← Prev', exact: true })).toBeDisabled();
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Category', { exact: true }).selectOption('Uncategorized');
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(sort).toHaveValue('amount:asc');
  await revealMobileControls(page, 'Transaction filters');
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByRole('button', { name: /^Date & amount/ }).click();
  await page.getByLabel('Max amount (£)', { exact: true }).fill('-20');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByRole('button', { name: /^Date & amount/ }).click();
  await expect(page.getByRole('button', { name: 'Remove maximum amount filter' })).toBeVisible();
  await page.getByRole('button', { name: 'Remove maximum amount filter' }).click();
  await expect(page.getByRole('button', { name: 'Remove maximum amount filter' })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('saving an exact rule closes both dialogs, retains filters and refreshes the matching list', async ({ page, request }, info) => {
  const name = `QA Unclassified ${info.project.name.includes('desktop') ? '14' : '15'}`;
  const before = (await (await request.get('/api/transactions', { params: { q: name } })).json()).items[0];
  await page.goto('/#transactions');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Category', { exact: true }).selectOption('Uncategorized');
  await page.getByLabel('Sort transactions', { exact: true }).selectOption('date:asc');
  await page.getByLabel('Search transactions', { exact: true }).fill(name);
  await page.getByRole('button', { name: `Review ${name}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Spending category', exact: true }).selectOption('Shopping');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await expect(dialog.getByRole('combobox', { name: 'Category', exact: true })).toHaveValue('Shopping');
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('No matching transactions.', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Transaction filters');
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('Uncategorized');
  await expect(page.getByLabel('Sort transactions', { exact: true })).toHaveValue('date:asc');
  const after = (await (await request.get(`/api/transactions/${before.id}/review`)).json()).transaction;
  expect(after.category).toBe('Shopping');
  for (const field of ['description', 'amount', 'txn_date', 'account_id']) expect(after[field]).toBe(before[field]);
});

test('unsaved row details are protected and cancelling a rule keeps the review draft', async ({ page }) => {
  const dialog = await openReview(page, 'QA Unclassified 16');
  await dialog.getByRole('button', { name: 'Notes & exclusions', exact: true }).click();
  await dialog.getByRole('textbox', { name: 'Review note', exact: true }).fill('Keep my unsaved note');
  await dialog.getByRole('combobox', { name: 'Spending category', exact: true }).selectOption('Shopping');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await expect(dialog.getByText('1 matching transactions', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeDisabled();
  await dialog.getByRole('checkbox', { name: /Continue without my unsaved note/ }).check();
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toHaveAccessibleName('Review transaction');
  await expect(dialog.getByRole('textbox', { name: 'Review note', exact: true })).toHaveValue('Keep my unsaved note');
  await expect(dialog.getByRole('combobox', { name: 'Spending category', exact: true })).toHaveValue('Shopping');
});

test('a failed exact-rule save stays open with an actionable error', async ({ page }) => {
  const dialog = await openReview(page, 'QA Unclassified 17');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await page.route('**/api/merchants/classify', route => route.fulfill({status:503,json:{detail:'Synthetic rule save unavailable'}}));
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Synthetic rule save unavailable');
  await expect(dialog).toHaveAccessibleName('Teach Am I Broke? this name');
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeDisabled();
});

test('an existing uncategorised name can be categorised without creating a duplicate', async ({ page, request }, info) => {
  const name = `QA Unclassified ${info.project.name.includes('desktop') ? '20' : '21'}`;
  const transaction = (await (await request.get('/api/transactions', { params: { q: name } })).json()).items[0];
  const draft = { example_id: transaction.id, canonical_name: `Saved bread and truffle ${info.project.name}`, category: 'Uncategorized', kind: 'merchant', spending_treatment: 'normal' };
  const preview = await (await request.post('/api/merchants/classify/preview', { data: draft })).json();
  const savedResponse = await request.post('/api/merchants/classify', { data: { ...draft, preview_token: preview.preview_token } });
  expect(savedResponse.ok()).toBeTruthy();
  const saved = await savedResponse.json();
  await page.goto('/#transactions');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Category', { exact: true }).selectOption('Uncategorized');
  await page.getByLabel('Search transactions', { exact: true }).fill(draft.canonical_name);
  await page.getByRole('button', { name: `Review ${draft.canonical_name}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await expect(dialog.getByText(/This saved name has no spending category/)).toBeVisible();
  await dialog.getByRole('checkbox', { name: 'Change the saved category for this name' }).check();
  await dialog.getByRole('combobox', { name: 'Category', exact: true }).selectOption('Eating out');
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: `Review ${draft.canonical_name}`, exact: true })).toHaveCount(0);
  const after = (await (await request.get('/api/transactions', { params: { q: draft.canonical_name } })).json()).items[0];
  expect(after.category).toBe('Eating out');
  expect(after.classification.merchant_id).toBe(saved.merchant_id);
});

test('a category chosen in transaction review carries into the existing saved rule', async ({ page, request }, info) => {
  const name = `QA Unclassified ${info.project.name.includes('desktop') ? '22' : '23'}`;
  const transaction = (await (await request.get('/api/transactions', { params: { q: name } })).json()).items[0];
  const draft = { example_id: transaction.id, canonical_name: `Saved category draft ${info.project.name}`, category: 'Uncategorized', kind: 'merchant', spending_treatment: 'normal' };
  const preview = await (await request.post('/api/merchants/classify/preview', { data: draft })).json();
  const savedResponse = await request.post('/api/merchants/classify', { data: { ...draft, preview_token: preview.preview_token } });
  expect(savedResponse.ok()).toBeTruthy();
  const saved = await savedResponse.json();
  await page.goto('/#transactions');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Category', { exact: true }).selectOption('Uncategorized');
  await page.getByLabel('Search transactions', { exact: true }).fill(draft.canonical_name);
  await page.getByRole('button', { name: `Review ${draft.canonical_name}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Spending category', exact: true }).selectOption('Eating out');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await expect(dialog.getByRole('checkbox', { name: 'Change the saved category for this name' })).toBeChecked();
  await expect(dialog.getByRole('combobox', { name: 'Category', exact: true })).toHaveValue('Eating out');
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: `Review ${draft.canonical_name}`, exact: true })).toHaveCount(0);
  const after = (await (await request.get('/api/transactions', { params: { q: draft.canonical_name } })).json()).items[0];
  expect(after.category).toBe('Eating out');
  expect(after.classification.merchant_id).toBe(saved.merchant_id);
});

test('an untyped debit-account payment can be taught as a friend with the chosen category', async ({ page, request }, info) => {
  const project = info.project.name.includes('desktop') ? 'desktop' : 'mobile';
  const source = `QA Friend ${project} Shared dinner`;
  const name = `Alex Morgan ${project}`;
  const before = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(before).toHaveLength(2);
  await page.goto('/#transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill(source);
  await page.getByRole('button', { name: `Review ${source}`, exact: true }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  await dialog.getByRole('combobox', { name: 'Who is this?', exact: true }).selectOption('person');
  await dialog.getByRole('textbox', { name: 'Display name', exact: true }).fill(name);
  await expect(dialog.getByRole('combobox', { name: 'Category', exact: true })).toHaveValue('Friends & shared costs');
  await dialog.getByRole('combobox', { name: 'Category', exact: true }).selectOption('Transfers & payments');
  await expect(dialog.getByRole('combobox', { name: 'How should payments count?', exact: true })).toHaveValue('normal');
  await expect(dialog.getByText('2 matching transactions', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('alert')).toHaveCount(0);
  await expect(dialog.getByRole('region', { name: 'Confirm own transfer' })).toHaveCount(0);
  await expect(dialog.getByRole('button', { name: 'Save exact rule', exact: true })).toBeEnabled();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog).toHaveCount(0);

  const after = (await (await request.get('/api/transactions', { params: { q: name } })).json()).items;
  expect(after).toHaveLength(2);
  for (const row of after) {
    expect(row.merchant_kind).toBe('person');
    expect(row.category).toBe('Transfers & payments');
    expect(row.spending_treatment).toBe('normal');
    const original = before.find((item: { id: number }) => item.id === row.id);
    for (const field of ['description', 'amount', 'account_id', 'txn_date']) expect(row[field]).toBe(original[field]);
  }
  const directory = (await (await request.get('/api/merchants')).json()).items;
  expect(directory.find((item: { canonical_name: string }) => item.canonical_name === name).aliases).toContainEqual(
    expect.objectContaining({ pattern: source, match_mode: 'exact', context: 'spending_bank' }),
  );
});
