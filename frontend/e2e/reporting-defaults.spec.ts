import { expect, test } from '@playwright/test';
import { wealth } from './fixtures/wealth';
import { revealMobileControls } from './navigation-helpers';

async function navigate(page: import('@playwright/test').Page, destination: string) {
  await page.evaluate(page => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page } })), destination);
}

test('an invalid saved locale leaves reports readable and cannot be saved again', async ({ page }) => {
  const errors: string[] = [];
  let writes = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/workspace/preferences', route => {
    if (route.request().method() === 'PATCH') writes++;
    return route.fulfill({ json: { reporting_currency: 'GBP', locale: 'en-abc', timezone: 'Europe/London' } });
  });
  await page.route('**/api/workspace/reports/cashflow?**', route => route.fulfill({ json: {
    currency: 'GBP', totals: { income: 0, spending: 0, net: 0 }, monthly: [], categories: [], coverage: {}, method: 'Synthetic empty report',
    partial: false, missing_fx_count: 0, possible_overlap_count: 0, stale_review_count: 0, source_review_count: 0, event_count: 0, events: [],
  } }));
  await page.goto('/#settings');
  await page.getByText('Workspace preferences', { exact: true }).click();
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Enter a valid locale');
  expect(writes).toBe(0);
  await page.getByText('Workspace reports', { exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'The saved locale could not be used.' })).toBeVisible();
  await expect(page.getByText('£0.00', { exact: true }).first()).toBeVisible();
  expect(errors).toEqual([]);
});

test('incomplete workspace preferences preserve the existing GBP currency default', async ({ page }) => {
  await page.route('**/api/workspace/preferences', route => route.fulfill({ json: { revision: 'incomplete-response' } }));
  await page.route('**/api/wealth?**', route => route.fulfill({ json: { ...wealth, currency: new URL(route.request().url()).searchParams.get('currency'), currencies: ['GBP'] } }));
  await page.route('**/api/investments/connected', route => route.fulfill({ json: { configured: false, retrieved_at: '2026-09-22T10:00:00Z', error: null, accounts: [], connections: { status: 'empty', data: [], error: null, truncated: false } } }));
  await page.goto('/#net-worth');
  await revealMobileControls(page, 'Accounts & currency');
  await expect(page.getByRole('combobox', { name: 'Reporting currency', exact: true })).toHaveValue('GBP');
  await navigate(page, 'connected-portfolio');
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByRole('combobox', { name: 'Investment reporting currency', exact: true })).toHaveValue('GBP');
  expect(await page.evaluate(() => document.body.innerText)).not.toContain('Unknown currency');
});

test('workspace reporting defaults remain selectable and respect currency choices already made in each view', async ({ page }) => {
  let defaultCurrency = 'JPY';
  await page.route('**/api/workspace/preferences', route => route.fulfill({ json: { reporting_currency: defaultCurrency, locale: 'en-GB', timezone: 'Europe/London' } }));
  await page.route('**/api/wealth?**', route => route.fulfill({ json: { ...wealth, currency: new URL(route.request().url()).searchParams.get('currency'), currencies: ['GBP'] } }));
  await page.route('**/api/investments/connected', route => route.fulfill({ json: { configured: false, retrieved_at: '2026-09-22T10:00:00Z', error: null, accounts: [], connections: { status: 'empty', data: [], error: null, truncated: false } } }));
  await page.goto('/#net-worth');
  await revealMobileControls(page, 'Accounts & currency');
  const netCurrency = page.getByRole('combobox', { name: 'Reporting currency', exact: true });
  await expect(netCurrency).toHaveValue('JPY');
  await expect(netCurrency.getByRole('option', { name: 'JPY', exact: true })).toHaveCount(1);
  await netCurrency.selectOption('GBP');
  await navigate(page, 'settings');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  defaultCurrency = 'EUR';
  await navigate(page, 'net-worth');
  await revealMobileControls(page, 'Accounts & currency');
  await expect(netCurrency).toHaveValue('GBP');

  await navigate(page, 'connected-portfolio');
  await revealMobileControls(page, 'Portfolio filters');
  const investmentCurrency = page.getByRole('combobox', { name: 'Investment reporting currency', exact: true });
  await expect(investmentCurrency).toHaveValue('EUR');
  await expect(investmentCurrency.getByRole('option', { name: 'EUR', exact: true })).toHaveCount(1);
  await investmentCurrency.selectOption('GBP');
  await navigate(page, 'settings');
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  defaultCurrency = 'JPY';
  await navigate(page, 'connected-portfolio');
  await revealMobileControls(page, 'Portfolio filters');
  await expect(investmentCurrency).toHaveValue('GBP');
});

test('workspace reports send source conflicts to bank evidence instead of transaction treatment', async ({ page }) => {
  let nativeReviewRequests = 0;
  await page.route('**/api/workspace/native-transactions/**', route => { nativeReviewRequests++; return route.fulfill({ status: 400, json: { detail: 'Unexpected treatment review' } }); });
  await page.route('**/api/workspace/reports/cashflow?**', route => route.fulfill({ json: {
    currency: 'GBP', totals: { income: 0, spending: 0, net: 0 }, monthly: [], categories: [], coverage: {}, method: 'Synthetic source evidence fixture.',
    partial: true, missing_fx_count: 0, possible_overlap_count: 0, stale_review_count: 0, source_review_count: 1, event_count: 1,
    events: [{ event_id: 'bank:9000', account_id: 'qa-current', account_name: 'Example bank', txn_date: '2026-09-20', description: 'Conflicting bank amount', category: 'Uncategorized', native_amount: -10, currency: 'EUR', reporting_amount: null, fx: null, included: false, source_review: true, review_reason: 'The bank changed its source currency.' }],
  } }));
  await page.goto('/#settings');
  await page.getByText('Workspace reports', { exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: '1 transaction needs bank source evidence checked.' })).toBeVisible();
  await page.getByText('Amounts and conversion evidence (1 transaction)', { exact: true }).click();
  const row = page.getByRole('row').filter({ hasText: 'Conflicting bank amount' });
  await expect(row).toContainText('The bank changed its source currency.');
  await expect(row.getByRole('button', { name: 'Review', exact: true })).toHaveCount(0);
  await row.getByRole('button', { name: 'Review bank evidence', exact: true }).click();
  await expect(page).toHaveURL(/#banking$/);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(nativeReviewRequests).toBe(0);
});

for (const [locale, total, iraqiAmount, afghanAmount, rate] of [
  ['en-GB', '3.328', '1.664', '1.66', '1.00241'],
  ['de-DE', '3,328', '1,664', '1,66', '1,00241'],
] as const) {
  test(`workspace reports preserve native currency precision with ${locale} amount and rate formatting`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
    await page.route('**/api/workspace/preferences', route => route.fulfill({ json: {
      reporting_currency: 'IQD', locale, timezone: 'Europe/London',
    } }));
    await page.route('**/api/workspace/reports/cashflow?**', route => route.fulfill({ json: {
      currency: 'IQD', totals: { income: 3.328, spending: 0, net: 3.328 },
      monthly: [{ month: '2026-09', income: 3.328, spending: 0, net: 3.328 }], categories: [],
      coverage: { '2026-09': { complete: true, missing_accounts: [] } }, method: 'Synthetic formatting fixture.',
      partial: false, missing_fx_count: 0, possible_overlap_count: 0, stale_review_count: 0, source_review_count: 0, event_count: 2,
      events: [
        { event_id: 'ledger:9101', account_id: 'qa-iqd', account_name: 'QA dinar account', txn_date: '2026-09-20',
          description: 'QA Iraqi payment', category: 'Income', native_amount: 1.664, currency: 'IQD', reporting_amount: 1.664,
          fx: { source: 'Original currency', date: '2026-09-20', rate: 1 }, included: true },
        { event_id: 'ledger:9102', account_id: 'qa-afn', account_name: 'QA afghani account', txn_date: '2026-09-20',
          description: 'QA Afghan payment', category: 'Income', native_amount: 1.66, currency: 'AFN', reporting_amount: 1.664,
          fx: { source: 'Synthetic dated quote', date: '2026-09-20', rate: 1.664 / 1.66 }, included: true },
      ],
    } }));
    await page.goto('/#settings');
    await page.getByText('Workspace reports', { exact: true }).click();
    const income = page.getByText('Income · IQD', { exact: true }).locator('..');
    await expect(income).toContainText(total);
    await page.getByText('Amounts and conversion evidence (2 transactions)', { exact: true }).click();
    const iraqiRow = page.getByRole('row').filter({ hasText: 'QA Iraqi payment' });
    const afghanRow = page.getByRole('row').filter({ hasText: 'QA Afghan payment' });
    await expect(iraqiRow.getByRole('cell').nth(2)).toContainText(iraqiAmount);
    await expect(afghanRow.getByRole('cell').nth(2)).toContainText(afghanAmount);
    await expect(afghanRow.getByRole('cell').nth(4)).toContainText(rate);
    expect(errors).toEqual([]);
  });
}
