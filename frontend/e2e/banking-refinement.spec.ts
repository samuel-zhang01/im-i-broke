import { revealMobileControls } from "./navigation-helpers";
import { test, expect, type Page } from '@playwright/test';

async function seedBanking(page: Page) {
  const stamp = new Date().toISOString();
  const counts = { cached: 2, imported: 1, matched: 0, pending: 0, review: 1 };
  const accounts = Array.from({ length: 12 }, (_, i) => ({
    id: `bank-${i}`, account_id: `saved-${i}`, institution_name: i === 0 ? 'American Express' : 'Revolut',
    name: i === 0 ? 'Amex card' : `Daily account ${i}`, currency: 'GBP', status: 'active', present: true,
    excluded: false, mapping_status: 'mapped', mapping_candidates: [], last_synced_at: stamp, last_error: null,
    counts, balance: { amount: i === 0 ? -642.94 : 123.45, currency: 'GBP', fetched_at: stamp },
  }));
  const data = { configured: true, accounts, local_accounts: accounts.map((a, i) => ({
    id: a.account_id, bank: a.institution_name, display_name: a.name, account_type: i === 0 ? 'credit' : 'current',
    currency: 'GBP', connected: true, balance: a.balance.amount, n_txns: 2,
  })), institutions: [], totals_by_currency: { GBP: 715.01 },
  sync: { last_success_at: stamp, running: false, interval_seconds: 14400, error: null },
  stats: { accounts: 12, mapped: 12, cached: 24, imported: 12, review: 12, pending: 0 } };
  const mutations: unknown[] = [];
  await page.route('**/api/banking**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.includes('/reviews/')) {
      mutations.push(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true } });
    }
    if (url.pathname === '/api/banking') return route.fulfill({ json: data });
    const account = accounts.find(a => a.id === url.pathname.split('/').at(-1)) || accounts[0];
    const linked = { id: 101, provider_id: 'revised', transaction_id: 10, date: '2026-09-18', amount: -9,
      currency: 'GBP', merchant: 'Changed bank purchase', description: 'Provider revised a linked purchase',
      is_pending: true, status: 'review', review_reason: 'A linked transaction changed. Original ledger entry retained.', candidates: [] };
    const pending = { ...linked, id: 102, merchant: 'Pending coffee', transaction_id: null, status: 'pending' };
    const booked = { ...linked, id: 103, merchant: 'Booked lunch', transaction_id: 11, is_pending: false, status: 'imported' };
    let rows = url.searchParams.get('status') === 'review' ? [linked] : url.searchParams.get('status') === 'pending' ? [pending] : [pending, booked];
    const query = url.searchParams.get('q');
    if (query) rows = rows.filter(row => row.merchant.toLowerCase().includes(query.toLowerCase()));
    return route.fulfill({ json: { account, transactions: rows, total: rows.length, reviews: rows.filter(r => r.status === 'review'),
      balance_history: [account.balance], totals_by_currency: {} } });
  });
  await page.route('**/api/transactions?**', route => route.fulfill({ json: { total: 1, offset: 0, limit: 30, sum_in: 0, sum_out: -9,
    items: [{ id: 10, account_id: 'saved-0', txn_date: '2026-09-17', merchant: 'Original statement purchase', description: 'Preserved', amount: -9, category: 'Food', transfer_group: null }] } }));
  await page.goto('/#banking');
  await expect(page.getByRole('heading', { name: 'Your banks', exact: true })).toBeVisible();
  return mutations;
}

test('drilling into a low account brings its heading and keyboard focus into view', async ({ page }) => {
  await seedBanking(page);
  await page.getByRole('button', { name: 'Open Daily account 11, GBP', exact: true }).click();
  const heading = page.getByRole('heading', { name: 'Account details', exact: true });
  await expect(heading).toBeFocused();
  await expect(heading).toBeInViewport();
  await page.getByRole('button', { name: 'All banks', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Bank balances', exact: true })).toBeFocused();
});

test('bank feed filters can all be reset at 320px without leaving the account', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await seedBanking(page);
  await page.getByRole('button', { name: 'Open Amex card, GBP', exact: true }).click();
  await page.getByRole('button', { name: 'Bank feed', exact: true }).click();
  await revealMobileControls(page, 'Activity filters');
  await page.getByRole('combobox', { name: 'Settlement', exact: true }).selectOption('pending');
  await page.getByRole('searchbox', { name: 'Search activity', exact: true }).fill('coffee');
  await page.getByLabel('From', { exact: true }).fill('2026-09-01');
  await expect(page.getByText('Pending coffee', { exact: true })).toBeVisible();
  await expect(page.getByText('Booked lunch', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Settlement', exact: true })).toHaveValue('all');
  await expect(page.getByRole('searchbox', { name: 'Search activity', exact: true })).toHaveValue('');
  await expect(page.getByLabel('From', { exact: true })).toHaveValue('');
  await expect(page.getByText('Booked lunch', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  const targets = await page.locator('main summary').evaluateAll(elements => elements.filter(e => e.getBoundingClientRect().height > 0).map(e => e.getBoundingClientRect().height));
  expect(targets.every(height => height >= 44)).toBeTruthy();
});

test('linked bank revisions only offer preserving the original ledger entry', async ({ page }) => {
  const mutations = await seedBanking(page);
  await page.getByRole('button', { name: 'Open Amex card, GBP', exact: true }).click();
  await page.getByRole('button', { name: 'Review (1)', exact: true }).click();
  await expect(page.getByText('Changed bank purchase', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add as new transaction', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Match existing', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Keep original ledger entry', exact: true }).click();
  await expect.poll(() => mutations.length).toBe(1);
  expect(mutations[0]).toEqual({ action: 'ignore' });
});


test('balance arithmetic is available on demand and account search remains nearby', async ({ page }) => {
  await seedBanking(page);
  const details = page.getByRole('region', { name: 'Connected bank balances', exact: true }).locator('details');
  await expect(details).not.toHaveAttribute('open', '');
  await expect(details.locator('dl')).not.toBeVisible();
  await details.locator('summary').click();
  await expect(details.locator('dl')).toBeVisible();
  await details.locator('summary').click();
  await page.getByRole('searchbox', { name: 'Find bank or account', exact: true }).fill('Amex');
  await expect(page.getByRole('button', { name: 'Open Amex card, GBP', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open Daily account 11, GBP', exact: true })).toHaveCount(0);
});
