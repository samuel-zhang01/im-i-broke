import { expect, test, type APIRequestContext } from '@playwright/test';

const headers = { 'X-Ledger-Request': '1' };
const mapping = { date_column: 'Date', description_column: 'Description', amount_column: 'Amount', currency: 'EUR' };
async function createAccount(request: APIRequestContext, name: string) {
  const response = await request.post('/api/workspace/accounts', { headers, data: { display_name: name, currency: 'EUR' } });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}
async function importRows(request: APIRequestContext, account_id: string, rows: string[]) {
  const input = { account_id, mapping, csv_text: `Date,Description,Amount\n${rows.join('\n')}\n`, source_name: 'Synthetic evidence' };
  const response = await request.post('/api/workspace/imports/preview', { headers, data: input });
  expect(response.ok()).toBeTruthy();
  const preview = await response.json();
  const committed = await request.post('/api/workspace/imports/commit', { headers, data: { ...input, preview_token: preview.preview_token } });
  expect(committed.ok()).toBeTruthy();
}

test('all same-day report evidence remains reachable and reviewable through the real API', async ({ page, request }, info) => {
  const day = info.project.name === 'desktop' ? '1981-01-10' : '1982-01-10';
  const account = await createAccount(request, `Paged evidence ${info.project.name}`);
  await importRows(request, account, Array.from({ length: 205 }, (_, i) => `${day},Synthetic evidence ${i},-1`));
  expect((await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'GBP' } })).ok()).toBeTruthy();
  const reportUrl = `/api/workspace/reports/cashflow?currency=GBP&account=${account}&date_from=${day}&date_to=${day}`;
  const firstPage = await (await request.get(reportUrl)).json();
  const response = await request.get(`${reportUrl}&offset=200&revision=${firstPage.report_revision}`);
  const lastPage = await response.json();
  expect(lastPage.events).toHaveLength(5);
  const target = lastPage.events[0];
  await page.goto('/#settings');
  await page.getByText('Workspace reports', { exact: true }).click();
  await page.getByLabel('From', { exact: true }).fill(day);
  await page.getByLabel('To', { exact: true }).fill(day);
  await page.getByText('Amounts and conversion evidence (205 transactions)', { exact: true }).click();
  const pages = page.getByRole('navigation', { name: 'Report evidence pages', exact: true });
  await expect(pages).toContainText('Showing 1–200 of 205 transactions');
  await expect(pages.getByRole('button', { name: 'Previous evidence', exact: true })).toBeDisabled();
  await pages.getByRole('button', { name: 'Next evidence', exact: true }).click();
  await expect(pages).toContainText('Showing 201–205 of 205 transactions');
  await expect(pages.getByRole('button', { name: 'Next evidence', exact: true })).toBeDisabled();
  const row = page.getByRole('row').filter({ has: page.getByRole('cell', { name: `${target.description} ${target.category}`, exact: true }) });
  await expect(row).toContainText('Missing dated exchange rate');
  await row.getByRole('button', { name: 'Review', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('€1.00');
  await dialog.getByRole('button', { name: 'Notes & exclusions', exact: true }).click();
  await dialog.getByRole('checkbox', { name: /Exclude this transaction from spending analytics/ }).check();
  await dialog.getByRole('button', { name: /Save review/ }).click();
  await expect(dialog).toHaveCount(0);
  await expect(pages).toContainText('Showing 1–200 of 205 transactions');
  await pages.getByRole('button', { name: 'Next evidence', exact: true }).click();
  await expect(pages).toContainText('Showing 201–205 of 205 transactions');
  await expect(row).toContainText('User exclusions');
  await pages.getByRole('button', { name: 'Previous evidence', exact: true }).click();
  await expect(pages).toContainText('Showing 1–200 of 205 transactions');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('report evidence restarts explicitly when another writer changes the page membership', async ({ page, request }, info) => {
  const day = info.project.name === 'desktop' ? '1985-01-10' : '1986-01-10';
  const account = await createAccount(request, `Changing evidence ${info.project.name}`);
  await importRows(request, account, Array.from({ length: 205 }, (_, i) => `${day},Original synthetic evidence ${i},-1`));
  await page.goto('/#settings');
  await page.getByText('Workspace reports', { exact: true }).click();
  await page.getByLabel('From', { exact: true }).fill(day);
  await page.getByLabel('To', { exact: true }).fill(day);
  await page.getByText('Amounts and conversion evidence (205 transactions)', { exact: true }).click();
  const pages = page.getByRole('navigation', { name: 'Report evidence pages', exact: true });
  await expect(pages).toContainText('Showing 1–200 of 205 transactions');
  // API writes emulate a second device/provider update without a local UI event.
  await importRows(request, account, [`${day},New synthetic evidence,-2`]);
  await pages.getByRole('button', { name: 'Next evidence', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Report evidence changed');
  await expect(pages.getByRole('button', { name: 'Next evidence', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Return to first evidence page', exact: true }).click();
  await expect(pages).toContainText('Showing 1–200 of 206 transactions');
  await expect(page.getByRole('alert')).toHaveCount(0);
  await pages.getByRole('button', { name: 'Next evidence', exact: true }).click();
  await expect(pages).toContainText('Showing 201–206 of 206 transactions');
});

test('CSV overlap beyond the sample boundary is shown before confirmation', async ({ page, request }, info) => {
  const account = await createAccount(request, `CSV last-row review ${info.project.name}`);
  await importRows(request, account, ['1983-01-10,Synthetic earlier evidence,-201']);
  await page.goto('/#settings');
  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption(account);
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'synthetic-large.csv', mimeType: 'text/csv', buffer: Buffer.from('Date,Description,Amount\n' + Array.from({ length: 201 }, (_, i) => `1983-01-10,Synthetic upload ${i + 1},-${i + 1}`).join('\n')) });
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  const preview = page.getByRole('region', { name: 'CSV import preview', exact: true });
  await expect(preview).toContainText('201 rows · 200 new · 0 already imported · 1 need review');
  const warning = preview.getByRole('row').filter({ hasText: 'Synthetic upload 201' });
  await expect(warning).toContainText('202');
  await expect(warning).toContainText('Existing transaction on the same date with the same amount');
  await expect(preview.getByRole('button', { name: 'Confirm CSV import', exact: true })).toBeDisabled();
  await preview.getByRole('button', { name: 'File sample (200)', exact: true }).click();
  await preview.getByRole('button', { name: 'Next CSV rows', exact: true }).click();
  await expect(preview.getByRole('navigation', { name: 'CSV evidence pages' })).toContainText('Showing 101–200 of 200 sample rows');
  await preview.getByRole('button', { name: 'Possible overlaps (1)', exact: true }).click();
  await expect(warning).toBeVisible();
  await preview.getByRole('checkbox', { name: /I reviewed the possible overlaps/ }).check();
  await preview.getByRole('button', { name: 'Confirm CSV import', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Import complete.' })).toBeVisible();
  const accounts = await (await request.get('/api/workspace/accounts')).json();
  expect(accounts.accounts.find((item: { id: string }) => item.id === account).transaction_count).toBe(202);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('every page of a fully overlapping CSV is accessible', async ({ page, request }, info) => {
  const account = await createAccount(request, `CSV many overlaps ${info.project.name}`);
  await importRows(request, account, ['1984-01-10,Synthetic earlier evidence,-7']);
  await page.goto('/#settings');
  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption(account);
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'synthetic-overlaps.csv', mimeType: 'text/csv', buffer: Buffer.from('Date,Description,Amount\n' + Array.from({ length: 205 }, (_, i) => `1984-01-10,Synthetic overlap ${i + 1},-7`).join('\n')) });
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  const preview = page.getByRole('region', { name: 'CSV import preview', exact: true });
  const pages = preview.getByRole('navigation', { name: 'CSV evidence pages', exact: true });
  await expect(pages).toContainText('Showing 1–100 of 205 possible overlaps');
  await pages.getByRole('button', { name: 'Next CSV rows', exact: true }).click();
  await expect(pages).toContainText('Showing 101–200 of 205 possible overlaps');
  await pages.getByRole('button', { name: 'Next CSV rows', exact: true }).click();
  await expect(pages).toContainText('Showing 201–205 of 205 possible overlaps');
  await expect(preview.getByRole('row').filter({ hasText: 'Synthetic overlap 205' })).toContainText('Existing transaction');
  await expect(pages.getByRole('button', { name: 'Next CSV rows', exact: true })).toBeDisabled();
  await expect(preview.getByRole('button', { name: 'Confirm CSV import', exact: true })).toBeDisabled();
});
