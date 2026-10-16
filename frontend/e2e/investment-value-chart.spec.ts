import { revealMobileControls } from './navigation-helpers';
import { expect, test, type Page } from '@playwright/test';

test.use({ timezoneId: 'Europe/London' });

const savedAt = '2026-09-18T15:00:00Z';
const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
const accountRows = [
  { id: 'demo-one', name: 'Demo 1', value: 1000, currency: 'GBP', archived: false },
  { id: 'demo-two', name: 'Demo 2', value: 1000, currency: 'USD', archived: false },
  { id: 'zero-one', name: 'Zero balance A', value: 0, currency: 'GBP', archived: true },
  { id: 'zero-two', name: 'Zero balance B', value: 0, currency: 'GBP', archived: true },
  { id: 'unknown-one', name: 'Unknown balance', value: null, currency: 'GBP', archived: true },
];
const marketPoints = [100, 0, null, 110, 120, 125].map((value, index) => ({ timestamp: `2026-09-18T${10 + index}:00:00Z`, value,
  priced_holdings: value === null ? 1 : 2, holdings_count: 2, known_accounts: 2, missing_accounts: 0,
  oldest_price_at: '2026-09-18T10:00:00Z', sessions: ['regular', 'pre-market'], estimated: true, coverage_key: index < 4 ? 'first-cohort' : 'second-cohort' }));

type FixtureOptions = { unavailable?: 'history' | 'prices'; historyAccountIds?: string[]; historyGap?: boolean;
  state?: { retrievedAt: string; value: number; connectedReads: number; analysisReads: number; failConnected?: boolean; failHistory?: boolean } };
async function fixtures(page: Page, requests: URL[], writes: string[], options: FixtureOptions = {}) {
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown) => route.fulfill({ json: data });
    const observedAt = options.state?.retrievedAt ?? savedAt;
    const totalValue = options.state?.value ?? 2000;
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: accountRows.map(row => ({ id: `connected:${row.id}`, default_included: !row.archived, archived_by_user: row.archived, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') {
      if (options.state) options.state.connectedReads += 1;
      if (options.state?.failConnected) return json({ configured: true, retrieved_at: observedAt, error: 'Synthetic saved portfolio outage', accounts: [], connections: section() });
      return json({ configured: true, retrieved_at: observedAt, error: null,
        accounts: accountRows.map(row => ({ id: row.id, name: row.name, institution: 'Demo broker', currency: row.currency, total_value: row.archived ? row.value : totalValue / 2,
          connection_id: `connection-${row.id}`, is_paper: false, total_value_as_of: observedAt, holdings_synced_at: observedAt, balances: section(), positions: section(), activities: section(), history: section(), orders: section() })), connections: section() });
    }
    if (url.pathname === '/api/investments/analysis') {
      requests.push(url);
      if (options.state) options.state.analysisReads += 1;
      const selected = url.searchParams.getAll('account');
      return json({ retrieved_at: observedAt, market_as_of: observedAt, balance_as_of: observedAt, holdings_as_of: observedAt, currency: url.searchParams.get('currency'), range: url.searchParams.get('range'), selected_account_ids: selected, paper: false,
        accounts: [], summary: { value: selected.length * totalValue / 2, cash: 100, unrealized_pnl: 10, day_move_estimate: 1, holdings_count: 0, selected_accounts: selected.length, known_accounts: selected.length, missing_accounts: 0, valued_holdings: 0, priced_holdings: 0, classified_holdings: 0, partial: false }, holdings: [], sectors: [], warnings: [], methodology: [] });
    }
    if (url.pathname === '/api/wealth') {
      requests.push(url);
      if (options.unavailable === 'history' || options.state?.failHistory) return route.fulfill({ status: 503, json: { error: 'Synthetic history outage' } });
      const requested = url.searchParams.getAll('account');
      const selected = requested;
      const oldCohort = options.historyAccountIds?.filter(id => requested.includes(id)) ?? requested;
      const dates = options.historyGap ? ['2026-08-31', '2026-09-01', '2026-09-17', '2026-09-18'] : ['2026-09-12', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18'];
      const values = options.historyGap ? [1000, null, null, totalValue] : [1000, 0, null, 1250, totalValue];
      const points = dates.map((date, index) => ({
        date, net_worth: values[index], assets: values[index], liabilities: 0,
        known_accounts: values[index] === null ? 0 : selected.length, missing_accounts: values[index] === null ? selected.length : 0,
        estimated: index !== dates.length - 1, carried_accounts: 0, complete: values[index] !== null, coverage_key: selected.join('|'),
        valuation_key: `${index === dates.length - 1 ? 'recorded' : 'daily'}:${selected.join('|')}`, valuation_basis: index === dates.length - 1 ? 'recorded_balance' : 'daily_holdings', reconstructed_accounts: index === dates.length - 1 ? 0 : selected.length,
      }));
      return json({ generated_at: observedAt, currency: url.searchParams.get('currency'), currencies: ['GBP', 'USD'], range: url.searchParams.get('range'),
        selected_account_ids: requested, accounts: accountRows.map(row => ({ id: `connected:${row.id}`, name: row.name, value: row.value, kind: 'investment', source: 'snaptrade', institution: 'Demo', currency: row.currency,
          default_included: !row.archived, archived_by_user: row.archived, native_value: row.value, as_of: savedAt, history_points: 5, history_provider_conflict: row.id === 'demo-one' ? { dates: 169 } : undefined })),
        summary: { net_worth: totalValue, selected_accounts: requested.length },
        history: { mode: 'net_assets', account_ids: selected, excluded_account_ids: [], account_count: selected.length, selected_accounts: requested.length,
          current_value: totalValue, start_date: points[0].date, end_date: points.at(-1)!.date, end_value: totalValue, change: null, change_pct: null,
          ...(options.historyGap ? { continuity: { last_continuous_date: '2026-08-31', recorded_start_date: '2026-09-18', recorded_end_date: '2026-09-18', recorded_days: 1,
            gaps: [{ start_date: '2026-09-01', end_date: '2026-09-17', days: 17, reason: 'missing_account_history', account_ids: selected }] } } : {}),
          change_comparable: false, points, net_points: points,
          cohort: { mode: 'comparable_cohort', account_ids: oldCohort, excluded_account_ids: requested.filter(id => !oldCohort.includes(id)),
            account_count: oldCohort.length, selected_accounts: requested.length, points: [] },
          coverage_note: 'Daily values use dated holdings and FX. The current observation starts a separate segment.' },
        points, warnings: [], methodology: [], cache: { saved_at: savedAt, stale: false, refresh_error: null } });
    }
    if (url.pathname === '/api/investments/market/value') {
      requests.push(url);
      if (options.unavailable === 'prices') return route.fulfill({ status: 503, json: { error: 'Synthetic price outage' } });
      return json({ generated_at: savedAt, currency: url.searchParams.get('currency'), range: url.searchParams.get('range'), interval: url.searchParams.get('interval'),
        basis: 'current_holdings', holdings_as_of: savedAt, market_as_of: savedAt, selected_account_ids: url.searchParams.getAll('account'), status: 'partial',
        summary: { value: 125, change: null, change_pct: null, selected_accounts: url.searchParams.getAll('account').length, known_accounts: 2, positions: 2, priced_positions: 2, partial: true },
        points: marketPoints, warnings: [], methodology: ['Prices are cached observations of current holdings.'] });
    }
    return json({ value: {}, revision: 'synthetic' });
  });
}
const latest = (requests: URL[], path: string) => requests.filter(request => request.pathname === path).at(-1);
const readout = (page: Page) => page.getByLabel('Inspected portfolio values', { exact: true });
const chart = (page: Page) => page.getByRole('group', { name: 'Interactive account value chart', exact: true });

test('one account-history chart uses the shared period and preserves current values, zero and gaps', async ({ page }, testInfo) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await fixtures(page, requests, writes);
  await page.goto('/#connected-portfolio');
  await expect(page.getByRole('heading', { name: /Asset history/ })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: 'Show recent prices' })).not.toBeChecked();
  await expect(page.getByLabel('History visual links', { exact: true })).toContainText('Dashed links bridge missing dates or a change in valuation source.');
  await expect(readout(page)).toContainText('£2,000.00');
  await expect(readout(page)).toContainText('18 Sept 2026');
  expect(latest(requests, '/api/wealth')?.searchParams.get('range')).toBe('1m');
  expect(latest(requests, '/api/investments/market/value')).toBeUndefined();
  await expect(page.getByRole('button', { name: /Both lines|Indexed trend|Recent prices|24 hours|30 days/ })).toHaveCount(0);
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByLabel('Investment price period')).toHaveCount(1);
  await expect(page.getByText('Historical values need review', { exact: true })).toHaveCount(0);
  await chart(page).focus();
  await chart(page).press('Home');
  await expect(readout(page)).toContainText('£1,000.00');
  await chart(page).press('ArrowRight');
  await expect(readout(page)).toContainText('£0.00');
  await chart(page).press('ArrowRight');
  await expect(readout(page)).toContainText('17 Sept 2026');
  await expect(readout(page)).toContainText('£1,250.00');
  await chart(page).press('End');
  await expect(readout(page)).toContainText('£2,000.00');
  await page.getByText('Dated values & sources', { exact: true }).click();
  await expect(page.getByLabel('Historical account coverage')).toContainText('169 conflicting provider estimates were superseded');
  await expect(page.getByLabel('Portfolio comparison observations').getByRole('row')).toHaveCount(5);
  await page.getByText('Dated values & sources', { exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath('account-history-current-point.png'), fullPage: false });
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});

test('optional recent-price overlay follows period, account and currency without replacing history', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [];
  await fixtures(page, requests, writes);
  await page.goto('/#connected-portfolio');
  await expect(readout(page)).toContainText('£2,000.00');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('6m');
  await expect.poll(() => latest(requests, '/api/wealth')?.searchParams.get('range')).toBe('6m');
  expect(latest(requests, '/api/investments/market/value')).toBeUndefined();
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(readout(page)).toContainText('£125.00');
  expect(latest(requests, '/api/investments/market/value')?.searchParams.get('range')).toBe('1m');
  expect(latest(requests, '/api/wealth')?.searchParams.get('range')).toBe('6m');
  await revealMobileControls(page, 'Portfolio filters');
  await expect(page.getByLabel('Investment price period')).toHaveValue('6m');
  await expect(page.getByLabel('Chart legend')).toContainText('Today’s holdings');
  await chart(page).focus(); await chart(page).press('Home');
  await expect(readout(page)).toContainText('£1,000.00');
  await chart(page).press('End'); await chart(page).press('ArrowLeft'); await chart(page).press('ArrowLeft'); await chart(page).press('ArrowLeft');
  await expect(readout(page)).toContainText('£0.00');
  await expect(readout(page)).toContainText('11:00 UTC');
  await chart(page).press('ArrowRight');
  await expect(readout(page)).toContainText('13:00 UTC');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment price period').selectOption('1w');
  await expect.poll(() => latest(requests, '/api/investments/market/value')?.searchParams.get('range')).toBe('1w');
  await expect.poll(() => latest(requests, '/api/wealth')?.searchParams.get('range')).toBe('1w');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment reporting currency').selectOption('USD');
  await expect(readout(page)).toContainText('US$125.00');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption('demo-two');
  await expect.poll(() => latest(requests, '/api/wealth')?.searchParams.getAll('account')).toEqual(['connected:demo-two']);
  await expect.poll(() => latest(requests, '/api/investments/market/value')?.searchParams.getAll('account')).toEqual(['demo-two']);
  expect(latest(requests, '/api/wealth')?.searchParams.get('currency')).toBe('USD');
  expect(latest(requests, '/api/investments/market/value')?.searchParams.get('currency')).toBe('USD');
  await page.getByRole('checkbox', { name: 'Show recent prices' }).uncheck();
  await expect(readout(page)).toContainText('US$2,000.00');
  await expect(page.getByLabel('Chart legend')).not.toContainText('Today’s holdings');
  expect(writes).toEqual([]);
});

test('default scope shares archived preferences with net assets and explicit archived accounts remain selectable', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [];
  await fixtures(page, requests, writes);
  await page.goto('/#connected-portfolio');
  await expect(readout(page)).toContainText('£2,000.00');
  await revealMobileControls(page, 'Portfolio filters');
  const selector = page.getByLabel('Account selection', { exact: true });
  await expect(selector.locator('option[value="all"]')).toHaveText('Active accounts (2)');
  for (const path of ['/api/wealth', '/api/investments/analysis']) expect(latest(requests, path)?.searchParams.getAll('account')).toEqual(path === '/api/wealth' ? ['connected:demo-one', 'connected:demo-two'] : ['demo-one', 'demo-two']);
  await expect(selector.locator('option[value="zero-one"]')).toContainText('Archived');
  await selector.selectOption('zero-one');
  await expect.poll(() => latest(requests, '/api/wealth')?.searchParams.getAll('account')).toEqual(['connected:zero-one']);
  await expect.poll(() => latest(requests, '/api/investments/analysis')?.searchParams.getAll('account')).toEqual(['zero-one']);
  await selector.selectOption('all');
  await expect.poll(() => latest(requests, '/api/wealth')?.searchParams.getAll('account')).toEqual(['connected:demo-one', 'connected:demo-two']);
  expect(writes).toEqual([]);
});

test('the recent-price overlay uses the full selected net-assets scope', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [];
  await fixtures(page, requests, writes, { historyAccountIds: ['connected:demo-one'] });
  await page.goto('/#connected-portfolio');
  await expect(readout(page)).toContainText('£2,000.00');
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(readout(page)).toContainText('£125.00');
  expect(latest(requests, '/api/investments/market/value')?.searchParams.getAll('account')).toEqual(['demo-one', 'demo-two']);
  expect(writes).toEqual([]);
});

test('320px touch layout keeps inspection usable and lets the page scroll through the chart', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Touch gestures run in the mobile project.');
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 320, height: 740 });
  await fixtures(page, requests, writes);
  await page.goto('/#connected-portfolio');
  await expect(readout(page)).toContainText('£2,000.00');
  await revealMobileControls(page, 'Portfolio filters');
  for (const control of [page.getByLabel('Account selection'), page.getByLabel('Investment reporting currency'), page.getByLabel('Investment price period'),
    page.getByLabel('Inspect comparison observations', { exact: true }), page.getByRole('button', { name: 'Previous comparison observation', exact: true }),
    page.getByRole('button', { name: 'Next comparison observation', exact: true }), page.getByRole('button', { name: 'Zoom in on comparison', exact: true }),
    page.getByRole('button', { name: 'Reset comparison zoom', exact: true }), page.getByRole('checkbox', { name: 'Show recent prices' }).locator('..')]) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull(); expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(320);
  }
  await page.getByRole('button', { name: 'Previous comparison observation', exact: true }).tap();
  await expect(readout(page)).toContainText('£1,250.00');
  await page.getByRole('button', { name: 'Next comparison observation', exact: true }).tap();
  await expect(readout(page)).toContainText('£2,000.00');
  await page.getByLabel('Inspect comparison observations', { exact: true }).focus();
  await page.keyboard.press('Home'); await expect(readout(page)).toContainText('£1,000.00');
  await page.keyboard.press('End'); await expect(readout(page)).toContainText('£2,000.00');
  await chart(page).evaluate(element => element.scrollIntoView({ block: 'center' }));
  const box = await chart(page).boundingBox(); const scrollBefore = await page.evaluate(() => scrollY);
  const session = await page.context().newCDPSession(page);
  const touch = (y: number) => [{ x: box!.x + box!.width / 2, y, radiusX: 4, radiusY: 4 }];
  const initialY = box!.y + box!.height / 2 + 60;
  await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: touch(initialY) });
  for (let step = 1; step <= 10; step += 1) await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: touch(initialY - step * 12) });
  await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(scrollBefore + 40);
  await session.detach();
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(page.getByLabel('Inspect comparison observations', { exact: true })).toHaveAttribute('max', '7');
  await chart(page).focus(); await chart(page).press('End');
  await expect(readout(page)).toContainText('£125.00');
  await page.getByText('Dated values & sources', { exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath('account-history-320px.png'), fullPage: true });
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

for (const unavailable of ['history', 'prices'] as const) test(`${unavailable} outages have a retry and never require writes`, async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await fixtures(page, requests, writes, { unavailable });
  await page.goto('/#connected-portfolio');
  if (unavailable === 'history') {
    await expect(page.getByRole('button', { name: 'Retry history', exact: true })).toBeVisible();
  } else {
    await expect(readout(page)).toContainText('£2,000.00');
    await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
    await expect(page.getByRole('button', { name: 'Retry prices', exact: true })).toBeVisible();
    await expect(readout(page)).toContainText('£2,000.00');
  }
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('a missing interval stays visible beside today’s recorded balance without fabricated observations', async ({ page }, testInfo) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await fixtures(page, requests, writes, { historyGap: true });
  await page.goto('/#connected-portfolio');
  const gap = page.getByLabel('Account history gap', { exact: true });
  await expect(gap).toContainText('1 Sept 2026 to 17 Sept 2026');
  await expect(gap).toContainText('Recorded broker balances begin 18 Sept 2026');
  await expect(readout(page)).toContainText('£2,000.00');
  await expect(readout(page)).toContainText('18 Sept 2026');
  await chart(page).focus(); await chart(page).press('ArrowLeft');
  await expect(readout(page)).toContainText('31 Aug 2026');
  await chart(page).press('ArrowRight');
  await expect(readout(page)).toContainText('18 Sept 2026');
  await page.getByText('Dated values & sources', { exact: true }).click();
  await expect(page.getByLabel('Portfolio comparison observations').getByRole('row')).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  if (testInfo.project.name === 'mobile') {
    await page.setViewportSize({ width: 320, height: 740 });
    await page.getByText('Chart sources & coverage · history gap', { exact: true }).click();
    await expect(gap).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath('history-gap-320px.png'), fullPage: true });
  }
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('one-minute cached reads update the headline and source dates without a brokerage refresh', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  const state = { retrievedAt: savedAt, value: 2000, connectedReads: 0, analysisReads: 0 };
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({ time: new Date(savedAt) });
  await fixtures(page, requests, writes, { state });
  await page.goto('/#connected-portfolio');
  const headline = page.getByRole('button', { name: 'Balance details', exact: true }).locator('../..');
  await expect(headline).toContainText('£2,000.00');
  const connectedBefore = state.connectedReads, analysisBefore = state.analysisReads;
  state.retrievedAt = '2026-09-18T15:01:00Z'; state.value = 2025;
  await page.clock.fastForward(60_001);
  await expect.poll(() => state.connectedReads).toBeGreaterThan(connectedBefore);
  await expect.poll(() => state.analysisReads).toBeGreaterThan(analysisBefore);
  await expect(headline).toContainText('£2,025.00');
  await expect(readout(page)).toContainText('£2,025.00');
  await page.getByRole('button', { name: 'Balance details', exact: true }).click();
  await expect(page.locator('#investment-analysis-notes')).toContainText('Broker balances as of 18 Sept 2026, 16:01');
  // A market or FX cache update can change value while the saved brokerage timestamp stays the same.
  const historyReads = requests.filter(request => request.pathname === '/api/wealth').length;
  state.value = 2050;
  await page.clock.fastForward(60_001);
  await expect.poll(() => requests.filter(request => request.pathname === '/api/wealth').length).toBeGreaterThan(historyReads);
  await expect(headline).toContainText('£2,050.00');
  await expect(readout(page)).toContainText('£2,050.00');
  await expect(page.locator('#investment-analysis-notes')).toContainText('Broker balances as of 18 Sept 2026, 16:01');
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('a degraded saved-portfolio poll preserves selected accounts and recovers on the next cached read', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  const state = { retrievedAt: savedAt, value: 2000, connectedReads: 0, analysisReads: 0, failConnected: false };
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({ time: new Date(savedAt) });
  await fixtures(page, requests, writes, { state });
  await page.goto('/#connected-portfolio');
  const headline = page.getByRole('button', { name: 'Balance details', exact: true }).locator('../..');
  await revealMobileControls(page, 'Portfolio filters');
  const selector = page.getByLabel('Account selection', { exact: true });
  await expect(headline).toContainText('£2,000.00');
  const connectedBefore = state.connectedReads;
  state.failConnected = true;
  await page.clock.fastForward(60_001);
  await expect.poll(() => state.connectedReads).toBeGreaterThan(connectedBefore);
  await expect(page.getByRole('alert')).toContainText('Update failed. Saved data remains visible.');
  await expect(selector.locator('option[value="all"]')).toHaveText('Active accounts (2)');
  await expect(selector).toHaveValue('all');
  await expect(headline).toContainText('£2,000.00');
  await expect(readout(page)).toContainText('£2,000.00');
  await expect(page.getByText('No investment accounts are available in the saved portfolio.', { exact: true })).toHaveCount(0);
  state.failConnected = false; state.retrievedAt = '2026-09-18T15:02:00Z'; state.value = 2025;
  await page.clock.fastForward(60_001);
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(headline).toContainText('£2,025.00');
  await expect(readout(page)).toContainText('£2,025.00');
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('an unavailable history poll keeps saved observations and visibility resume reads the recovered cache', async ({ page }) => {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  const state = { retrievedAt: savedAt, value: 2000, connectedReads: 0, analysisReads: 0, failHistory: false };
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install({ time: new Date(savedAt) });
  await fixtures(page, requests, writes, { state });
  await page.goto('/#connected-portfolio');
  await expect(readout(page)).toContainText('£2,000.00');
  state.failHistory = true;
  await page.clock.fastForward(60_001);
  await expect(page.getByRole('status').filter({ hasText: 'Saved history remains visible.' })).toBeVisible();
  await expect(readout(page)).toContainText('£2,000.00');
  state.failHistory = false; state.value = 2075;
  const historyReads = requests.filter(request => request.pathname === '/api/wealth').length;
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect.poll(() => requests.filter(request => request.pathname === '/api/wealth').length).toBeGreaterThan(historyReads);
  await expect(readout(page)).toContainText('£2,075.00');
  await expect(page.getByRole('button', { name: 'Retry history', exact: true })).toHaveCount(0);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('recent-price sources disclose quote age separately from portfolio observations', async ({ page }) => {
  await fixtures(page, [], []);
  await page.goto('/#connected-portfolio');
  await page.getByRole('checkbox', { name: 'Show recent prices' }).check();
  await expect(page.getByText(/^Price range:/)).toContainText('18 Sept 2026, 10:00 UTC');
  await expect(page.getByText(/^Price range:/)).toContainText('to');
  await expect(page.getByText(/^Price range:/)).toContainText('pre-market');
  await expect(page.getByText(/^Oldest price in the latest estimate:/)).toBeHidden();
  await page.getByText('Chart sources & coverage', { exact: true }).click();
  await expect(page.getByText(/^Oldest price in the latest estimate:/)).toContainText('18 Sept 2026, 10:00 UTC');
  await expect(page.getByText(/Broker balances can use newer prices or different FX/)).toBeVisible();
});
