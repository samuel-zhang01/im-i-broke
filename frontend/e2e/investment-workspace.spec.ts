import { revealMobileControls } from './navigation-helpers';
import { test, expect } from '@playwright/test';

const savedAt = '2026-09-18T10:00:00Z';
const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
const accounts = ['demo-one', 'demo-two'].map((id, index) => ({
  id, name: `Demo ${index + 1}`, institution: 'Demo broker', number_suffix: null, type: 'Invest', currency: 'GBP', total_value: 1000,
  connection_id: `connection-${id}`, is_paper: false, holdings_synced_at: savedAt, activities_synced_at: savedAt, initial_sync_complete: true,
  balances: section([{ currency: 'GBP', cash: 100, buying_power: null }]), positions: section(), orders: section(), history: section(),
  activities: section([{ id: `activity-${id}`, type: index ? 'BUY' : 'DIVIDEND', description: index ? 'Demo purchase' : 'Demo dividend', symbol: 'DEMO', amount: 10, currency: 'GBP', quantity: null, date: '2026-09-16' }]),
}));
const holdings = Array.from({ length: 12 }, (_, index) => ({
  id: `holding-${index}`, account_id: 'demo-one', account_name: 'Demo 1', institution: 'Demo broker', position_id: `position-${index}`,
  instrument_id: `instrument-${index}`, symbol: `DEMO${index}`, name: `Demonstration company ${index}`, asset_type: 'stock', currency: 'GBP',
  quantity: 2, native_price: 20, native_value: 40, native_average_cost: 15, native_unrealized_pnl: 10, value: 40, unrealized_pnl: 10,
  unrealized_pct: 33.3, weight_pct: 2, sector: index === 11 ? 'Healthcare' : 'Technology', market_symbol: `DEMO${index}`, market_currency: 'GBP', market_as_of: savedAt,
  market_status: 'ok', returns: { '1d': 1, '1w': 2, '1m': 3, '3m': 4, ytd: 5, '1y': 6 }, period_return_pct: 3, day_move_estimate: .4, is_cash: false, stale: false,
}));

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown) => route.fulfill({ json: data });
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: accounts.map(account => ({ id: `connected:${account.id}`, default_included: true, archived_by_user: false, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') return json({ configured: true, retrieved_at: savedAt, error: null, accounts, connections: section() });
    if (url.pathname === '/api/investments/analysis') return json({
      retrieved_at: savedAt, market_as_of: savedAt, currency: 'GBP', range: '1m', selected_account_ids: accounts.map(account => account.id), paper: false,
      accounts: [], summary: { value: 2000, cash: 200, unrealized_pnl: 120, day_move_estimate: 4.8, holdings_count: 12, selected_accounts: 2, known_accounts: 2, missing_accounts: 0, valued_holdings: 12, priced_holdings: 12, classified_holdings: 12, partial: false },
      holdings, sectors: ['Technology', 'Healthcare'].map(name => ({ name, value: 40, weight_pct: 2, holdings_count: 1, priced_holdings: 1, day_move_estimate: .4 })), warnings: [], methodology: [],
    });
    if (url.pathname === '/api/investments/analysis/insights') return json({
      as_of: savedAt, retrieved_at: savedAt, currency: 'GBP', selected_account_ids: ['demo-one', 'demo-two'], paper: false,
      summary: { review_count: 1, quality_count: 1, movement_count: 0, context_count: 0, accounts: 2, holdings: 12 },
      items: [{ id: 'healthcare-review', category: 'quality', severity: 'info', code: 'synthetic', title: 'Check healthcare holding', detail: 'Synthetic review in a different sector.',
        action: { label: 'Open healthcare holding', target: 'holding', account_id: 'demo-one', position_id: 'position-11' }, account_ids: ['demo-one'], holding_ids: ['holding-11'], evidence: [] }],
      coverage: { accounts: 2, known_accounts: 2, holdings: 12, usable_cost_holdings: 12, verified_market_holdings: 12, unique_instruments: 12, movement_eligible_instruments: 12, history_accounts: 0, history_interval_days: null, activity_rows: 2, personal_profit_available: false }, methodology: [],
    });
    if (url.pathname === '/api/wealth') return json({ generated_at: savedAt, currency: 'GBP', accounts: [], selected_account_ids: ['connected:demo-one', 'connected:demo-two'], points: [], summary: { selected_accounts: 2 }, warnings: [] });
    if (url.pathname === '/api/investments/analysis/security') return json({
      holding: holdings.find(holding => holding.position_id === url.searchParams.get('position')), currency: 'GBP', range: url.searchParams.get('range'),
      points: [{ date: '2026-09-01', close: 19, return_pct: 0 }, { date: '2026-09-17', close: 20, return_pct: 5.26 }], market_currency: 'GBP', price_basis: 'price_return', source_url: 'https://example.com/demo-market-source', warnings: [],
    });
    return json({ value: {}, revision: 'synthetic' });
  });
  await page.goto('/#connected-portfolio');
  await expect(page.getByRole('heading', { name: /^Holdings performance/ })).toBeVisible();
});


test('holding rows keep identity and amounts readable without extra stacked lines', async ({ page }, info) => {
  const trigger = page.getByRole('button', { name: 'View DEMO0 performance in Demo 1', exact: true }).filter({ visible: true });
  await expect(trigger).toBeVisible();
  if (info.project.name === 'desktop') {
    const row = page.getByRole('region', { name: 'Investment performance by holding', exact: true }).getByRole('row').filter({ has: trigger });
    await expect(row).toContainText('Demonstration company 0');
    await expect(row).toContainText('2 units · Demo 1');
    await expect(row).toContainText('£40.00');
    const rowBox = await row.boundingBox();
    expect(rowBox!.height).toBeLessThanOrEqual(60);
    expect(rowBox!.height).toBeGreaterThanOrEqual(44);
  }
  await trigger.click();
  await expect(page.getByRole('dialog').getByRole('heading', { name: 'Demonstration company 0', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('holding-table-density.png') });
});

test('investment groups preserve holdings pagination and stock-dialog keyboard access', async ({ page }) => {
  await expect(page.getByRole('tablist', { name: 'Investment views' }).getByRole('tab')).toHaveCount(4);
  await expect(page.getByRole('group', { name: 'Portfolio sections' }).getByRole('button')).toHaveText(['Holdings', 'Accounts']);
  await expect(page.getByRole('button', { name: /Saved snapshots|Statement archive/ })).toHaveCount(0);
  const pagination = page.getByRole('navigation', { name: 'Holding pages' });
  await pagination.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(pagination).toContainText('11–12 of 12');
  await page.getByLabel('Search investment performance').fill('DEMO11');
  await expect(pagination).toHaveCount(0);
  const holding = page.getByRole('button', { name: 'View DEMO11 performance in Demo 1', exact: true }).filter({ visible: true });
  await holding.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Quantity / average cost', { exact: true })).toBeVisible();
  await dialog.getByLabel('Security history period').selectOption('3m');
  await expect(dialog.getByRole('link', { name: 'Market data source' })).toBeHidden();
  for (const key of ['Tab', 'Shift+Tab']) {
    for (let index = 0; index < 14; index += 1) {
      await page.keyboard.press(key);
      expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    }
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(holding).toBeFocused();
});

test('dividend focus resets when switching to an account without dividends', async ({ page }) => {
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('button', { name: 'Dividends · 1', exact: true }).click();
  await expect(page.getByText('Demo dividend', { exact: true })).toBeVisible();
  await expect(page.getByText('Demo purchase', { exact: true })).toHaveCount(0);
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption('demo-two');
  await expect(page.getByText('Demo purchase', { exact: true })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Activity focus' })).toHaveCount(0);
});

test('security selection remains usable at intermediate phone widths', async ({ page }) => {
  await page.getByRole('button', { name: 'View DEMO0 performance in Demo 1', exact: true }).filter({ visible: true }).click();
  const dialog = page.getByRole('dialog');
  const select = dialog.getByLabel('Security to chart', { exact: true });
  for (const width of [320, 390, 430]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(select).toBeVisible();
    await expect.poll(async () => (await select.boundingBox())!.width).toBeGreaterThan(120);
    const box = (await select.boundingBox())!;
    const panel = (await dialog.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(panel.x);
    expect(box.x + box.width).toBeLessThanOrEqual(panel.x + panel.width);
  }
  await select.selectOption('holding-11');
  await expect(dialog.getByRole('heading', { name: 'Demonstration company 11', exact: true })).toBeVisible();
});

test('adding a cached benchmark keeps the security histories and avoids duplicate reads', async ({ page }) => {
  let securityReads = 0;
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/investments/analysis/security') securityReads++;
  });
  await page.route('**/api/investments/market/benchmarks?**', route => route.fulfill({ json: { series: [{
    id: 'qa-index', name: 'QA Index', currency: 'GBP', as_of: savedAt, status: 'ok', source_url: null,
    points: [{ date: '2026-09-01', close: 100 }, { date: '2026-09-17', close: 105 }],
  }] } }));
  await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
  await page.getByRole('group', { name: 'Analysis sections' }).getByRole('button', { name: 'Compare', exact: true }).click();
  await expect(page.getByText('Common dates:', { exact: false })).toBeVisible();
  expect(securityReads).toBe(2);
  await page.getByText('Choose securities & benchmarks', { exact: true }).click();
  const benchmark = page.getByRole('checkbox', { name: 'QA Index', exact: true });
  const chartIndex = page.getByRole('term').filter({ hasText: 'QA Index' });
  await benchmark.check();
  await expect(chartIndex).toBeVisible();
  await expect(page.getByText('3 selected', { exact: true })).toBeVisible();
  await benchmark.uncheck();
  await expect(chartIndex).toHaveCount(0);
  await expect(page.getByText('Common dates:', { exact: false })).toBeVisible();
  expect(securityReads).toBe(2);
  const security = page.getByRole('checkbox', { name: /^DEMO1 · Demonstration company 1\b/ });
  await security.uncheck();
  await expect(page.getByText('1 selected', { exact: true })).toBeVisible();
  await expect.poll(() => securityReads).toBe(3);
});


test('review opens the exact holding outside a retained sector filter', async ({ page }) => {
  await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
  await page.getByRole('button', { name: 'View Technology holdings', exact: true }).click();
  await expect(page.getByRole('heading', { name: /^Holdings performance/ })).toContainText('11 positions');
  await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
  await page.getByRole('group', { name: 'Analysis sections' }).getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByRole('button', { name: 'Open healthcare holding', exact: false }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Security to chart')).toHaveValue('holding-11');
  await expect(dialog.getByRole('heading', { name: 'Demonstration company 11', exact: true })).toBeVisible();
});

test('investment history exposes its dated coverage and requests exact ranges with fresh zoom', async ({ page }) => {
  await page.route('**/api/wealth?**', route => {
    const url = new URL(route.request().url());
    const selected = url.searchParams.getAll('account');
    const points = ['2026-09-12', '2026-09-15', '2026-09-17'].map((date, index) => ({
      date, net_worth: 1000 + index * 125, assets: 1000 + index * 125, liabilities: 0,
      known_accounts: selected.length, missing_accounts: 0, estimated: true, carried_accounts: 0,
      complete: true, coverage_key: selected.join('|'), valuation_key: `daily:${selected.join('|')}`,
      valuation_basis: 'daily_holdings', reconstructed_accounts: selected.length,
    }));
    return route.fulfill({ json: {
      generated_at: savedAt, currency: 'GBP', currencies: ['GBP'], range: url.searchParams.get('range'),
      selected_account_ids: selected, accounts: [], summary: { net_worth: 2000, selected_accounts: selected.length },
      history: { mode: 'comparable_cohort', account_ids: selected, excluded_account_ids: [],
        account_count: selected.length, selected_accounts: selected.length, current_value: 2000,
        start_date: points[0].date, end_date: points[2].date, end_value: 1250, change: 250, change_pct: 25,
        change_comparable: true, points, coverage_note: 'Daily values reconstructed using dated holdings and FX.' },
      points, warnings: [], methodology: [], cache: { saved_at: savedAt, stale: false, refresh_error: null },
    } });
  });
  await page.reload();
  const readout = page.getByLabel('Inspected portfolio values', { exact: true });
  const chart = page.getByRole('group', { name: 'Interactive account value chart', exact: true });
  await expect(readout).toContainText('£1,250.00');
  await page.getByRole('button', { name: 'Zoom in on comparison', exact: true }).click();
  await chart.focus(); await chart.press('Home');
  await expect(readout).not.toContainText('12 Sept 2026');
  const weeklyRequest = page.waitForRequest(request => request.url().includes('/api/wealth?') && new URL(request.url()).searchParams.get('range') === '1w');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('1w');
  await weeklyRequest;
  await expect(readout).toContainText('£1,250.00');
  await chart.focus(); await chart.press('Home');
  await expect(readout).toContainText('12 Sept 2026');
  await page.getByRole('button', { name: 'Zoom in on comparison', exact: true }).click();
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption('demo-two');
  await expect(readout).toContainText('£1,250.00');
  await chart.focus(); await chart.press('Home');
  await expect(readout).toContainText('12 Sept 2026');
  const yearlyRequest = page.waitForRequest(request => request.url().includes('/api/wealth?') && new URL(request.url()).searchParams.get('range') === 'ytd');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('ytd');
  await yearlyRequest;
  await expect(readout).toContainText('£1,250.00');
  await page.getByText('Dated values & sources', { exact: true }).click();
  await expect(page.getByLabel('Historical account coverage')).toContainText('1 / 1 selected accounts in this history');
  await expect(page.getByLabel('Portfolio comparison observations').getByRole('row')).toHaveCount(4);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('investment account and view controls are recalled while explicit account links take priority', async ({ page }) => {
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByRole('combobox', { name: 'Account selection' }).selectOption('demo-two');
  await page.getByRole('combobox', { name: 'Investment price period' }).selectOption('3m');
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Account activity', exact: true })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page: 'settings' } })));
  await expect(page.getByRole('heading', { level: 1, name: 'Settings', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('tab', { name: 'Activity', exact: true })).toHaveAttribute('aria-selected', 'true');
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByRole('combobox', { name: 'Account selection' })).toHaveValue('demo-two');
  await page.getByRole('tab', { name: 'Portfolio', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Investment price period' })).toHaveValue('3m');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page: 'connected-portfolio', accountId: 'demo-one' } })));
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByRole('combobox', { name: 'Account selection' })).toHaveValue('demo-one');
  await expect(page.getByRole('tab', { name: 'Portfolio', exact: true })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('combobox', { name: 'Account selection' }).selectOption('demo-two');
  // Following the same destination again must still override a manual change.
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page: 'connected-portfolio', accountId: 'demo-one' } })));
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByRole('combobox', { name: 'Account selection' })).toHaveValue('demo-one');
});

test('missing previous closes explain a partial session move while account value stays complete', async ({ page }) => {
  await page.route('**/api/investments/analysis?**', route => route.fulfill({ json: {
    retrieved_at: savedAt, market_as_of: savedAt, currency: 'GBP', range: '1m', selected_account_ids: accounts.map(account => account.id), paper: false,
    accounts: [], summary: { value: 2000, cash: 200, unrealized_pnl: 120, day_move_estimate: 4.4, holdings_count: 12, selected_accounts: 2, known_accounts: 2, missing_accounts: 0, valued_holdings: 12, priced_holdings: 11, classified_holdings: 12, partial: false },
    holdings: holdings.map((holding, index) => index ? holding : { ...holding, day_move_estimate: null, day_move_unavailable_reason: 'missing_previous_session_close', day_move_missing_date: '2026-09-17' }),
    sectors: [], warnings: [], methodology: [],
  } }));
  await page.reload();
  const coverage = page.getByRole('status').filter({ hasText: 'Session move is a subtotal' });
  await expect(coverage).toContainText('11 of 12 positions');
  await expect(coverage).toContainText('2026-09-17');
  await expect(page.getByText('£2,000.00', { exact: true }).first()).toBeVisible();
  await coverage.getByRole('button', { name: 'View excluded positions' }).click();
  await expect(page.locator('#investment-analysis-notes')).toHaveAttribute('open', '');
  await expect(page.locator('#investment-analysis-notes')).toContainText('Previous session close unavailable');
});
