import { revealMobileControls } from './navigation-helpers';
import { test, expect } from '@playwright/test';

test.use({ timezoneId: 'Europe/London' });

const savedAt = '2026-09-18T10:00:00Z';
const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
const account = {
  id: 'currency-demo', name: 'Currency demo', institution: 'Demo broker', number_suffix: null, type: 'Invest',
  currency: 'GBP', total_value: 100, connection_id: 'demo-connection', is_paper: false,
  holdings_synced_at: savedAt, activities_synced_at: savedAt, initial_sync_complete: true,
  balances: section(), positions: section(), history: section(),
  activities: section([{ id: 'dividend', type: 'DIVIDEND', description: 'Demo dividend', symbol: 'DEMO', amount: 10, currency: 'GBP', quantity: null, date: '2026-09-16' }]),
  orders: section([{ id: 'filled-order', symbol: 'FILLEDDEMO', status: 'FILLED', quantity: 1, filled_quantity: 1, currency: 'GBP', placed_at: savedAt }]),
};
const secondAccount = { ...account, id: 'second-demo', name: 'Second demo',
  activities: section([{ id: 'buy', type: 'BUY', description: 'Demo purchase', symbol: 'DEMO', amount: -10, currency: 'GBP', quantity: 1, date: '2026-09-16' }]),
  orders: section([{ id: 'pending-order', symbol: 'PENDINGDEMO', status: 'PENDING', quantity: 1, filled_quantity: 0, currency: 'GBP', placed_at: savedAt }]),
};
const holdings = [
  { id: 'priced', position_id: 'priced', instrument_id: 'usd-listing', symbol: 'DEMO', value: 60, market_currency: 'USD' },
  { id: 'unknown', position_id: 'unknown', instrument_id: 'unknown-listing', symbol: 'UNKNOWN', value: 40, market_currency: null },
].map(row => ({
  ...row, account_id: account.id, account_name: account.name, institution: account.institution,
  name: `${row.symbol} company`, asset_type: 'stock', currency: 'GBP', quantity: 1,
  native_price: row.value, native_value: row.value, native_average_cost: row.value,
  native_unrealized_pnl: 0, unrealized_pnl: 0, unrealized_pct: 0, weight_pct: row.value,
  sector: 'Technology', market_symbol: row.market_currency ? row.symbol : null, market_as_of: savedAt,
  market_status: row.market_currency ? 'ready' : 'unmatched', returns: { '1d': 0, '1w': 0, '1m': 0, '3m': null, ytd: null, '1y': null },
  period_return_pct: 0, day_move_estimate: 0, is_cash: false, stale: false,
}));

function calendarFixture(url: URL, intervalChange = 25) {
  const year = Number(url.searchParams.get('year')), month = Number(url.searchParams.get('month'));
  const day = (n: number) => `${year}-${String(month).padStart(2, '0')}-${String(n).padStart(2, '0')}`;
  const summary = { change: null, change_pct: null, complete: false, observed_days: 0, expected_days: 3, positive_days: 0, negative_days: 0 };
  return { currency: 'GBP', year, month, source: 'estimate', metric: 'account_value_change', selected_account_ids: [account.id], paper: false,
    available_years: [year], available_sources: [], summary, year_summary: summary, months: [], warnings: [], methodology: [],
    points: [{ date: day(1), value: 100 }, { date: day(3), value: 100 + intervalChange }],
    days: [1, 2, 3].map(n => ({ date: day(n), end_value: n === 1 ? 100 : n === 3 ? 100 + intervalChange : null,
      start_value: null, change: null, observation_change: n === 3 ? intervalChange : null,
      observation_start_value: n === 3 ? 100 : null, observation_start_date: n === 3 ? day(1) : null,
      observation_days: n === 3 ? 2 : null, known_accounts: n === 2 ? 0 : 1, selected_accounts: 1, reason: n === 2 ? 'missing' : null })),
  };
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown) => route.fulfill({ json: data });
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: [account, secondAccount].map(row => ({ id: `connected:${row.id}`, default_included: true, archived_by_user: false, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') return json({ configured: true, retrieved_at: savedAt, error: null, accounts: [account, secondAccount], connections: section() });
    if (url.pathname === '/api/investments/analysis') return json({
      retrieved_at: savedAt, market_as_of: savedAt, currency: 'GBP', range: url.searchParams.get('range'), selected_account_ids: [account.id], paper: false,
      accounts: [], holdings, sectors: [], warnings: [], methodology: [],
      summary: { value: 100, cash: 0, unrealized_pnl: 0, day_move_estimate: 0, holdings_count: 2, selected_accounts: 1, known_accounts: 1, missing_accounts: 0, valued_holdings: 2, priced_holdings: 1, classified_holdings: 2, partial: false },
    });
    if (url.pathname === '/api/investments/analysis/security') {
      const range = url.searchParams.get('range');
      const flat = range === '1m';
      return json({
        holding: { ...holdings[0], period_return_pct: flat ? 0 : range === 'all' ? 50 : null },
        currency: 'GBP', range, market_currency: 'USD', price_basis: 'price_return', source_url: null, warnings: [],
        points: [{ date: '2026-08-19', close: 100, return_pct: 0 }, { date: '2026-09-18', close: flat ? 100 : 150, return_pct: flat ? 0 : 50 }],
      });
    }
    if (url.pathname === '/api/wealth') return json({ generated_at: savedAt, currency: 'GBP', selected_account_ids: url.searchParams.getAll('account'), accounts: [], points: [], summary: { selected_accounts: 1 }, warnings: [] });
    if (url.pathname === '/api/investments/analysis/calendar') return json(calendarFixture(url));
    return json({ value: {}, revision: 'synthetic' });
  });
  await page.goto('/#connected-portfolio');
  await expect(page.getByRole('heading', { name: /^Holdings performance/ })).toBeVisible();
});

test('quote-currency allocation keeps valuation currency and unmatched listings separate', async ({ page }) => {
  await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
  await page.getByRole('button', { name: 'Currency', exact: true }).click();
  const allocation = page.getByRole('heading', { name: 'Priced holdings allocation' }).locator('../..');
  const dollarRow = allocation.locator('dl > div').filter({ has: page.getByText('USD', { exact: true }) });
  await expect(dollarRow).toContainText('£60.00');
  await expect(dollarRow).toContainText('60.0% of priced holdings');
  const unknownRow = allocation.locator('dl > div').filter({ has: page.getByText('Unknown quote currency', { exact: true }) });
  await expect(unknownRow).toContainText('£40.00');
  await expect(unknownRow).toContainText('40.0% of priced holdings');
  await expect(allocation.locator('dt').getByText('GBP', { exact: true })).toHaveCount(0);
});

test('security detail preserves unavailable requested-period returns and genuine zero across periods', async ({ page }, testInfo) => {
  await page.getByRole('button', { name: 'View DEMO performance in Currency demo', exact: true }).filter({ visible: true }).click();
  const dialog = page.getByRole('dialog');
  const headlineReturn = dialog.getByText('period price return', { exact: true }).locator('..');
  await expect(headlineReturn).toContainText('0.00%');
  await dialog.getByLabel('Security history period').selectOption('1y');
  await expect(dialog.getByText(/Requested-period return is unavailable/)).toBeVisible();
  await expect(headlineReturn).toContainText('—');
  await expect(headlineReturn).not.toContainText('50.00%');
  await expect(dialog.getByText(/Available observations:/)).toContainText('19 Aug 2026 – 18 Sept 2026');
  await dialog.getByRole('button', { name: 'Price return', exact: true }).click();
  await expect(headlineReturn).toContainText('—');
  await dialog.screenshot({ path: testInfo.outputPath('unavailable-period-detail.png') });
  await dialog.getByLabel('Security history period').selectOption('all');
  await expect(headlineReturn).toContainText('+50.00%');
  await expect(dialog.getByText(/Requested-period return is unavailable/)).toHaveCount(0);
  await dialog.getByLabel('Security history period').selectOption('1m');
  await expect(headlineReturn).toContainText('0.00%');
});

test('activity and order filters reset to visible choices when account scope changes', async ({ page }) => {
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('combobox', { name: 'Activity type', exact: true }).selectOption('DIVIDEND');
  await expect(page.getByText('Demo purchase', { exact: true })).toHaveCount(0);
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption(secondAccount.id);
  await expect(page.getByRole('combobox', { name: 'Activity type', exact: true })).toHaveValue('all');
  await expect(page.getByText('Demo purchase', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption('all');
  await expect(page.getByRole('combobox', { name: 'Activity type', exact: true })).toHaveValue('all');
  await page.getByRole('button', { name: 'Orders', exact: true }).click();
  await page.getByRole('combobox', { name: 'Order status', exact: true }).selectOption('FILLED');
  await expect(page.getByText('PENDINGDEMO', { exact: true })).toHaveCount(0);
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption(secondAccount.id);
  await expect(page.getByRole('combobox', { name: 'Order status', exact: true })).toHaveValue('all');
  await expect(page.getByText('PENDINGDEMO', { exact: true })).toBeVisible();
});

test('calendar exact-value table follows between-observation versus daily mode', async ({ page }, testInfo) => {
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('button', { name: 'Value calendar', exact: true }).click();
  await page.getByText('Calculation & observations', { exact: true }).click();
  await expect(page.getByRole('columnheader', { name: 'Interval change', exact: true })).toBeVisible();
  const change = page.getByRole('table').getByRole('row').nth(3).getByRole('cell').nth(2);
  await expect(change).toContainText('£25.00');
  await expect(change).toContainText('2 days');
  await page.getByLabel('Calendar comparison', { exact: true }).selectOption('daily');
  await expect(page.getByRole('columnheader', { name: 'Daily change', exact: true })).toBeVisible();
  await expect(change).toHaveText('—');
  await page.getByLabel('Calendar comparison', { exact: true }).selectOption('observed');
  await expect(change).toContainText('£25.00');
  await page.screenshot({ path: testInfo.outputPath('observed-calendar-values.png'), fullPage: true });
});

test('single-observation and empty security histories never establish a period return', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/investments/analysis/security?**', route => {
    const url = new URL(route.request().url());
    const empty = url.searchParams.get('position') === 'unknown';
    return route.fulfill({ json: {
      holding: { ...(empty ? holdings[1] : holdings[0]), period_return_pct: null },
      currency: 'GBP', range: url.searchParams.get('range'), market_currency: empty ? null : 'USD',
      price_basis: 'price_return', source_url: null, warnings: [],
      points: empty ? [] : [{ date: '2026-09-18', close: 100, return_pct: 0 }],
    } });
  });
  await page.getByRole('button', { name: 'View DEMO performance in Currency demo', exact: true }).filter({ visible: true }).click();
  const dialog = page.getByRole('dialog');
  const headlineReturn = dialog.getByText('period price return', { exact: true }).locator('..');
  await expect(dialog.getByText(/Requested-period return is unavailable/)).toBeVisible();
  await expect(headlineReturn).toContainText('—');
  await expect(headlineReturn).not.toContainText('0.00%');
  await dialog.getByRole('button', { name: 'Price return', exact: true }).click();
  await expect(headlineReturn).toContainText('—');
  await dialog.getByLabel('Security to chart', { exact: true }).selectOption('unknown');
  await expect(dialog.getByText('No supported market history is saved for this security yet.', { exact: true })).toBeVisible();
  await expect(headlineReturn).toContainText('—');
  await expect(dialog.getByText(/Available observations:/)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('calendar keeps a genuine zero interval distinct from missing daily change', async ({ page }) => {
  await page.route('**/api/investments/analysis/calendar?**', route => route.fulfill({
    json: calendarFixture(new URL(route.request().url()), 0),
  }));
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('button', { name: 'Value calendar', exact: true }).click();
  await page.getByText('Calculation & observations', { exact: true }).click();
  const change = page.getByRole('table').getByRole('row').nth(3).getByRole('cell').nth(2);
  await expect(change).toContainText('£0.00');
  await expect(change).toContainText('2 days');
  await page.getByRole('button', { name: /£0\.00 value change, including cash flows over 2 days/ }).click();
  await expect(page.getByRole('status')).toContainText('£0.00');
  await page.getByLabel('Calendar comparison', { exact: true }).selectOption('daily');
  await expect(change).toHaveText('—');
  await expect(page.getByRole('status')).toContainText('Both comparison dates need complete coverage.');
});

test('activity pagination resets when the account scope removes the second page', async ({ page }) => {
  const manyActivities = Array.from({ length: 30 }, (_, index) => ({
    id: `dividend-${index}`, type: 'DIVIDEND', description: `Dividend ${index + 1}`, symbol: 'DEMO',
    amount: index === 0 ? 0 : 10, currency: 'GBP', quantity: null, date: '2026-09-16',
  }));
  await page.route('**/api/investments/connected', route => route.fulfill({ json: {
    configured: true, retrieved_at: savedAt, error: null,
    accounts: [{ ...account, activities: section(manyActivities) }, secondAccount], connections: section(),
  } }));
  await page.reload();
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await expect(page.getByRole('row').filter({ hasText: 'Dividend 1' }).first()).toContainText('£0.00');
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByText('Page 2 of 2', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection', { exact: true }).selectOption(secondAccount.id);
  await expect(page.getByText('Demo purchase', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Next', exact: true })).toHaveCount(0);
  await page.getByRole('searchbox', { name: 'Search activity', exact: true }).fill('not present');
  await expect(page.getByText('No activity matches these filters.', { exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search activity', exact: true }).fill('');
  await expect(page.getByText('Demo purchase', { exact: true })).toBeVisible();
});

test('complete account balances stay distinct from partial session estimates and fund classification', async ({ page }, testInfo) => {
  const writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  const excluded = [
    ['APLD', 'share_units_changed'], ['NVDA', 'missing_previous_session_close'],
    ['META', 'missing_previous_session_close'], ['PYPL', 'missing_previous_session_close'],
  ];
  const positions = Array.from({ length: 79 }, (_, index) => ({
    ...holdings[0], id: `position-${index}`, position_id: `position-${index}`, instrument_id: `listing-${index}`,
    symbol: excluded[index]?.[0] ?? `SEC${index}`, name: `Synthetic security ${index}`,
    asset_type: index < 73 ? 'stock' : 'etf', sector: index < 73 ? 'Technology' : 'Funds',
    native_value: 4000, value: 4000, weight_pct: 4000 / 337238.77 * 100,
    day_move_estimate: index < 4 ? null : 100,
    day_move_unavailable_reason: excluded[index]?.[1] ?? null,
    returns: { ...holdings[0].returns, '1d': index < 4 ? null : 2.5 },
  }));
  await page.route('**/api/investments/analysis?**', route => route.fulfill({ json: {
    retrieved_at: '2026-09-18T16:41:00Z', balance_as_of: '2026-09-18T09:18:00Z', holdings_as_of: '2026-09-18T16:40:00Z',
    market_as_of: '2026-09-18T16:30:00Z', currency: 'GBP', range: new URL(route.request().url()).searchParams.get('range'),
    selected_account_ids: [account.id, secondAccount.id], paper: false, accounts: [], holdings: positions, sectors: [], warnings: [], methodology: [],
    summary: { value: 337238.77, cash: 21238.77, unrealized_pnl: 79000, day_move_estimate: 7500,
      holdings_count: 79, selected_accounts: 2, known_accounts: 2, missing_accounts: 0, valued_holdings: 79,
      priced_holdings: 75, classified_holdings: 73, direct_holdings: 73, fund_holdings: 6, unclassified_holdings: 0,
      pnl_holdings: 79, partial: true },
  } }));
  await page.reload();
  const balance = page.getByRole('button', { name: 'Balance details', exact: true }).locator('../..');
  await expect(balance).toContainText('£337,238.77');
  await expect(balance).toContainText('2 / 2 accounts');
  await expect(balance).not.toContainText(/partial/i);
  const session = page.getByLabel('Session move coverage', { exact: true });
  await expect(session).toHaveText('75 / 79 positions · partial');
  await session.click();
  const notes = page.locator('#investment-analysis-notes');
  await expect(page.getByLabel('Investment data coverage')).toContainText('Positions valued79 / 79');
  await expect(notes).toContainText('73 / 73 direct positions classified by sector · 6 funds grouped separately');
  await expect(notes).not.toContainText('73 / 79');
  const reasons = page.getByLabel('Positions excluded from session move', { exact: true });
  await expect(reasons.locator('summary')).toHaveText(['Previous session close unavailable · 3 positions', 'Share units need verification after a split · 1 position']);
  await expect(reasons.getByRole('button', { name: 'View APLD details in Currency demo', exact: true })).toBeHidden();
  await reasons.getByText('Previous session close unavailable', { exact: false }).click();
  for (const symbol of ['NVDA', 'META', 'PYPL']) await expect(reasons.getByRole('button', { name: `View ${symbol} details in Currency demo`, exact: true })).toBeVisible();
  await expect(reasons).toContainText('Their known market values remain included in your account value.');
  await expect(notes).toContainText('Broker balances as of 18 Sept 2026, 10:18');
  await expect(notes).toContainText('Holdings as of 18 Sept 2026, 17:40');
  await expect(notes.getByText(/Broker balances as of/)).toBeHidden();
  await notes.getByText('Source details', { exact: true }).click();
  await expect(notes.getByText(/Broker balances as of/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  if (testInfo.project.name === 'mobile') {
    await page.setViewportSize({ width: 320, height: 740 });
    await expect(reasons).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath('separate-metric-coverage-320px.png'), fullPage: true });
  }
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('a broker balance without a valuation timestamp shows its retrieval time without changing coverage', async ({ page }) => {
  const writes: string[] = [], errors: string[] = [];
  const state = { retrievedAt: '2026-09-18T18:45:00Z' };
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.clock.install({ time: new Date(state.retrievedAt) });
  await page.route('**/api/investments/connected', route => route.fulfill({ json: {
    configured: true, retrieved_at: state.retrievedAt, error: null,
    accounts: [account, secondAccount].map(row => ({ ...row, total_value_as_of: null })), connections: section(),
  } }));
  await page.route('**/api/investments/analysis?**', route => route.fulfill({ json: {
    retrieved_at: state.retrievedAt, balance_as_of: null, holdings_as_of: savedAt, market_as_of: savedAt,
    currency: 'GBP', range: new URL(route.request().url()).searchParams.get('range'),
    selected_account_ids: [account.id, secondAccount.id], paper: false,
    accounts: [], holdings, sectors: [], warnings: [], methodology: [],
    summary: { value: 200, cash: 100, unrealized_pnl: 0, day_move_estimate: 0,
      holdings_count: 2, selected_accounts: 2, known_accounts: 2, missing_accounts: 0,
      valued_holdings: 2, priced_holdings: 1, classified_holdings: 2, direct_holdings: 2,
      fund_holdings: 0, unclassified_holdings: 0, pnl_holdings: 2, partial: true },
  } }));
  await page.reload();
  const balance = page.getByRole('button', { name: 'Balance details', exact: true }).locator('../..');
  await expect(balance).toContainText('£200.00');
  await expect(balance).toContainText('2 / 2 accounts');
  await expect(balance).not.toContainText(/partial/i);
  await page.getByRole('button', { name: 'Balance details', exact: true }).click();
  const notes = page.locator('#investment-analysis-notes');
  await expect(notes).toContainText('Broker balances retrieved 18 Sept 2026, 19:45; the broker did not supply a valuation timestamp.');
  await expect(notes).not.toContainText('Broker balances as of');
  await expect(notes).toContainText('Holdings as of 18 Sept 2026, 11:00');
  await expect(page.getByLabel('Investment data coverage').locator('dd')).toHaveText(['2 / 2', '2 / 2', '1 / 2']);
  state.retrievedAt = '2026-09-18T18:46:00Z';
  await page.clock.fastForward(60_001);
  await expect(notes).toContainText('Broker balances retrieved 18 Sept 2026, 19:46; the broker did not supply a valuation timestamp.');
  await expect(notes).not.toContainText('Broker balances as of');
  await expect(notes).toContainText('Holdings as of 18 Sept 2026, 11:00');
  await expect(balance).toContainText('£200.00');
  await expect(balance).toContainText('2 / 2 accounts');
  await expect(balance).not.toContainText(/partial/i);
  await expect(page.getByLabel('Session move coverage', { exact: true })).toHaveText('1 / 2 positions · partial');
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('repeated session exclusions collapse by reason while preserving each account and position', async ({ page }, testInfo) => {
  const writes: string[] = [], errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  const selectedAccounts = [account, secondAccount].map((row, index) => ({ ...row, name: 'Investment account', number_suffix: `${1001 + index}`, value: 100, selected: true }));
  const positions = Array.from({ length: 79 }, (_, index) => ({ ...holdings[0],
    id: `excluded-${index}`, position_id: `excluded-${index}`, account_id: selectedAccounts[index % 2].id,
    account_name: selectedAccounts[index % 2].name, symbol: index < 2 ? 'SAME' : `SEC${index}`,
    name: `Synthetic company ${index}`, day_move_estimate: null, day_move_unavailable_reason: 'stale_holdings',
  }));
  const warning = 'Some holdings are stale. Their account values remain visible, but daily move estimates are withheld.';
  const method = 'Security returns exclude dividends, fees and FX; they are not your personal investment return.';
  await page.route('**/api/investments/connected', route => route.fulfill({ json: {
    configured: true, retrieved_at: savedAt, error: null, connections: section(),
    accounts: selectedAccounts.map((row, index) => index === 0 ? row : { ...row,
      positions: { status: 'error', data: [{ id: 'retained-position' }], truncated: false, stale: true, error: 'Synthetic holdings refresh failed.' } }),
    cache: { stale: true, refresh_error: 'Synthetic refresh incomplete.' },
  } }));
  await page.route('**/api/investments/analysis?**', route => {
    const url = new URL(route.request().url()), ids = url.searchParams.getAll('account');
    const selected = positions.filter(row => ids.includes(row.account_id));
    return route.fulfill({ json: { retrieved_at: savedAt, market_as_of: savedAt, currency: 'GBP', range: url.searchParams.get('range'),
      selected_account_ids: ids, accounts: selectedAccounts.filter(row => ids.includes(row.id)), holdings: selected, sectors: [],
      warnings: [warning, warning], methodology: [method],
      summary: { value: ids.length * 100, cash: 0, unrealized_pnl: 0, day_move_estimate: null,
        holdings_count: selected.length, selected_accounts: ids.length, known_accounts: ids.length, missing_accounts: 0,
        valued_holdings: selected.length, priced_holdings: 0, classified_holdings: selected.length, partial: true },
    } });
  });
  await page.reload();
  await page.getByLabel('Session move coverage', { exact: true }).click();
  const notes = page.locator('#investment-analysis-notes');
  const exclusions = page.getByLabel('Positions excluded from session move', { exact: true });
  await expect(page.getByLabel('Investment data coverage').locator('dd')).toHaveText(['2 / 2', '79 / 79', '0 / 79']);
  await expect(exclusions.locator('summary')).toHaveText(['Holdings need updating · 79 positions']);
  await expect(exclusions.getByRole('button').filter({ visible: true })).toHaveCount(0);
  await expect(notes.getByText(warning, { exact: true })).toBeHidden();
  await expect(notes.getByText(warning, { exact: true })).toHaveCount(1);
  await expect(notes.getByText(method, { exact: true })).toBeHidden();
  await exclusions.locator('summary').click();
  await expect(exclusions.getByText('Investment account · ••1001 · 40 positions', { exact: true })).toBeVisible();
  await expect(exclusions.getByText('Investment account · ••1002 · 39 positions', { exact: true })).toBeVisible();
  await expect(exclusions.getByRole('button', { name: 'View SAME details in Investment account · ••1001', exact: true })).toBeVisible();
  await expect(exclusions.getByRole('button', { name: 'View SAME details in Investment account · ••1002', exact: true })).toBeVisible();
  await expect(exclusions.getByRole('button')).toHaveCount(79);
  if (testInfo.project.name === 'mobile') await page.setViewportSize({ width: 320, height: 740 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await exclusions.screenshot({ path: testInfo.outputPath('grouped-session-exclusions.png') });
  await notes.getByText('How figures are calculated', { exact: true }).click();
  await expect(notes.getByText(method, { exact: true })).toBeVisible();
  await notes.getByText('Source details', { exact: true }).click();
  await expect(notes).toContainText('Holdings: refresh failed; 1 saved record retained.');
  await expect(notes).toContainText('Synthetic holdings refresh failed.');
  await expect(page.getByLabel('SnapTrade refresh status')).toContainText('Last refresh incomplete. Saved data remains available.');
  await expect(page.getByLabel('SnapTrade refresh status')).not.toContainText('Saved data may be out of date.');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection').selectOption(secondAccount.id);
  await expect(exclusions.locator('summary')).toHaveText(['Holdings need updating · 39 positions']);
  await page.getByLabel('Session move coverage', { exact: true }).click();
  await expect(exclusions.getByRole('button').filter({ visible: true })).toHaveCount(0);
  await exclusions.locator('summary').click();
  await expect(exclusions.getByRole('button', { name: /1001/ })).toHaveCount(0);
  await expect(exclusions.getByRole('button')).toHaveCount(39);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('SnapTrade refresh status follows the server schedule and preserves the last successful retrieval after a failure', async ({ page }, testInfo) => {
  const writes: string[] = [], errors: string[] = [];
  const state = { enabled: true, interval: 600, stale: false, scheduleKnown: true, success: savedAt as string | null };
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.clock.install({ time: new Date(savedAt) });
  await page.route('**/api/investments/connected', route => route.fulfill({ json: {
    configured: true, retrieved_at: savedAt, error: null, accounts: [account, secondAccount], connections: section(),
    ...(state.scheduleKnown ? { refresh_schedule: { enabled: state.enabled, interval_seconds: state.interval } } : {}),
    cache: { saved_at: savedAt, last_successful_refresh_at: state.success,
      last_attempt_at: state.stale ? '2026-09-18T10:03:00Z' : savedAt,
      is_cached: true, stale: state.stale, refresh_error: state.stale ? 'Synthetic refresh failure; saved portfolio retained.' : null },
  } }));
  await page.route('**/api/investments/analysis/coverage?**', route => route.fulfill({ json: {
    generated_at: savedAt, retrieved_at: savedAt, selected_account_ids: [account.id, secondAccount.id], paper: false,
    accounts: [account, secondAccount].map(row => ({ id: row.id, name: row.name, institution: row.institution,
      is_paper: false, connection_disabled: false, account_value_known: true, initial_sync_complete: true,
      sections: [], activity_archive: null, observations: [] })),
    performance: { personal_return_available: false, reason: 'Funding history has not been verified.' }, methodology: [],
  } }));
  await page.reload();
  await revealMobileControls(page, 'Portfolio filters');
  const status = page.getByLabel('SnapTrade refresh status', { exact: true });
  await expect(status).toContainText('Source: SnapTrade');
  await expect(status).toContainText('Last successful retrieval 18 Sept 2026, 11:00');
  await expect(status).toContainText('Balances and holdings refresh every 10 minutes while the Am I Broke? server is running.');
  await page.getByRole('tab', { name: 'Connections', exact: true }).click();
  await expect(page.getByText('SnapTrade is the source of truth for current balances and holdings. Am I Broke? retrieves current data from your connected accounts and saves each successful observation for account history.', { exact: true })).toBeVisible();
  await expect(page.getByText('Broker transactions follow SnapTrade’s daily sync, separately from balance and holdings updates. The refresh schedule and last successful retrieval are shown above.', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  if (testInfo.project.name === 'mobile') {
    await page.setViewportSize({ width: 320, height: 740 });
    await expect(status).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath('snaptrade-source-status-320px.png'), fullPage: true });
  }
  state.enabled = false;
  await page.clock.fastForward(60_001);
  await expect(status).toContainText('Automatic SnapTrade refresh is off. Use Refresh to retrieve current data.');
  await expect(status).not.toContainText('every 10 minutes');
  state.enabled = true; state.interval = 1800;
  await page.clock.fastForward(60_001);
  await expect(status).toContainText('Balances and holdings refresh every 30 minutes');
  state.stale = true;
  await page.clock.fastForward(60_001);
  await expect(status).toContainText('Last refresh incomplete. Saved data remains available.');
  await expect(status).toContainText('Last successful retrieval 18 Sept 2026, 11:00');
  await expect(status).not.toContainText('11:03');
  await page.getByRole('button', { name: 'Data details', exact: true }).click();
  await expect(page.locator('#investment-data-notes')).toContainText('Synthetic refresh failure; saved portfolio retained.');
  state.scheduleKnown = false; state.success = null;
  await page.clock.fastForward(60_001);
  await expect(status).toContainText('Automatic refresh schedule is not reported.');
  await expect(status).toContainText('Last successful retrieval time is not reported.');
  await expect(status.locator('time')).toHaveCount(0);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});
