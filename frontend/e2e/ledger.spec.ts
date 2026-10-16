import { revealMobileControls } from './navigation-helpers';
import { navigateFinance } from "./navigation-helpers";
import { test, expect, type Page } from '@playwright/test';

const pages = [
  ['overview', 'Overview'], ['analytics', 'Spending'], ['transactions', 'Transactions'],
  ['planning', 'Planning'], ['investments', 'Investments'], ['merchants', 'People & merchants'],
  ['anomalies', 'Review'], ['transfers', 'Transfers'], ['recurring', 'Recurring'],
  ['advice', 'Savings'],
] as const;

async function go(page: Page, id: string, title: string) {
  await page.locator('nav[aria-label="Main navigation"]').waitFor({ state: 'attached' });
  if (id === 'investments') {
    await page.goto('/#investments');
    await expect(page.getByRole('heading', { level: 1, name: 'Investments', exact: true })).toBeVisible();
    return;
  }
  await navigateFinance(page, id, title);
}

test.beforeEach(async ({ page }) => {
  // No external service is required for the browser suite, including quote refresh.
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
});

test('all activity and planning pages load without browser errors or page-level horizontal overflow', async ({ page }) => {
  const failures: string[] = [];
  page.on('pageerror', error => failures.push(error.message));
  page.on('console', message => { if (message.type() === 'error') failures.push(message.text()); });
  page.on('response', response => { if (response.status() >= 500) failures.push(`${response.status()} ${response.url()}`); });
  for (const [id, title] of pages) {
    await go(page, id, title);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByRole('status').filter({ hasText: /Loading|Calculating|Updating spending/ })).toHaveCount(0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    expect(overflow, `${title}: horizontal page overflow`).toBeLessThanOrEqual(1);
    await expect(page.locator('main')).not.toContainText(/\bNaN\b|\bInfinity\b/);
  }
  expect(failures).toEqual([]);
});

test('transaction search, pagination, and custom category filters work', async ({ page }) => {
  await go(page, 'transactions', 'Transactions');
  await page.getByRole('button', { name: 'Next →', exact: true }).click();
  await expect(page.getByRole('button', { name: '← Prev', exact: true })).toBeEnabled();
  await page.getByLabel('Search transactions', { exact: true }).fill('QA Legal');
  await expect(page.getByRole('button', { name: 'Review QA Legal', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '← Prev', exact: true })).toBeDisabled();
  await page.getByLabel('Search transactions', { exact: true }).fill('');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Category', { exact: true }).selectOption('Legal services');
  await expect(page.getByRole('button', { name: 'Review QA Legal', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Review QA Unclassified/ })).toHaveCount(0);
});

test('transaction recurring links open exact evidence and review', async ({ page }) => {
  await go(page, 'transactions', 'Transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill('QA Stream');
  await page.getByRole('button', { name: 'View QA Stream in Recurring', exact: true }).first().click();
  await expect(page.getByRole('heading', { level: 1, name: 'Recurring', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { level: 2, name: 'QA Stream', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Review payment on/ })).toHaveCount(4);
  await page.getByRole('button', { name: /^Review payment on/ }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('QA Stream');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('subscriptions without timing evidence remain inspectable', async ({ page }) => {
  await go(page, 'recurring', 'Recurring');
  await page.getByRole('button', { name: 'View payments for QA Annual in QA Current', exact: true }).click();
  await expect(page.getByRole('heading', { level: 2, name: 'QA Annual', exact: true })).toBeVisible();
  await expect(page.getByText(/No frequency or monthly cost has been assumed/)).toBeVisible();
  await expect(page.getByRole('button', { name: /^Review payment on/ })).toHaveCount(1);
});

test('a review saves its category and note across reload without changing source data', async ({ page, request }, info) => {
  const merchant = `QA Review ${info.project.name}`;
  const before = (await (await request.get('/api/transactions', { params: { q: merchant } })).json()).items.find((t: any) => t.merchant === merchant);
  expect(before).toBeTruthy();
  await go(page, 'transactions', 'Transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill(merchant);
  await page.getByRole('button', { name: `Review ${merchant}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('combobox', { name: 'Spending category', exact: true }).selectOption('Legal services');
  await dialog.getByRole('button', { name: 'Notes & exclusions', exact: true }).click();
  await dialog.getByRole('textbox', { name: 'Review note', exact: true }).fill('Synthetic browser review');
  await dialog.getByRole('button', { name: /Save review/ }).click();
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await go(page, 'transactions', 'Transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill(merchant);
  await page.getByRole('button', { name: `Review ${merchant}`, exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('textbox', { name: 'Review note', exact: true })).toHaveValue('Synthetic browser review');
  const after = (await (await request.get(`/api/transactions/${before.id}/review`)).json()).transaction;
  expect(after.category).toBe('Legal services');
  for (const field of ['description', 'amount', 'txn_date', 'account_id']) expect(after[field]).toBe(before[field]);
});

test('classification inbox expands and exact rules classify matching payments', async ({ page, request }, info) => {
  await go(page, 'merchants', 'People & merchants');
  await page.getByRole('button', { name: /^Show 25 more labels/ }).click();
  await expect(page.getByText(/Showing 33 of/)).toBeVisible();
  const source = `QA Match ${info.project.name}`;
  await page.getByLabel('Search classification inbox', { exact: true }).fill(source);
  await page.getByRole('button', { name: `Classify ${source}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Display name', { exact: true }).fill(`QA Matched ${info.project.name}`);
  await dialog.getByRole('combobox', { name: 'Category', exact: true }).selectOption('Eating out');
  await expect(dialog.getByText('2 matching transactions', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Save exact rule', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const rows = (await (await request.get('/api/transactions', { params: { q: source } })).json()).items;
  expect(rows).toHaveLength(2);
  expect(rows.every((row: any) => row.category === 'Eating out' && row.merchant === `QA Matched ${info.project.name}`)).toBe(true);
});

test('API failure shows an actionable error and retry recovers', async ({ page }) => {
  await page.route('**/api/transactions?**', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic unavailable database' }) }));
  await go(page, 'transactions', 'Transactions');
  await expect(page.getByRole('alert')).toContainText('Synthetic unavailable database');
  // Let initial preference hydration finish while the outage still applies.
  // Otherwise its automatic refetch can recover between unroute and the click.
  await page.waitForLoadState('networkidle');
  await page.unroute('**/api/transactions?**');
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^Review QA/ }).first()).toBeVisible();
});

test('no investment accounts has an honest empty state', async ({ page }) => {
  await go(page, 'investments', 'Investments');
  await expect(page.getByText('No investment accounts are available in the saved portfolio.')).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('planning preserves explicit zero assumptions in a saved scenario', async ({ page }, info) => {
  await go(page, 'planning', 'Planning');
  await revealMobileControls(page, 'Scenario assumptions');
  const wealth = page.getByRole('spinbutton', { name: /^Starting wealth/ });
  const saving = page.getByRole('spinbutton', { name: /^Monthly saving/ });
  await wealth.fill('0');
  await saving.fill('0');
  await revealMobileControls(page, 'Scenario assumptions');
  await page.getByRole('spinbutton', { name: /^Projection years/ }).fill('1');
  await page.getByText('Save or load a scenario', { exact: false }).first().click();
  await page.getByRole('textbox', { name: 'Scenario name', exact: true }).fill(`QA zero ${info.project.name}`);
  await page.getByRole('button', { name: 'Save shown scenario', exact: true }).click();
  await expect(page.getByRole('button', { name: `Load scenario QA zero ${info.project.name}`, exact: true })).toBeVisible();
  await page.reload();
  await page.getByText('Save or load a scenario', { exact: false }).first().click();
  await page.getByRole('button', { name: `Load scenario QA zero ${info.project.name}`, exact: true }).click();
  await revealMobileControls(page, 'Scenario assumptions');
  await expect(wealth).toHaveValue('0');
  await expect(saving).toHaveValue('0');
  await revealMobileControls(page, 'Scenario assumptions');
  await page.getByRole('spinbutton', { name: /^Projection years/ }).fill('0');
  await expect(page.getByRole('button', { name: 'Update saved scenario', exact: true })).toBeDisabled();
  await expect(page.getByText(/Complete valid values to update the chart/)).toBeVisible();
});

test('resolving and reopening a review preserves recorded spending', async ({ page, request }) => {
  const before = await (await request.get('/api/summary')).json();
  await go(page, 'anomalies', 'Review');
  const resolve = page.getByRole('button', { name: /^Resolve / }).first();
  await expect(resolve).toBeVisible();
  const name = (await resolve.getAttribute('aria-label'))!.replace(/^Resolve /, '');
  const resolvedResponse = page.waitForResponse(response => response.request().method() === 'POST' && /\/api\/transactions\/\d+\/review$/.test(new URL(response.url()).pathname));
  await resolve.click();
  const resolved = await resolvedResponse;
  expect(resolved.ok()).toBe(true);
  const reviewedId = Number(new URL(resolved.url()).pathname.split('/')[3]);
  await page.getByRole('button', { name: 'Resolved history', exact: true }).click();
  const reopen = page.getByRole('button', { name: `Reopen ${name}`, exact: true });
  await expect(reopen).toBeVisible();
  expect((await (await request.get('/api/summary')).json()).spending).toBe(before.spending);
  await reopen.click();
  await page.getByRole('button', { name: 'Open reviews', exact: true }).click();
  await expect(page.getByRole('button', { name: `Resolve ${name}`, exact: true }).first()).toBeVisible();
  await expect.poll(async () => (await (await request.get('/api/anomalies?status=open')).json()).some((row: { id: number }) => row.id === reviewedId)).toBe(true);
  expect((await (await request.get('/api/summary')).json()).spending).toBe(before.spending);
});

test('an older failed filter save cannot mark a newer saved choice as unsaved', async ({ page, request }) => {
  let signalFirst!: () => void;
  let releaseFirst!: () => void;
  const firstArrived = new Promise<void>(resolve => { signalFirst = resolve; });
  const firstMayFinish = new Promise<void>(resolve => { releaseFirst = resolve; });
  let saves = 0;
  await page.route('**/api/preferences/spending', async route => {
    if (route.request().method() === 'PUT' && ++saves === 1) {
      signalFirst();
      await firstMayFinish;
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic older save failure' }) });
    } else await route.continue();
  });
  await go(page, 'analytics', 'Spending');
  await page.getByText(/^Filters ·/).click();
  const account = page.getByRole('combobox', { name: 'Spending account', exact: true });
  await account.selectOption('qa-current');
  await firstArrived;
  await account.selectOption('qa-credit');
  releaseFirst();
  await expect.poll(async () => (await (await request.get('/api/preferences/spending')).json()).value.query?.account).toBe('qa-credit');
  await expect(page.getByRole('alert')).toHaveCount(0);
  await page.reload();
  await page.getByText(/^Filters ·/).click();
  await expect(account).toHaveValue('qa-credit');
});
