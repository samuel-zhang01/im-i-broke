import { expect, test } from '@playwright/test';
import { revealMobileControls } from './navigation-helpers';

function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

test('shared async refresh keeps the overview heading in place', async ({ page }) => {
  const pending = gate();
  let revision = 'loading-baseline', reads = 0, revisionReads = 0;
  await page.route('**/api/revision', route => { revisionReads++; return route.fulfill({ json: { revision } }); });
  await page.route('**/api/summary', async route => { reads++; if (reads > 1) await pending.promise; await route.continue(); });
  await page.goto('/#overview');
  const heading = page.getByRole('heading', { name: 'The latest picture', exact: true });
  await expect(heading).toBeVisible();
  await expect.poll(() => revisionReads).toBeGreaterThan(0);
  const y = () => heading.evaluate(element => element.getBoundingClientRect().top + scrollY);
  const before = await y();
  revision = 'loading-updated';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => reads).toBeGreaterThan(1);
  expect(Math.abs(await y() - before)).toBeLessThanOrEqual(1);
  pending.release();
  await expect(page.getByRole('status').filter({ hasText: 'Updating results…' })).toHaveCount(0);
  expect(Math.abs(await y() - before)).toBeLessThanOrEqual(1);
});

test('retained transaction rows do not move while filters refresh', async ({ page }, info) => {
  const pending = gate();
  let waiting = false;
  await page.route('**/api/transactions?**', async route => {
    if (new URL(route.request().url()).searchParams.get('q') === 'QA') {
      waiting = true;
      await pending.promise;
    }
    await route.continue();
  });
  await page.goto('/#transactions');
  const row = page.getByRole('button', { name: /^Review QA/ }).first();
  await expect(row).toBeVisible();
  const y = () => row.evaluate(element => element.getBoundingClientRect().top + scrollY);
  const unfiltered = await y();
  await page.getByLabel('Search transactions', { exact: true }).fill('Q');
  await page.waitForResponse(response => new URL(response.url()).pathname === '/api/transactions' && new URL(response.url()).searchParams.get('q') === 'Q');
  await expect(page.getByRole('status').filter({ hasText: 'Updating results…' })).toHaveCount(0);
  expect(Math.abs(await y() - unfiltered)).toBeLessThanOrEqual(1);
  const before = await y();
  await page.getByLabel('Search transactions', { exact: true }).fill('QA');
  await expect.poll(() => waiting).toBe(true);
  await expect(page.getByRole('status').filter({ hasText: 'Updating results…' })).toBeAttached();
  expect(Math.abs(await y() - before)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('retained-transactions.png') });
  pending.release();
  await expect(page.getByRole('status').filter({ hasText: 'Updating results…' })).toHaveCount(0);
  expect(Math.abs(await y() - before)).toBeLessThanOrEqual(1);
});

test('bank loading preserves the first content position and respects reduced motion', async ({ page }, info) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript(() => {
    const shifts: number[] = [];
    (window as Window & { loadingShifts?: number[] }).loadingShifts = shifts;
    if (PerformanceObserver.supportedEntryTypes.includes('layout-shift')) new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        const shift = entry as PerformanceEntry & { value: number; hadRecentInput: boolean };
        if (!shift.hadRecentInput) shifts.push(shift.value);
      }
    }).observe({ type: 'layout-shift', buffered: true });
  });
  const pending = gate();
  const counts = { cached: 1, imported: 1, matched: 0, pending: 0, review: 0 };
  const savedAt = '2026-09-21T10:00:00Z';
  const bank = { id: 'loading-bank', account_id: 'qa-current', name: 'Current account', institution_name: 'Demo Bank', currency: 'GBP', status: 'active', excluded: false, present: true, mapping_status: 'mapped', mapping_candidates: [], last_synced_at: savedAt, last_error: null, counts, balance: { amount: 1000, currency: 'GBP', fetched_at: savedAt } };
  await page.route('**/api/banking', async route => {
    await pending.promise;
    await route.fulfill({ json: { configured: true, accounts: [bank], local_accounts: [{ id: 'qa-current', bank: 'Demo Bank', display_name: 'Current account', account_type: 'current', currency: 'GBP', connected: true, balance: 1000, n_txns: 1 }], institutions: [], totals_by_currency: { GBP: 1000 }, sync: { last_attempt_at: savedAt, last_success_at: savedAt, error: null, running: false, interval_seconds: 3600 }, stats: { accounts: 1, mapped: 1, ...counts } } });
  });
  await page.goto('/#banking');
  const skeleton = page.getByRole('status', { name: 'Opening your banks and saved history…', exact: true });
  await expect(skeleton).toBeVisible();
  await expect(skeleton.locator('.skeleton-block').first()).toBeVisible();
  const y = await skeleton.locator('.skeleton-block').first().evaluate(element => element.getBoundingClientRect().top + scrollY);
  expect(await skeleton.locator('.skeleton-block').first().evaluate(element => getComputedStyle(element).animationName)).toBe('none');
  await page.screenshot({ path: info.outputPath('bank-loading.png') });
  await page.evaluate(() => { const shifts = (window as Window & { loadingShifts?: number[] }).loadingShifts; if (shifts) shifts.length = 0; });
  pending.release();
  await expect(page.getByLabel('Find bank or account', { exact: true })).toBeVisible();
  await expect(skeleton).toHaveCount(0);
  const headingY = await page.getByRole('heading', { name: 'Your banks', exact: true }).evaluate(element => element.getBoundingClientRect().top + scrollY);
  expect(Math.abs(headingY - y)).toBeLessThanOrEqual(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('bank-loaded.png') });
  const shifts = await page.evaluate(() => (window as Window & { loadingShifts?: number[] }).loadingShifts || []);
  await info.attach('bank-loading-geometry.json', { body: JSON.stringify({ firstContentDelta: headingY - y, cls: shifts.reduce((sum, value) => sum + value, 0), shifts }), contentType: 'application/json' });
});

test('portfolio refresh avoids duplicate analysis requests and retains the history chart', async ({ page }, info) => {
  const savedAt = '2026-09-21T10:00:00Z';
  const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
  const account = { id: 'loading-demo', name: 'Loading demo account', institution: 'Demo broker', currency: 'GBP', total_value: 1000,
    connection_id: 'demo-connection', is_paper: false, holdings_synced_at: savedAt, initial_sync_complete: true,
    balances: section(), positions: section(), orders: section(), history: section(), activities: section() };
  const portfolio = { configured: true, retrieved_at: savedAt, accounts: [account], connections: section(), error: null,
    refresh_schedule: { enabled: true, interval_seconds: 3600 }, cache: { last_successful_refresh_at: savedAt, stale: false } };
  const refresh = gate(), historyRefresh = gate(), initialAnalysis = gate();
  let analyses = 0, histories = 0, refreshing = false;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/investments/connected/refresh') { refreshing = true; await refresh.promise; return route.fulfill({ json: portfolio }); }
    if (url.pathname === '/api/investments/connected') return route.fulfill({ json: portfolio });
    if (url.pathname === '/api/wealth/sources') return route.fulfill({ json: { sources: [{ id: 'connected:loading-demo', default_included: true }] } });
    if (url.pathname === '/api/investments/analysis') {
      analyses++;
      if (analyses === 1) await initialAnalysis.promise;
      return route.fulfill({ json: { retrieved_at: savedAt, market_as_of: savedAt, currency: 'GBP', range: '1m', selected_account_ids: [account.id], paper: false,
        accounts: [], summary: { value: 1000, cash: 100, unrealized_pnl: 10, day_move_estimate: 1, holdings_count: 0, selected_accounts: 1, known_accounts: 1, missing_accounts: 0, valued_holdings: 0, priced_holdings: 0, classified_holdings: 0, partial: false }, holdings: [], sectors: [], warnings: [], methodology: [] } });
    }
    if (url.pathname === '/api/wealth') {
      histories++;
      if (histories > 1) await historyRefresh.promise;
      const points = ['2026-09-19', '2026-09-20', '2026-09-21'].map((date, index) => ({ date, net_worth: 900 + index * 50, assets: 900 + index * 50, liabilities: 0, known_accounts: 1, missing_accounts: 0, complete: true, estimated: false, carried_accounts: 0, coverage_key: 'demo', valuation_key: 'demo', valuation_basis: 'recorded_balance' }));
      return route.fulfill({ json: { generated_at: savedAt, currency: 'GBP', range: '1m', selected_account_ids: ['connected:loading-demo'], accounts: [], summary: { selected_accounts: 1, net_worth: 1000 }, points,
        history: { mode: 'comparable_cohort', account_ids: ['connected:loading-demo'], excluded_account_ids: [], account_count: 1, selected_accounts: 1, current_value: 1000, start_date: points[0].date, end_date: points[2].date, end_value: 1000, change: 100, change_pct: 11.1, change_comparable: true, points, coverage_note: 'Synthetic recorded values' }, warnings: [], methodology: [], cache: { saved_at: savedAt, stale: false, refresh_error: null } } });
    }
    if (url.pathname === '/api/wealth/refresh' || url.pathname === '/api/investments/market/refresh') return route.fulfill({ json: { failed: 0, updated: 0, attempted: 0, missing: 0 } });
    return route.continue();
  });
  await page.goto('/#connected-portfolio');
  const placeholder = page.getByRole('status', { name: 'Loading investment performance…', exact: true });
  await expect(placeholder).toBeVisible();
  const summaryHeight = await placeholder.locator('.rounded-xl').first().evaluate(element => element.getBoundingClientRect().height);
  await page.screenshot({ path: info.outputPath('portfolio-loading.png') });
  initialAnalysis.release();
  const chart = page.getByRole('group', { name: 'Interactive account value chart', exact: true });
  await expect(chart).toBeVisible();
  const loadedSummary = page.getByText('Account value · GBP', { exact: true }).locator('xpath=ancestor::div[@aria-busy][1]');
  expect(Math.abs(await loadedSummary.evaluate(element => element.getBoundingClientRect().height) - summaryHeight)).toBeLessThanOrEqual(10);
  const initialCount = analyses;
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByRole('button', { name: '↻ Refresh', exact: true }).click();
  await expect.poll(() => refreshing).toBe(true);
  await page.waitForTimeout(200);
  expect(analyses).toBe(initialCount);
  await expect(chart).toBeVisible();
  refresh.release();
  await expect.poll(() => analyses).toBe(initialCount + 1);
  await expect.poll(() => histories).toBe(2);
  await expect(chart).toBeVisible();
  await expect(page.getByRole('status', { name: 'Loading account value history…', exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('retained-portfolio-chart.png') });
  historyRefresh.release();
  await expect(chart).toBeVisible();
});
