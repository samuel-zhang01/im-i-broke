import { revealMobileControls } from './navigation-helpers';
import { navigateFinance } from "./navigation-helpers";
import { test, expect, type Page } from '@playwright/test';

async function go(page: Page, id: string, label: string) {
  await navigateFinance(page, id, label);
}
test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('unchanged data does not duplicate summary or transaction reads', async ({ page }) => {
  // Keep this test about client deduplication; read-side cache initialization
  // may legitimately advance the synthetic server's file-based revision.
  await page.route('**/api/revision', route => route.fulfill({ json: { revision: 'stable-client-test' } }));
  let summaryReleased = false, earlyHistoryReads = 0;
  await page.route('**/api/summary', async route => {
    const response = await route.fetch();
    await new Promise(resolve => setTimeout(resolve, 300));
    summaryReleased = true;
    await route.fulfill({ response });
  });
  const counts = new Map<string, number>();
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    counts.set(path, (counts.get(path) || 0) + 1);
    if (!summaryReleased && ['/api/timeseries', '/api/spending-explorer'].includes(path)) earlyHistoryReads++;
  });
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'overview'));
  await page.goto('/#overview');
  await expect(page.getByRole('heading', { name: 'The latest picture', exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  expect(counts.get('/api/summary')).toBe(1);
  expect(counts.get('/api/wealth')).toBe(1);
  expect(counts.get('/api/analytics')).toBe(1);
  expect(earlyHistoryReads).toBe(0);
  await go(page, 'transactions', 'Transactions');
  await expect(page.getByRole('button', { name: /^Review QA/ }).first()).toBeVisible();
  await page.waitForTimeout(450);
  expect(counts.get('/api/transactions')).toBe(1);
  expect(counts.get('/api/workspace/category-options')).toBe(1);
  expect(counts.get('/api/summary')).toBe(1);
});

test('merchant directory loads on demand and activity shows saved identity evidence', async ({ page }, info) => {
  const counts = new Map<string, number>();
  page.on('request', request => { const path = new URL(request.url()).pathname; counts.set(path, (counts.get(path) || 0) + 1); });
  await page.route('**/api/merchants/*/activity', route => route.fulfill({ json: {
    merchant: { id: 1, canonical_name: 'QA Cafe', kind: 'merchant', category: 'Eating out', spending_treatment: 'normal', source: 'public_research', source_url: 'https://example.com/cafe' }, currency: 'GBP',
    summary: { transactions: 2, money_in: 5, money_out: 30, net: -25, first_seen: '2026-09-01', last_seen: '2026-09-18', active_months: 1, accounts: 1 },
    accounts: [{ id: 'qa-current', name: 'QA Current' }], monthly: [{ month: '2026-09', money_in: 5, money_out: 30, payments: 2 }],
    transactions: [{ id: 999, txn_date: '2026-09-18', description: 'Original cafe purchase', merchant: 'QA Cafe', category: 'Eating out', amount: -30, account_id: 'qa-current', account: 'QA Current' }],
    bank_labels: [{ label: '<script>bank label text</script>', bank: 'QA Bank', transactions: 1, fetched_at: '2026-09-18T10:00:00Z' }],
    method: 'Recorded GBP movement. Includes transfers and refunds; excludes pending entries.'
  } }));
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'merchants'));
  await page.goto('/');
  await expect(page.getByRole('group', { name: 'Merchant workspace' })).toBeVisible();
  await expect.poll(() => counts.get('/api/merchants/inbox')).toBe(1);
  await page.waitForTimeout(400);
  expect(counts.get('/api/merchants') || 0).toBe(0);
  await page.getByRole('button', { name: /^Saved merchants & aliases/ }).click();
  await page.getByRole('searchbox', { name: 'Search people and merchants', exact: true }).fill('QA Cafe');
  await page.getByRole('button', { name: 'View QA Cafe activity', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Recorded money out');
  await expect(dialog).toContainText('£30.00');
  await dialog.getByText('Bank labels linked to this saved name (1)', { exact: true }).click();
  await expect(dialog.getByText('<script>bank label text</script>', { exact: true })).toBeVisible();
  expect(await dialog.locator('script').count()).toBe(0);
  await expect(dialog).toContainText('Your saved name defines the identity');
  await expect(dialog.getByRole('link', { name: 'business website ↗' })).toHaveAttribute('href', 'https://example.com/cafe');
  await expect(dialog.getByRole('link', { name: 'business website ↗' })).toHaveAttribute('rel', 'noopener noreferrer');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `../output/playwright/merchant-profile-${info.project.name}.png`, fullPage: true });
});

test('recurring cards exclude stale upcoming dates and retain searchable history', async ({ page }, info) => {
  const date = new Date(); date.setDate(date.getDate() + 2);
  const next = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const series = (index: number, extra = {}) => ({ key: `pattern-${index}`, merchant: `QA Pattern ${String(index).padStart(2, '0')}`, account: 'QA Current', category: 'Subscriptions', cadence: 'monthly', cadence_days: 30, typical_amount: 12, current_amount: 12, monthly_cost: 12, first_seen: '2026-01-01', last_seen: '2026-09-01', next_expected: next, n_occurrences: 5, txn_ids: [index], payments: [], data_stale: false, status: 'active', ...extra });
  await page.route('**/api/recurring?**', route => route.fulfill({ json: { patterns: [...Array.from({ length: 22 }, (_, index) => series(index)), series(99, { merchant: 'QA Stale', data_stale: true })], candidates: [] } }));
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'recurring'));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Expected in the next 30 days', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'View upcoming QA Stale payment', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^View payments for/ })).toHaveCount(18);
  await page.getByRole('searchbox', { name: 'Find a payment', exact: true }).fill('QA Stale');
  await expect(page.getByRole('button', { name: 'View payments for QA Stale in QA Current', exact: true })).toBeVisible();
  await revealMobileControls(page, 'Payment filters & sort');
  await page.getByRole('combobox', { name: 'Evidence', exact: true }).selectOption('current');
  await expect(page.getByText('No patterns match these filters.', { exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Find a payment', exact: true }).fill('');
  await page.screenshot({ path: `../output/playwright/recurring-polish-${info.project.name}.png`, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('review queues paginate before rendering hundreds of cards', async ({ page }) => {
  await page.route('**/api/anomalies?status=open', route => route.fulfill({ json: Array.from({ length: 30 }, (_, i) => ({ id: i + 1, account: 'QA Current', txn_date: '2026-09-18', description: `QA Review row ${i}`, merchant: `QA Review row ${i}`, amount: -40, category: 'Shopping', reason: 'A synthetic amount outlier', z: 4, outcome: null })) }));
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'anomalies'));
  await page.goto('/');
  await expect(page.getByRole('button', { name: /^Resolve QA Review row/ })).toHaveCount(12);
  await page.getByRole('button', { name: 'Next page →', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Review queue pages', exact: true })).toContainText('13–24 of 30');
  await page.getByRole('textbox', { name: 'Search anomaly reason or merchant', exact: true }).fill('row 29');
  await expect(page.getByRole('button', { name: 'Resolve QA Review row 29', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Review queue pages', exact: true })).toContainText('1–1 of 1');
});


test('transaction review keeps saved identity separate from original native bank evidence', async ({ page }) => {
  await page.route('**/api/transactions/*/review', async route => {
    const response = await route.fetch();
    const result = await response.json();
    await route.fulfill({ json: { ...result, bank_evidence: { merchant: 'QA raw bank merchant', description: 'QA original native transfer', date: '2026-09-18', amount: -20, currency: 'SGD', status: 'matched', fetched_at: '2026-09-18T10:00:00Z', bank: 'QA Bank', note: 'Bank wording is source evidence; your saved name and category remain authoritative.' } } });
  });
  await page.goto('/#transactions');
  await page.getByRole('button', { name: /^Review QA/ }).first().click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Original bank feed evidence', { exact: true })).toBeVisible();
  await expect(dialog.getByText('QA raw bank merchant', { exact: false })).not.toBeVisible();
  await dialog.getByText('Original bank feed evidence', { exact: true }).click();
  await expect(dialog).toContainText('QA original native transfer');
  await expect(dialog).toContainText('SGD');
  await expect(dialog).toContainText('your saved name and category remain authoritative');
  await expect(dialog.getByRole('textbox', { name: /^Name for this transaction/ })).not.toHaveValue('QA raw bank merchant');
});

test('bank index opens without downloading the chart engine', async ({ page }) => {
  const scriptSizes: Promise<number>[] = [];
  page.on('response', response => {
    if (response.request().resourceType() === 'script') scriptSizes.push(response.body().then(body => body.length));
  });
  await page.goto('/#banking');
  await expect(page.getByRole('heading', { name: 'Bank balances', exact: true })).toBeVisible();
  await page.waitForTimeout(300);
  const sizes = await Promise.all(scriptSizes);
  // The chart engine alone is >1MB; a bank-only route needs the app shell and its own view.
  expect(sizes.reduce((sum, bytes) => sum + bytes, 0)).toBeLessThan(400_000);
});
