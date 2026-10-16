import { test, expect, type Page } from '@playwright/test';

async function mockAmex(page: Page, connected: boolean, unconfirmed = false) {
  const fetchedAt = new Date().toISOString();
  const local = { id: 'qa-amex', bank: 'amex', display_name: 'QA Amex Gold', account_type: 'credit', currency: 'GBP', connected: connected && !unconfirmed, balance: -80, balance_asof: '2026-09-01', n_txns: 2 };
  // A zero balance still represents a credit-card account; type is saved metadata.
  const account = { id: 'qa-amex-connection', account_id: unconfirmed ? null : local.id, institution_name: 'American Express', name: 'Gold card', currency: 'GBP', status: 'active', present: true, excluded: false, mapping_status: unconfirmed ? 'review' : 'mapped', mapping_candidates: [], requires_balance_direction: unconfirmed, balance: { amount: unconfirmed ? null : 0, reported_amount: unconfirmed ? 500 : 0, currency: 'GBP', fetched_at: fetchedAt }, last_synced_at: fetchedAt, last_error: null, amount_multiplier: 1, balance_multiplier: -1, counts: { cached: 2, imported: 1, matched: 0, pending: 1, review: 0 } };
  const banking = { configured: true, accounts: connected ? [account] : [], local_accounts: [local], institutions: [], totals_by_currency: connected && !unconfirmed ? { GBP: 0 } : {}, sync: { last_attempt_at: fetchedAt, last_success_at: fetchedAt, error: null, running: false, interval_seconds: 900 }, stats: { accounts: Number(connected), mapped: Number(connected), cached: connected ? 2 : 0, imported: connected ? 1 : 0, review: 0, pending: connected ? 1 : 0 } };
  await page.route('**/*', route => {
    const request = route.request();
    if (new URL(request.url()).hostname !== '127.0.0.1' || !['GET', 'HEAD'].includes(request.method())) return route.abort();
    return route.continue();
  });
  await page.route('**/api/revision', route => route.fulfill({ json: { revision: 'amex-connection-ui-test' } }));
  await page.route('**/api/banking**', route => {
    if (new URL(route.request().url()).pathname === '/api/banking') return route.fulfill({ json: banking });
    return route.fulfill({ json: { account, total: 2, transactions: [
      { id: 1, provider_id: 'booked', date: '2026-09-18', amount: -15, currency: 'GBP', merchant: 'QA new card purchase', description: 'Booked card payment', is_pending: false, status: 'imported', transaction_id: 99, review_reason: null },
      { id: 2, provider_id: 'pending', date: '2026-09-19', amount: -5, currency: 'GBP', merchant: 'QA pending card payment', description: 'Authorisation', is_pending: true, status: 'pending', transaction_id: null, review_reason: null },
    ], balance_history: [account.balance], reviews: [], totals_by_currency: { GBP: { money_in: 0, money_out: 15 } } } });
  });
  await page.route('**/api/transactions?**', route => route.fulfill({ json: { total: 2, offset: 0, limit: 30, sum_in: 0, sum_out: -95, items: [
    { id: 98, account_id: local.id, txn_date: '2026-09-01', merchant: 'QA retained statement purchase', description: 'Original statement description', amount: -80, category: 'Shopping', transfer_group: null },
    { id: 99, account_id: local.id, txn_date: '2026-09-18', merchant: 'QA new card purchase', description: 'Booked card payment', amount: -15, category: 'Shopping', transfer_group: null },
  ] } }));
  await page.goto('/#banking');
}

test('a missing Amex connection retains history and gives a provider-neutral next step', async ({ page }) => {
  await mockAmex(page, false);
  await page.getByText('How bank feeds and statement history work together', { exact: true }).click();
  await expect(page.getByText(/Missing a connected account\?/)).toContainText('Check which accounts your provider connection can access in Settings');
  await page.getByRole('button', { name: 'Open QA Amex Gold, GBP', exact: true }).click();
  await expect(page.getByText('Credit card', { exact: true })).toBeVisible();
  await expect(page.getByText('Imported statements', { exact: true })).toBeVisible();
  await expect(page.getByText('QA retained statement purchase', { exact: true })).toBeVisible();
  await expect(page.getByText(/When a bank connection returns this account/)).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Amex remains statement only');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('connected Amex opens one credit account with retained ledger and bank activity', async ({ page }) => {
  await mockAmex(page, true);
  const openAccount = page.getByRole('button', { name: 'Open QA Amex Gold, GBP', exact: true });
  await expect(openAccount).toHaveCount(1);
  await openAccount.click();
  await expect(page.getByText('Credit card', { exact: true })).toBeVisible();
  await expect(page.getByText('Lunch Flow bank feed', { exact: true })).toBeVisible();
  await expect(page.getByText('QA retained statement purchase', { exact: true })).toBeVisible();
  await expect(page.getByText('QA new card purchase', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Bank feed', exact: true }).click();
  await expect(page.getByText('QA pending card payment', { exact: true })).toBeVisible();
  await expect(page.getByText('QA new card purchase', { exact: true })).toBeVisible();
  await expect(page.getByText('Statement only', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});


test('an unconfirmed card balance stays out of bank totals while its sign evidence is visible', async ({ page }) => {
  await mockAmex(page, true, true);
  const totals = page.getByRole('region', { name: 'Connected bank balances', exact: true });
  await expect(totals).toContainText('No confirmed connected balance is available yet.');
  await expect(totals).not.toContainText('£500.00');
  await page.getByRole('button', { name: 'Explore American Express', exact: true }).click();
  await expect(totals).not.toContainText('£500.00');
  await page.getByRole('button', { name: 'Open Gold card, GBP', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Account balance', exact: true })).toContainText('Unconfirmed bank balance');
  await expect(page.getByText('Provider-reported balance: £500.00. Confirm how the bank reports debt before including this card in balances.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Link saved history', exact: true })).toBeDisabled();
});
