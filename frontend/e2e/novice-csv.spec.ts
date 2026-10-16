import { expect, test, type APIRequestContext } from '@playwright/test';

const headers = { 'X-Ledger-Request': '1' };
async function account(request: APIRequestContext, name: string) {
  const response = await request.post('/api/workspace/accounts', { headers, data: { display_name: name, currency: 'EUR' } });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

test('a first CSV explains international formats, previews signs and reports exact duplicate results', async ({ page, request }, info) => {
  const narrow = (info.project.use.viewport?.width || 1440) < 640;
  if (narrow) await page.setViewportSize({ width: 320, height: 844 });
  const name = `Novice CSV ${info.project.name}`;
  const id = await account(request, name);
  const year = narrow ? '1988' : '1987';
  const csv = Buffer.from(`Date;Description;Amount;Reference\n21/09/${year};Novice coffee;12,34;novice-coffee\n22/09/${year};Novice salary;-50,00;novice-salary\n`);
  await page.goto('/#settings');
  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption(id);
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'bank-export.csv', mimeType: 'text/csv', buffer: csv });
  await expect(page.getByText('Only one column heading was found.', { exact: false })).toBeVisible();
  await page.getByRole('combobox', { name: 'Separator', exact: true }).selectOption(';');
  await expect(page.getByRole('combobox', { name: 'Date column', exact: true })).toHaveValue('Date');
  await expect(page.getByRole('combobox', { name: 'Description column', exact: true })).toHaveValue('Description');
  await expect(page.getByRole('combobox', { name: 'Amount column', exact: true })).toHaveValue('Amount');
  await expect(page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true })).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Saved column mapping', exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  const preview = page.getByRole('region', { name: 'CSV import preview', exact: true });
  await expect(preview.getByRole('alert')).toContainText('does not match the selected date format. Choose the format used in your file, then preview again.');
  await expect(preview.getByRole('button', { name: 'Confirm CSV import', exact: true })).toBeDisabled();
  await page.getByRole('combobox', { name: 'Date format', exact: true }).selectOption('%d/%m/%Y');
  await page.getByRole('combobox', { name: 'Decimal separator', exact: true }).selectOption(',');
  await page.getByRole('combobox', { name: 'Amount direction', exact: true }).selectOption('-1');
  await page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true }).selectOption('Reference');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(preview).toContainText('2 rows · 2 new · 0 already imported · 0 need review');
  const coffee = preview.getByRole('row').filter({ hasText: 'Novice coffee' });
  await expect(coffee).toContainText(`${year}-09-21`);
  await expect(coffee).toContainText('-12.34 EUR');
  await expect(coffee).toContainText('Ready to import');
  await expect(preview.getByRole('row').filter({ hasText: 'Novice salary' })).toContainText('50 EUR');
  if ((page.viewportSize()?.width || 0) < 640) {
    const amount = coffee.getByRole('cell').filter({ hasText: '-12.34 EUR' });
    const [cellBounds, previewBounds] = await Promise.all([amount.boundingBox(), preview.boundingBox()]);
    expect(cellBounds).not.toBeNull(); expect(previewBounds).not.toBeNull();
    expect(cellBounds!.x).toBeGreaterThanOrEqual(previewBounds!.x);
    expect(cellBounds!.x + cellBounds!.width).toBeLessThanOrEqual(previewBounds!.x + previewBounds!.width);
  }
  await preview.getByRole('button', { name: 'Confirm CSV import', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Import complete.' })).toHaveText(`Import complete. 2 transactions added to ${name}.`);
  const preferences = await (await request.get('/api/workspace/preferences')).json();
  try {
    expect((await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'EUR' } })).ok()).toBeTruthy();
    await page.getByRole('button', { name: 'View imported income and spending', exact: true }).click();
    await expect(page.locator('#settings-reports > summary')).toBeFocused();
    await page.getByLabel('From', { exact: true }).fill(`${year}-09-21`);
    await page.getByLabel('To', { exact: true }).fill(`${year}-09-22`);
    await expect(page.getByText('Spending · EUR', { exact: true }).locator('..')).toContainText('€12.34');
    await expect(page.getByText('Income · EUR', { exact: true }).locator('..')).toContainText('€50.00');
  } finally {
    expect((await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: preferences.reporting_currency } })).ok()).toBeTruthy();
  }
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'bank-export-copy.csv', mimeType: 'text/csv', buffer: csv });
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(preview).toContainText('2 rows · 0 new · 2 already imported · 0 need review');
  await expect(preview.getByRole('row').filter({ hasText: 'Novice coffee' })).toContainText('Already imported');
  await preview.getByRole('button', { name: 'Confirm CSV import', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'No transactions added.' })).toHaveText('No transactions added. 2 transactions were already imported into this account.');
  await page.locator('#settings-reports > summary').click();
  await expect(page.locator('#settings-reports')).toHaveJSProperty('open', false);
  await page.getByRole('button', { name: 'View imported income and spending', exact: true }).click();
  await expect(page.locator('#settings-reports')).toHaveJSProperty('open', true);
  await expect(page.locator('#settings-reports > summary')).toBeFocused();
  const accounts = await (await request.get('/api/workspace/accounts')).json();
  expect(accounts.accounts.find((item: { id: string }) => item.id === id).transaction_count).toBe(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('replacement files retain valid manual mappings and explain unrecognized headings', async ({ page, request }, info) => {
  const id = await account(request, `CSV mappings ${info.project.name}`);
  await page.goto('/#settings');
  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption(id);
  const file = (name: string, csv: string) => ({ name, mimeType: 'text/csv', buffer: Buffer.from(csv) });
  await page.getByLabel('CSV file', { exact: true }).setInputFiles(file('with-reference.csv', 'Booking date,Date,Details,Value,Reference,Currency\n2026-09-20,2026-09-21,First sample,-1,mapping-1,EUR\n'));
  await page.getByRole('combobox', { name: 'Date column', exact: true }).selectOption('Date');
  await page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true }).selectOption('Reference');
  await page.getByText('Saved mappings and other options', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Currency column (optional)', exact: true }).selectOption('Currency');
  await page.getByLabel('CSV file', { exact: true }).setInputFiles(file('without-reference.csv', 'Booking date,Date,Details,Value\n2026-09-20,2026-09-21,Replacement sample,-2\n'));
  await expect(page.getByRole('combobox', { name: 'Date column', exact: true })).toHaveValue('Date');
  await expect(page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true })).toHaveValue('');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  const preview = page.getByRole('region', { name: 'CSV import preview', exact: true });
  await expect(preview).toContainText('1 new');
  await expect(preview).toContainText('2026-09-21');
  await expect(preview.getByRole('alert')).toHaveCount(0);
  await page.getByLabel('CSV file', { exact: true }).setInputFiles(file('different-headings.csv', 'When,Payee,Total\n2026-09-22,Custom bank wording,-3\n'));
  await expect(preview).toHaveCount(0);
  await expect(page.getByText('Choose the columns your bank uses for dates, descriptions and amounts.', { exact: false })).toBeVisible();
  await page.getByRole('combobox', { name: 'Date column', exact: true }).selectOption('When');
  await page.getByRole('combobox', { name: 'Description column', exact: true }).selectOption('Payee');
  await page.getByRole('combobox', { name: 'Amount column', exact: true }).selectOption('Total');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(preview).toContainText('Custom bank wording');
  await expect(preview).toContainText('-3 EUR');
  await expect(preview.getByRole('button', { name: 'Confirm CSV import', exact: true })).toBeEnabled();
});

test('a blank import source opens optional settings and focuses the field before preview', async ({ page, request }, info) => {
  const id = await account(request, `CSV source validation ${info.project.name}`);
  const consoleErrors: string[] = [];
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  let requests = 0;
  page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith('/api/workspace/imports/preview')) requests++; });
  await page.goto('/#settings');
  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption(id);
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'source-validation.csv', mimeType: 'text/csv', buffer: Buffer.from('Date,Description,Amount\n2026-09-21,Source validation,-12.34\n') });
  const options = page.getByText('Saved mappings and other options', { exact: true });
  await options.click();
  await page.getByLabel('Import source', { exact: true }).fill('');
  await options.click();
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(page.getByLabel('Import source', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Import source', { exact: true })).toBeFocused();
  const sourceMessage = 'Enter an import source name, such as CSV import. Reuse this name for future exports from this account.';
  await expect(page.getByRole('alert').filter({ hasText: sourceMessage })).toBeVisible();
  await expect(page.getByLabel('Import source', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByLabel('Import source', { exact: true })).toHaveAccessibleDescription(sourceMessage);
  expect(requests).toBe(0);
  expect(consoleErrors.filter(message => message.includes('not focusable'))).toEqual([]);
  await page.getByRole('combobox', { name: 'Date column', exact: true }).selectOption('');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Date column', exact: true })).toBeFocused();
  expect(requests).toBe(0);
  await page.getByRole('combobox', { name: 'Date column', exact: true }).selectOption('Date');
  await page.getByLabel('Import source', { exact: true }).fill('Novice source');
  await expect(page.getByRole('alert').filter({ hasText: sourceMessage })).toHaveCount(0);
  await expect(page.getByLabel('Import source', { exact: true })).not.toHaveAttribute('aria-invalid', 'true');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(page.getByRole('region', { name: 'CSV import preview', exact: true })).toContainText('1 new');
  expect(requests).toBe(1);
  expect(consoleErrors.filter(message => message.includes('not focusable'))).toEqual([]);
});
