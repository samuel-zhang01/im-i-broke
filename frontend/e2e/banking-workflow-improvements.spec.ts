import { expect, test, type Page } from '@playwright/test';
import { navigateFinance, revealMobileControls } from './navigation-helpers';

async function seed(page: Page, { reviews = 0, delayHistory = false } = {}) {
  const stamp = '2026-09-21T10:00:00Z';
  const account = {
    id: 'qa-bank-workflow', account_id: 'qa-current', institution_name: 'QA Bank', name: 'QA everyday', currency: 'GBP',
    status: 'active', excluded: false, present: true, mapping_status: 'mapped', mapping_candidates: [], last_error: null,
    last_synced_at: stamp, balance: { amount: 1234.56, currency: 'GBP', fetched_at: stamp },
    counts: { cached: 63, imported: 30, matched: 0, pending: 2, review: reviews },
  };
  let pending = Array.from({ length: reviews }, (_, index) => ({
    id: index + 1, provider_id: `overlap-${index + 1}`, date: '2026-09-21', amount: -12, currency: 'GBP',
    merchant: `QA overlap ${index + 1}`, description: 'Bank merchant wording', is_pending: false, status: 'review',
    transaction_id: null, review_reason: 'Compare the saved statement before choosing whether this is the same payment.',
    candidates: [{ id: 9000 + index, txn_date: '2026-09-20', merchant: 'QA saved purchase', description: 'Original saved statement wording', amount: -12, category: 'Food' }],
  }));
  const mutations: unknown[] = [], offsets: number[] = [];
  let releaseHistory!: () => void;
  const history = new Promise<void>(resolve => { releaseHistory = resolve; });
  let holdSearch = false, releaseSearch!: () => void;
  const search = new Promise<void>(resolve => { releaseSearch = resolve; });
  const base = () => ({
    configured: true, accounts: [{ ...account, counts: { ...account.counts, review: pending.length } }],
    local_accounts: [{ id: 'qa-current', bank: 'QA Bank', display_name: 'QA everyday', account_type: 'current', currency: 'GBP', connected: true, balance: 1234.56, n_txns: 30 }],
    institutions: [], totals_by_currency: { GBP: 1234.56 },
    sync: { last_attempt_at: stamp, last_success_at: stamp, running: false, interval_seconds: 3600, error: null },
    stats: { accounts: 1, mapped: 1, cached: 63, imported: 30, review: pending.length, pending: 2 },
  });
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await page.route('**/api/revision', route => route.fulfill({ json: { revision: 'bank-workflow-fixture' } }));
  await page.route('**/api/banking**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.includes('/reviews/')) {
      mutations.push(route.request().postDataJSON());
      const id = Number(url.pathname.split('/').at(-1));
      pending = pending.filter(item => item.id !== id);
      return route.fulfill({ json: { ok: true } });
    }
    if (url.pathname === '/api/banking') return route.fulfill({ json: base() });
    const review = url.searchParams.get('status') === 'review';
    const offset = Number(url.searchParams.get('offset') || 0);
    const limit = Number(url.searchParams.get('limit') || 30);
    if (review) offsets.push(offset);
    if (limit === 1 && delayHistory) await history;
    return route.fulfill({ json: {
      account, total: review ? pending.length : 0, transactions: [], reviews: review ? pending.slice(offset, offset + limit) : [],
      balance_history: [{ amount: 1200, currency: 'GBP', fetched_at: '2026-09-20T10:00:00Z' }, account.balance], totals_by_currency: {},
    } });
  });
  await page.route('**/api/transactions?**', async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('q') && holdSearch) await search;
    const items = Array.from({ length: url.searchParams.get('q') ? 1 : 5 }, (_, index) => ({
      id: index + 1, account_id: 'qa-current', account: 'QA everyday', txn_date: '2026-09-21', merchant: `QA saved activity ${index + 1}`,
      description: 'Saved statement activity', amount: -12, category: 'Food', transfer_group: null,
    }));
    return route.fulfill({ json: { items, total: items.length, offset: 0, limit: 30, sum_in: 0, sum_out: -60 } });
  });
  await page.goto('/#banking');
  await page.getByRole('button', { name: 'Open QA everyday, GBP', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Account details', exact: true })).toBeVisible();
  return { mutations, offsets, releaseHistory, releaseSearch, holdSearch: () => { holdSearch = true; } };
}

const documentY = (locator: ReturnType<Page['locator']>) => locator.evaluate(element => element.getBoundingClientRect().top + window.scrollY);

test('account activity precedes connection details and slow balance history never flashes empty', async ({ page }, info) => {
  const fixture = await seed(page, { delayHistory: true });
  try {
    await expect(page.getByText('QA saved activity 1', { exact: true })).toBeVisible();
    await expect(page.getByRole('status', { name: 'Loading saved balance observations…', exact: true })).toBeVisible();
    await expect(page.getByText('Your balance timeline starts here', { exact: true })).toHaveCount(0);
    const activity = page.getByLabel('Account activity', { exact: true });
    const connection = page.getByLabel('Connection & history', { exact: true });
    expect(await documentY(activity)).toBeLessThan(await documentY(connection));
    const before = await documentY(activity);
    fixture.releaseHistory();
    await expect(page.getByRole('img', { name: /^Saved GBP balances/ })).toBeVisible();
    const scale = page.locator('figure [aria-hidden="true"] span');
    await expect(scale).toHaveCount(2);
    expect(await scale.first().textContent()).not.toBe(await scale.last().textContent());
    expect(Math.abs(await documentY(activity) - before)).toBeLessThanOrEqual(1);
    if (info.project.name === 'mobile') {
      await expect(page.getByLabel('From', { exact: true })).not.toBeVisible();
      await expect(page.getByRole('button', { name: 'Activity filters', exact: true })).toHaveAttribute('aria-expanded', 'false');
    }
    await page.getByRole('button', { name: 'Bank feed', exact: true }).click();
    await revealMobileControls(page, 'Activity filters');
    await page.getByRole('combobox', { name: 'Settlement', exact: true }).selectOption('pending');
    await page.getByLabel('From', { exact: true }).fill('2026-09-01');
    await expect(page.getByRole('button', { name: 'Clear filters', exact: true })).toBeVisible();
    if (info.project.name === 'mobile') {
      await page.getByRole('button', { name: 'Activity filters', exact: true }).click();
      await expect(page.getByRole('combobox', { name: 'Settlement', exact: true })).not.toBeVisible();
      await expect(page.getByRole('button', { name: 'Activity filters', exact: true })).toContainText('Pending only');
      await expect(page.getByRole('searchbox', { name: 'Search activity', exact: true })).toBeVisible();
    }
    await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
    await revealMobileControls(page, 'Activity filters');
    await expect(page.getByRole('combobox', { name: 'Settlement', exact: true })).toHaveValue('all');
    await expect(page.getByLabel('From', { exact: true })).toHaveValue('');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `../output/playwright/bank-workflow-${info.project.name}.png`, fullPage: true });
  } finally { fixture.releaseHistory(); }
});

test('resolving the final overlap on a later page opens the remaining queue and focuses it', async ({ page }, info) => {
  if (info.project.name === 'mobile') await page.setViewportSize({ width: 320, height: 844 });
  const fixture = await seed(page, { reviews: 31 });
  await page.getByRole('button', { name: 'Review (31)', exact: true }).click();
  const pager = page.getByRole('navigation', { name: 'Account activity pages', exact: true });
  await expect(pager).toContainText('1–30 of 31 reviews');
  await expect(page.getByRole('button', { name: 'Match existing', exact: true })).toHaveCount(1);
  await pager.getByRole('button', { name: 'Next →', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'QA overlap 31', exact: true })).toBeFocused();
  await expect(pager).toContainText('31–31 of 31 reviews');
  const selector = await page.getByRole('combobox', { name: 'Review item on this page', exact: true }).boundingBox();
  expect(selector!.width).toBeGreaterThanOrEqual(100);
  const match = page.getByRole('button', { name: 'Match existing', exact: true });
  await expect(match).toBeDisabled();
  await page.getByRole('combobox', { name: 'Existing transaction', exact: true }).selectOption('9030');
  const bank = page.getByRole('region', { name: 'Bank transaction', exact: true });
  const saved = page.getByRole('region', { name: 'Saved transaction comparison', exact: true });
  await expect(saved).toContainText('Original saved statement wording');
  await expect(saved).toContainText('Same amount · Different date');
  const bankBox = (await bank.boundingBox())!, savedBox = (await saved.boundingBox())!;
  if (info.project.name === 'mobile') expect(savedBox.y).toBeGreaterThan(bankBox.y + bankBox.height);
  else expect(Math.abs(savedBox.y - bankBox.y)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `../output/playwright/bank-review-comparison-${info.project.name}.png`, fullPage: true });
  await match.click();
  await expect.poll(() => fixture.mutations.length).toBe(1);
  expect(fixture.mutations[0]).toEqual({ action: 'match', transaction_id: 9030 });
  await expect(pager).toContainText('1–30 of 30 reviews');
  await expect(page.getByRole('heading', { name: 'QA overlap 1', exact: true })).toBeFocused();
  await expect(page.getByText('No overlaps to review for these filters.', { exact: true })).toHaveCount(0);
  expect(fixture.offsets.slice(-2)).toEqual([30, 0]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('delayed account searches keep rows steady and account controls survive navigation', async ({ page }) => {
  const fixture = await seed(page);
  const search = page.getByRole('searchbox', { name: 'Search activity', exact: true });
  const first = page.getByText('QA saved activity 1', { exact: true });
  await expect(first).toBeVisible();
  // First activate the compact clear-filter strip, then hold the next request.
  await search.fill('saved');
  await expect(page.getByText('QA saved activity 2', { exact: true })).toHaveCount(0);
  const before = await documentY(first);
  fixture.holdSearch();
  try {
    await search.fill('saved activity');
    await expect(page.getByLabel('Account activity results', { exact: true })).toHaveAttribute('aria-busy', 'true');
    expect(Math.abs(await documentY(first) - before)).toBeLessThanOrEqual(1);
    await expect(search).toBeFocused();
    fixture.releaseSearch();
    await expect(page.getByLabel('Account activity results', { exact: true })).toHaveAttribute('aria-busy', 'false');
    await navigateFinance(page, 'transactions', 'Transactions');
    await navigateFinance(page, 'banking', 'Banks & accounts');
    await expect(page.getByRole('heading', { name: 'QA everyday', exact: true })).toBeVisible();
    await expect(page.getByRole('searchbox', { name: 'Search activity', exact: true })).toHaveValue('saved activity');
    await expect(page.getByRole('button', { name: 'Complete ledger', exact: true })).toHaveAttribute('aria-pressed', 'true');
  } finally { fixture.releaseSearch(); }
});
