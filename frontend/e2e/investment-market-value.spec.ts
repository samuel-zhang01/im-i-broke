import { revealMobileControls } from './navigation-helpers';
import { expect, test } from '@playwright/test';

const savedAt = '2026-09-18T15:00:00Z';
const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
const accounts = ['demo-one', 'demo-two'].map(id => ({ id, name: id, institution: 'Demo', currency: id === 'demo-two' ? 'USD' : 'GBP', total_value: 1000,
  connection_id: `connection-${id}`, is_paper: false, holdings_synced_at: savedAt, balances: section(), positions: section(), activities: section(), history: section(), orders: section() }));
const holdings = [{ id: 'demo-one/p', account_id: 'demo-one', account_name: 'Demo', institution: 'Demo', position_id: 'p', instrument_id: 'i', symbol: 'DEMO', name: 'Synthetic holding', asset_type: 'stock', currency: 'GBP', quantity: 1, native_price: 100, native_value: 100, native_average_cost: 90, native_unrealized_pnl: 10, value: 100, unrealized_pnl: 10, unrealized_pct: 11.1, weight_pct: 10, sector: 'Technology', market_currency: 'GBP', market_as_of: savedAt, market_status: 'ready', returns: { '1d': 1 }, period_return_pct: 1, day_move_estimate: 1, is_cash: false, stale: false }];
const points = [100, 0, null, 110, 120, 125].map((value, index) => ({ timestamp: `2026-09-18T${10 + index}:00:00Z`, value, priced_holdings: value === null ? 1 : 2, holdings_count: 2,
  known_accounts: 1, missing_accounts: 1, oldest_price_at: '2026-09-17T20:00:00Z', estimated: true, coverage_key: index < 4 ? 'first-cohort' : 'second-cohort' }));

async function fixtures(page: import('@playwright/test').Page, marketRequests: URL[], writes: string[]) {
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown) => route.fulfill({ json: data });
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: accounts.map(account => ({ id: `connected:${account.id}`, default_included: true, archived_by_user: false, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') return json({ configured: true, retrieved_at: savedAt, error: null, accounts, connections: section() });
    if (url.pathname === '/api/investments/analysis') return json({ retrieved_at: savedAt, market_as_of: savedAt, currency: url.searchParams.get('currency'), range: '1m', selected_account_ids: url.searchParams.getAll('account'), paper: false,
      accounts: [], summary: { value: 2000, cash: 100, unrealized_pnl: 10, day_move_estimate: 1, holdings_count: 1, selected_accounts: 2, known_accounts: 2, missing_accounts: 0, valued_holdings: 1, priced_holdings: 1, classified_holdings: 1, partial: false }, holdings, sectors: [], warnings: [], methodology: [] });
    if (url.pathname === '/api/wealth') {
      const selected = url.searchParams.getAll('account');
      const points = ['2026-09-12', '2026-09-18'].map((date, index) => ({ date, net_worth: 1000 + index * 1000, complete: true, known_accounts: selected.length, missing_accounts: 0, estimated: false, carried_accounts: 0, coverage_key: selected.join('|'), valuation_key: 'recorded', valuation_basis: 'recorded_balance' }));
      return json({ generated_at: savedAt, currency: url.searchParams.get('currency'), accounts: [], selected_account_ids: selected, points, summary: { net_worth: 2000, selected_accounts: selected.length }, warnings: [] });
    }
    if (url.pathname === '/api/investments/market/value') {
      marketRequests.push(url);
      const empty = url.searchParams.get('range') === '1m';
      const advancing = marketRequests.length > 1;
      const generatedAt = advancing ? '2026-09-18T16:00:00Z' : savedAt;
      const observed = advancing ? points.map((point, index) => index === points.length - 1 ? { ...point, timestamp: generatedAt } : point) : points;
      return json({ generated_at: generatedAt, currency: url.searchParams.get('currency'), range: url.searchParams.get('range'), interval: url.searchParams.get('interval'), basis: 'current_holdings', holdings_as_of: savedAt, market_as_of: savedAt,
        selected_account_ids: url.searchParams.getAll('account'), status: empty ? 'unavailable' : 'partial',
        summary: { value: empty ? null : 125, change: null, change_pct: null, selected_accounts: 2, known_accounts: 1, positions: 2, priced_positions: empty ? 0 : 2, partial: true },
        points: empty ? [] : observed, warnings: ['One selected account is unavailable.'], methodology: ['Prices are delayed cached observations of current holdings.'] });
    }
    return json({ value: {}, revision: 'synthetic' });
  });
}

test('an unavailable price overlay preserves account history and its inspection controls', async ({ page }, testInfo) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await fixtures(page, requests, writes);
  await page.goto('/#connected-portfolio');
  const readout = page.getByLabel('Inspected portfolio values', { exact: true });
  await expect(readout).toContainText('£2,000.00');
  expect(requests).toHaveLength(0);
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(page.getByTestId('combined-value-view')).toContainText('No recent prices available for these accounts.');
  await expect(readout).toContainText('£2,000.00');
  expect(requests.at(-1)?.searchParams.get('range')).toBe('1m');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('1w');
  await expect(readout).toContainText('£125.00');
  expect(requests.at(-1)?.searchParams.get('range')).toBe('1w');
  expect(requests.at(-1)?.searchParams.get('interval')).toBe('1h');
  const chart = page.getByRole('group', { name: 'Interactive account value chart', exact: true });
  await chart.focus(); await chart.press('Home');
  await expect(readout).toContainText('£1,000.00');
  await chart.press('End'); await expect(readout).toContainText('£125.00');
  await page.getByRole('checkbox', { name: 'Show recent prices' }).uncheck();
  await expect(readout).toContainText('£2,000.00');
  await expect(page.getByRole('heading', { name: /Asset history/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath('optional-price-overlay.png'), fullPage: false });
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('recent prices polls cached GET only while the overlay and document are visible', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [];
  await fixtures(page, requests, writes);
  await page.clock.install({ time: new Date(savedAt) });
  await page.goto('/#connected-portfolio');
  await expect(page.getByLabel('Inspected portfolio values', { exact: true })).toContainText('£2,000.00');
  await page.clock.fastForward(60_001);
  expect(requests).toHaveLength(0);
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('1w');
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(page.getByLabel('Inspected portfolio values', { exact: true })).toContainText('£125.00');
  const initial = requests.length;
  await page.clock.fastForward(60_001);
  await expect.poll(() => requests.length).toBe(initial + 1);
  await page.getByRole('group', { name: 'Interactive account value chart', exact: true }).focus();
  await page.keyboard.press('End');
  await expect(page.getByLabel('Inspected portfolio values', { exact: true })).toContainText('16:00 UTC');
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }); document.dispatchEvent(new Event('visibilitychange')); });
  await page.clock.fastForward(60_001);
  expect(requests).toHaveLength(initial + 1);
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect.poll(() => requests.length).toBe(initial + 2);
  await page.getByRole('checkbox', { name: 'Show recent prices' }).uncheck();
  await expect(page.getByLabel('Inspected portfolio values', { exact: true })).toContainText('£2,000.00');
  const stopped = requests.length;
  await page.clock.fastForward(120_001);
  expect(requests).toHaveLength(stopped);
  expect(writes).toEqual([]);
});
