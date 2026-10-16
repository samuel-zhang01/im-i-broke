import { test, expect } from '@playwright/test';
import { revealMobileControls } from './navigation-helpers';

const patterns = Array.from({ length: 40 }, (_, index) => ({
  key: `pattern-${index}`, merchant: `Utility ${String(index).padStart(2, '0')}`, account: 'Demo Current', category: 'Bills',
  cadence: 'monthly', cadence_days: 30, typical_amount: 20 + index, current_amount: 20 + index,
  monthly_cost: 20 + index, historical_monthly_cost: 20 + index, first_seen: '2026-01-01', last_seen: '2026-09-01',
  n_occurrences: 8, txn_ids: [], next_expected: null, payments: [], status: 'active',
  pattern_type: 'regular_payment', forecast_eligible: true, data_stale: false,
}));

async function navigate(page: import('@playwright/test').Page, destination: string, query?: string) {
  await page.evaluate(({ destination, query }) => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page: destination, query } })), { destination, query });
}

test('recurring controls and pagination survive navigation and scroll waits for delayed real content', async ({ page }) => {
  let hold = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/recurring?**', async route => {
    if (hold) await gate;
    await route.fulfill({ json: { patterns, candidates: [] } });
  });
  await page.goto('/#recurring');
  await page.getByRole('searchbox', { name: 'Find a payment' }).fill('Utility');
  await revealMobileControls(page, 'Payment filters & sort');
  await page.getByRole('combobox', { name: 'Evidence', exact: true }).selectOption('current');
  await page.getByRole('combobox', { name: 'Sort by', exact: true }).selectOption('merchant');
  await page.getByRole('button', { name: 'Reverse recurring sort order' }).click();
  await page.getByRole('navigation', { name: 'Recurring pattern pages' }).getByRole('button', { name: /Next page/ }).click();
  await expect(page.getByRole('navigation', { name: 'Recurring pattern pages' })).toContainText('19–36 of 40');
  await page.evaluate(() => window.scrollTo(0, 420));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(420);
  await navigate(page, 'settings');
  await expect(page.getByRole('heading', { level: 1, name: 'Settings', exact: true })).toBeVisible();
  // Expire the short read cache without sleeping or changing browser timers.
  await page.clock.setFixedTime(new Date(Date.now() + 60_000));
  hold = true;
  await page.goBack();
  await expect(page.getByRole('heading', { level: 1, name: 'Recurring', exact: true })).toBeVisible();
  await expect(page.getByRole('status', { name: 'Loading…', exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  release();
  await expect(page.getByRole('navigation', { name: 'Recurring pattern pages' })).toContainText('19–36 of 40');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(420);
  if ((page.viewportSize()?.width ?? 1440) < 640) await expect(page.getByRole('button', { name: 'Payment filters & sort', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByRole('searchbox', { name: 'Find a payment' })).toHaveValue('Utility');
  await revealMobileControls(page, 'Payment filters & sort');
  await expect(page.getByRole('combobox', { name: 'Evidence', exact: true })).toHaveValue('current');
  await expect(page.getByRole('combobox', { name: 'Sort by', exact: true })).toHaveValue('merchant');
  await expect(page.getByRole('button', { name: 'Reverse recurring sort order' })).toHaveText('↓');
  await expect(page.getByRole('region', { name: 'Recurring payment records', exact: true }).getByRole('button', { name: /^View payments/ }).first()).toHaveAccessibleName('View payments for Utility 21 in Demo Current');

  // An explicit search destination supersedes remembered filters and page.
  await navigate(page, 'settings');
  await navigate(page, 'recurring', 'Utility 03');
  await expect(page.getByRole('searchbox', { name: 'Find a payment' })).toHaveValue('Utility 03');
  await expect(page.getByRole('navigation', { name: 'Recurring pattern pages' })).toContainText('1–1 of 1');
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  await revealMobileControls(page, 'Payment filters & sort');
  await expect(page.getByRole('combobox', { name: 'Evidence', exact: true })).toHaveValue('all');
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toContain('Utility');
});

test('manual input cancels delayed scroll restoration and a new launch resets view controls', async ({ page }) => {
  let hold = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/recurring?**', async route => { if (hold) await gate; await route.fulfill({ json: { patterns, candidates: [] } }); });
  await page.goto('/#recurring');
  await page.getByRole('searchbox', { name: 'Find a payment' }).fill('Utility');
  await page.evaluate(() => window.scrollTo(0, 420));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(420);
  await navigate(page, 'settings');
  await page.clock.setFixedTime(new Date(Date.now() + 60_000));
  hold = true;
  await page.goBack();
  await expect(page.getByRole('status', { name: 'Loading…', exact: true })).toBeVisible();
  // Controls inside the pending results card are not mounted yet. Keyboard
  // focus on the always-present main landmark must also cancel restoration.
  await page.locator('#main-content').focus();
  const userPosition = await page.evaluate(() => window.scrollY);
  release();
  await expect(page.getByRole('navigation', { name: 'Recurring pattern pages' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(userPosition);
  await expect(page.getByRole('searchbox', { name: 'Find a payment' })).toHaveValue('Utility');
  await page.reload();
  await expect(page.getByRole('searchbox', { name: 'Find a payment' })).toHaveValue('');
});

test('transaction account, filters and page persist in memory and sign-out clears them', async ({ page }) => {
  let authenticated = true;
  await page.route('**/api/auth/status', route => route.fulfill({ json: { enabled: true, configured: true, authenticated } }));
  await page.route('**/api/auth/logout', route => { authenticated = false; return route.fulfill({ json: { ok: true } }); });
  await page.route('**/api/auth/login', route => { authenticated = true; return route.fulfill({ json: { ok: true } }); });
  await page.route('**/api/summary**', route => route.fulfill({ json: { accounts: [{ id: 'demo-current', display_name: 'Demo Current', currency: 'GBP', account_type: 'current' }], n_transactions: 120 } }));
  await page.route('**/api/transactions?**', route => {
    const query = new URL(route.request().url()).searchParams;
    const offset = Number(query.get('offset') || 0), limit = Number(query.get('limit') || 50);
    return route.fulfill({ json: { offset, limit, total: 120, sum_in: 0, sum_out: -120,
      items: Array.from({ length: limit }, (_, index) => ({ id: offset + index + 1, account_id: 'demo-current', account: 'Demo Current', txn_date: '2026-09-21', description: `Utility ${offset + index + 1}`, merchant: null, amount: -1, category: 'Bills', is_anomaly: 0 })) } });
  });
  await page.goto('/#transactions');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByRole('combobox', { name: 'Account', exact: true }).selectOption('demo-current');
  await page.getByLabel('Search transactions', { exact: true }).fill('Utility');
  await page.getByRole('combobox', { name: 'Sort transactions' }).selectOption('amount:asc');
  await expect(page.getByRole('button', { name: 'Next →', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Next →', exact: true }).click();
  const pageLabel = (page.viewportSize()?.width ?? 1440) < 640 ? '21–40 of 120' : '51–100 of 120';
  await expect(page.getByText(pageLabel, { exact: true })).toBeVisible();
  await navigate(page, 'settings');
  await page.goBack();
  await expect(page.getByText(pageLabel, { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Transaction filters');
  await expect(page.getByRole('combobox', { name: 'Account', exact: true })).toHaveValue('demo-current');
  await expect(page.getByLabel('Search transactions', { exact: true })).toHaveValue('Utility');
  await expect(page.getByRole('combobox', { name: 'Sort transactions' })).toHaveValue('amount:asc');
  await navigate(page, 'settings');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByLabel('Password', { exact: true }).fill('synthetic-password');
  await page.getByRole('textbox', { name: 'Authenticator or recovery code', exact: true }).fill('123456');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Settings', exact: true })).toBeVisible();
  await navigate(page, 'transactions');
  await expect(page.getByLabel('Search transactions', { exact: true })).toHaveValue('');
  await revealMobileControls(page, 'Transaction filters');
  await expect(page.getByRole('combobox', { name: 'Account', exact: true })).toHaveValue('');
  await expect(page.getByRole('combobox', { name: 'Sort transactions' })).toHaveValue('date:desc');
});
