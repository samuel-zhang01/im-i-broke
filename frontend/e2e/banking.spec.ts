import { revealMobileControls } from "./navigation-helpers";
import { test, expect, type Page } from '@playwright/test';

function fixture() {
  const counts = { cached: 3, imported: 1, matched: 1, pending: 1, review: 0 };
  const base = { currency: 'GBP', status: 'active', excluded: false, present: true, mapping_status: 'mapped', mapping_candidates: [], last_synced_at: '2026-09-18T11:00:00Z', last_error: null, counts };
  const accounts = [
    { ...base, id: 'bank-main', account_id: 'qa-current', institution_name: 'Lloyds Bank', name: 'Current', balance: { amount: 1234.56, currency: 'GBP', fetched_at: '2026-09-18T11:00:00Z' } },
    { ...base, id: 'bank-sgd', account_id: 'qa-sgd', institution_name: 'DBS Singapore', name: 'Everyday savings', currency: 'SGD', counts: { ...counts, pending: 0 }, balance: { amount: 2500, currency: 'SGD', fetched_at: '2026-09-18T11:00:00Z' } },
  ];
  return {
    configured: true, accounts,
    local_accounts: [
      { id: 'qa-current', bank: 'Lloyds', display_name: 'Daily spending', account_type: 'current', currency: 'GBP', connected: true, balance: 1234.56, n_txns: 20 },
      { id: 'qa-sgd', bank: 'DBS', display_name: 'Everyday savings', account_type: 'savings', currency: 'SGD', connected: true, balance: 2500, n_txns: 0 },
      { id: 'qa-amex', bank: 'Amex', display_name: 'Amex Gold', account_type: 'credit', currency: 'GBP', connected: false, balance: -200, balance_asof: '2026-08-31', n_txns: 45 },
    ],
    institutions: [], totals_by_currency: { GBP: 1234.56, SGD: 2500 },
    sync: { last_attempt_at: '2026-09-18T11:00:00Z', last_success_at: '2026-09-18T11:00:00Z', error: null, running: false, interval_seconds: 14400 },
    stats: { accounts: 2, mapped: 2, cached: 6, imported: 2, review: 0, pending: 1 },
  };
}
async function seedRoutes(page: Page) {
  const data = fixture();
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'banking'));
  await page.route('**/api/banking**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/banking/sync') return route.fulfill({ status: 502, body: 'PRIVATE_PROVIDER_KEY_SHOULD_NOT_DISPLAY' });
    if (url.pathname === '/api/banking') return route.fulfill({ json: data });
    const id = decodeURIComponent(url.pathname.split('/').at(-1)!);
    const account = data.accounts.find(a => a.id === id)!;
    const pending = { id: 3, provider_id: 'new-pending', date: '2026-09-18', amount: -9.5, currency: account.currency, merchant: 'Pending coffee', description: 'Coffee authorisation', is_pending: true, status: 'pending', transaction_id: null, review_reason: null };
    const booked = { id: 2, provider_id: 'new-booked', date: '2026-09-17', amount: 100, currency: account.currency, merchant: 'Booked refund', description: 'Refund', is_pending: false, status: account.currency === 'GBP' ? 'imported' : 'native', transaction_id: account.currency === 'GBP' ? 999 : null, review_reason: null };
    let rows = url.searchParams.get('status') === 'pending' ? [pending] : [pending, booked];
    const query = url.searchParams.get('q')?.toLowerCase();
    if (query) rows = rows.filter(row => row.merchant.toLowerCase().includes(query));
    return route.fulfill({ json: { account, transactions: rows, total: rows.length, balance_history: [account.balance], reviews: [], totals_by_currency: url.searchParams.get('status') === 'pending' ? {} : { [account.currency]: { money_in: 100, money_out: 0 } } } });
  });
  await page.route('**/api/transactions?**', async route => {
    const url = new URL(route.request().url());
    const rows = [
      { id: 998, account_id: url.searchParams.get('account'), txn_date: '2026-08-20', merchant: 'Preserved statement purchase', description: 'Original reviewed purchase', amount: -25, category: 'Food', transfer_group: null },
      { id: 999, account_id: url.searchParams.get('account'), txn_date: '2026-09-17', merchant: 'Booked bank refund', description: 'Newly reconciled activity', amount: 100, category: 'Refund', transfer_group: null },
    ].filter(row => !url.searchParams.get('q') || row.merchant.toLowerCase().includes(url.searchParams.get('q')!.toLowerCase()));
    await route.fulfill({ json: { total: rows.length, offset: 0, limit: 30, sum_in: 100, sum_out: -25, items: rows } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Bank balances', exact: true })).toBeVisible();
}

test('renewed bank feed reconnects to saved history after identity confirmation', async ({ page }) => {
  const data = fixture();
  const previous = { ...data.accounts[0], id: 'previous-feed', account_id: 'qa-current',
    institution_name: 'HSBC', name: 'Previous card feed', present: false, status: 'ACTIVE',
    amount_multiplier: -1, balance_multiplier: -1 };
  const renewed = { ...previous, id: 'renewed-feed', account_id: null, name: 'Renewed card',
    present: true, amount_multiplier: 1, balance_multiplier: 1, mapping_status: 'review',
    mapping_candidates: [], mapping_reason: 'A previous connection from this bank is missing or inactive.',
    requires_balance_direction: true };
  const bankData = { ...data, accounts: [previous, renewed], local_accounts: [
    { ...data.local_accounts[0], bank: 'HSBC', display_name: 'Saved July card', account_type: 'credit', connected: true }],
  };
  const mutations: unknown[] = [];
  let recovered = false;
  const response = () => recovered ? { ...bankData, accounts: [
    { ...previous, excluded: true }, { ...renewed, account_id: 'qa-current', mapping_status: 'mapped' }],
  } : bankData;
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'banking'));
  await page.route('**/api/banking**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/mapping')) {
      mutations.push(route.request().postDataJSON());
      if (mutations.length === 1) return route.fulfill({ status: 409, json: { detail: 'Synthetic retry.' } });
      recovered = true;
      return route.fulfill({ json: response() });
    }
    if (url.pathname === '/api/banking') return route.fulfill({ json: response() });
    return route.fulfill({ json: { account: renewed, transactions: [], total: 0, balance_history: [], reviews: [], totals_by_currency: {} } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open Renewed card, GBP', exact: true }).click();
  await page.getByRole('combobox', { name: 'Saved account', exact: true }).selectOption('qa-current');
  await page.getByLabel('How should this card balance appear?', { exact: true }).selectOption('-1');
  const save = page.getByRole('button', { name: 'Reconnect saved history', exact: true });
  await expect(save).toBeDisabled();
  await page.getByRole('checkbox', { name: 'This is the same financial account as the previous feed. Keep its saved history and card settings.', exact: true }).check();
  await save.click();
  await expect.poll(() => mutations.length).toBe(1);
  expect(mutations[0]).toEqual({ account_id: 'qa-current', create_new: false,
    amount_multiplier: -1, balance_multiplier: -1, replace_connection_id: 'previous-feed', confirm_reconnection: true });
  await expect(page.getByRole('alert')).toContainText('This account or transaction changed');
  await expect(page.getByRole('combobox', { name: 'Saved account', exact: true })).toHaveValue('qa-current');
  await expect(save).toBeEnabled();
  await save.click();
  await expect.poll(() => mutations.length).toBe(2);
  await expect(page.getByRole('heading', { name: 'Saved July card', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Link existing account history', exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('banks and individual accounts retain statements and separate native currency feeds', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await seedRoutes(page);
  await expect(page.getByRole('region', { name: 'Connected bank balances', exact: true })).toBeVisible();
  await expect(page.getByLabel('Connected bank balances', { exact: true })).toContainText('£1,234.56');
  await expect(page.getByLabel('Connected bank balances', { exact: true })).toContainText('SGD');
  await expect(page.getByRole('button', { name: 'Open Amex Gold, GBP', exact: true })).toBeVisible();
  await page.screenshot({ path: `../output/playwright/banking-${testInfo.project.name}.png`, fullPage: true });
  await page.getByRole('button', { name: 'Explore Lloyds', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your accounts', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Open Daily spending, GBP', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Daily spending', exact: true })).toBeVisible();
  await expect(page.getByText('Preserved statement purchase', { exact: true })).toBeVisible();
  await expect(page.getByText('Booked bank refund', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Bank feed', exact: true }).click();
  await revealMobileControls(page, 'Activity filters');
  await page.getByRole('combobox', { name: 'Settlement', exact: true }).selectOption('pending');
  await expect(page.getByText('Pending coffee', { exact: true })).toBeVisible();
  await expect(page.getByText('Booked refund', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Money in · GBP', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'All banks', exact: true }).click();
  await page.getByRole('button', { name: 'Open Everyday savings, SGD', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Complete ledger', exact: true })).toHaveCount(0);
  await expect(page.getByText('Money in · SGD', { exact: true })).toBeVisible();
  await expect(page.getByText('Money in · GBP', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
});

test('failed bank refresh retains cached accounts and hides raw upstream errors', async ({ page }) => {
  await seedRoutes(page);
  await page.getByRole('button', { name: 'Refresh banks', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Your saved history is still available');
  await expect(page.locator('body')).not.toContainText('PRIVATE_PROVIDER_KEY');
  await expect(page.getByRole('button', { name: 'Open Daily spending, GBP', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Open Amex Gold, GBP', exact: true }).click();
  await expect(page.getByText('Statement only', { exact: true })).toBeVisible();
  await expect(page.getByText('Preserved statement purchase', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Bank feed', exact: true })).toHaveCount(0);
});

test('linking a credit card requires a balance direction and preserves the chosen sign', async ({ page }) => {
  const data = fixture();
  const card = { ...data.accounts[0], id: 'new-credit', account_id: null, institution_name: 'HSBC', name: 'Credit card', mapping_status: 'review', mapping_candidates: ['qa-current'], requires_balance_direction: true, balance: { amount: 500, currency: 'GBP', fetched_at: '2026-09-18T11:00:00Z' } };
  const bankData = { ...data, accounts: [card], local_accounts: [{ ...data.local_accounts[0], bank: 'HSBC', display_name: 'Saved credit card', account_type: 'credit', connected: false }] };
  const mutations: unknown[] = [];
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'banking'));
  await page.route('**/api/banking**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/mapping')) {
      mutations.push(route.request().postDataJSON());
      return route.fulfill({ json: bankData });
    }
    if (url.pathname === '/api/banking') return route.fulfill({ json: bankData });
    return route.fulfill({ json: { account: card, transactions: [], total: 0, balance_history: [], reviews: [], totals_by_currency: {} } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Open Credit card, GBP', exact: true }).click();
  await page.getByRole('combobox', { name: 'Saved account', exact: true }).selectOption('qa-current');
  await expect(page.getByRole('button', { name: 'Link saved history', exact: true })).toBeDisabled();
  await page.getByLabel('How should this card balance appear?', { exact: true }).selectOption('-1');
  await page.getByRole('button', { name: 'Link saved history', exact: true }).click();
  await expect.poll(() => mutations.length).toBe(1);
  expect(mutations[0]).toEqual({ account_id: 'qa-current', create_new: false, balance_multiplier: -1, amount_multiplier: 1 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('background sync completion clears the refreshing state without a manual reload', async ({ page }) => {
  const data = fixture();
  let reads = 0;
  await page.route('**/api/banking', route => {
    reads += 1;
    return route.fulfill({ json: { ...data, sync: { ...data.sync, running: reads === 1, error: reads === 1 ? 'Lunch Flow is temporarily unavailable.' : null } } });
  });
  await page.goto('/#banking');
  await expect(page.getByRole('button', { name: 'Refreshing banks…' })).toBeDisabled();
  await expect(page.getByText('Lunch Flow is temporarily unavailable.', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Refresh banks', exact: true })).toBeEnabled();
  expect(reads).toBeGreaterThanOrEqual(2);
});
