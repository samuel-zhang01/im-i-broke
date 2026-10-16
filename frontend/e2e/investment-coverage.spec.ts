import { revealMobileControls } from './navigation-helpers';
import { expect, test, type Page } from '@playwright/test';

const savedAt = '2026-09-18T12:00:00Z';
const section = (data: unknown[] = []) => ({ status: data.length ? 'ok' : 'empty', data, error: null, truncated: false });
const accounts = [
  { id: 'zero-account', name: 'Zero account', currency: 'GBP', total_value: 0, is_paper: false },
  { id: 'unknown-account', name: 'Unknown account', currency: 'USD', total_value: null, is_paper: false },
  { id: 'paper-account', name: 'Paper account', currency: 'GBP', total_value: 100, is_paper: true },
].map(account => ({ ...account, institution: 'Demo broker', number_suffix: null, type: 'Invest', connection_id: 'demo-connection',
  holdings_synced_at: '2026-09-09T08:00:00Z', activities_synced_at: savedAt, initial_sync_complete: true,
  balances: section(), positions: section(), orders: section(), activities: section(), history: section(),
}));

function archiveCoverage(accountId: string, total = 32) {
  return { account_id: accountId, is_paper: accountId === 'paper-account', stored_activities: total,
    first_activity_at: '2026-01-01', last_activity_at: '2026-09-17', last_attempt_at: savedAt,
    last_fully_fetched_window: { start: '2026-06-20', end: '2026-09-18', retrieved_at: savedAt },
    fully_fetched_windows: [{ start: '2026-06-20', end: '2026-09-18', retrieved_at: savedAt }],
    partial_or_failed_requests: 1, history_complete: false,
    limitations: ['Fetched windows do not establish complete lifetime funding history.'],
  };
}

function coverageResponse(url: URL) {
  const selected = url.searchParams.getAll('account');
  return { generated_at: savedAt, retrieved_at: savedAt, selected_account_ids: selected, paper: selected.includes('paper-account'),
    accounts: accounts.filter(account => selected.includes(account.id)).map(account => ({
      id: account.id, name: account.name, institution: account.institution, is_paper: account.is_paper,
      connection_disabled: false, account_value_known: account.total_value !== null,
      sections: [
        { key: 'positions', status: 'error', rows: 2, retained: true, truncated: true, source_at: account.holdings_synced_at,
          source_age_days: 9, source_outdated: true, retrieved_at: savedAt, first_date: null, last_date: null, error: 'Some holdings could not be refreshed.' },
        { key: 'balances', status: 'empty', rows: 0, retained: false, truncated: false, source_at: null,
          source_age_days: null, source_outdated: false, retrieved_at: savedAt, first_date: null, last_date: null, error: null },
        { key: 'return_rates', status: 'unsupported', rows: 0, retained: false, truncated: false, source_at: null,
          source_age_days: null, source_outdated: false, retrieved_at: savedAt, first_date: null, last_date: null, error: null },
      ],
      activity_archive: archiveCoverage(account.id),
      observations: [{ retrieved_at: savedAt, observed_at: account.holdings_synced_at, source_time_known: 1, holdings_status: 'error', holdings_truncated: 1 }],
    })), performance: { personal_return_available: false, reason: 'Funding history has not been verified. Personal returns remain unavailable.' },
    methodology: ['Source sync and received dates describe separate events.'],
  };
}

async function fixtures(page: Page, options: { failCoverageOnce?: boolean; failArchiveOnce?: boolean } = {}) {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  let coverageAttempts = 0, archiveAttempts = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const json = (data: unknown) => route.fulfill({ json: data });
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: accounts.map(account => ({ id: `connected:${account.id}`, default_included: true, archived_by_user: false, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') return json({ configured: true, retrieved_at: savedAt, accounts, connections: section(), error: null });
    if (url.pathname === '/api/investments/analysis') return json({ retrieved_at: savedAt, market_as_of: savedAt, currency: url.searchParams.get('currency'),
      range: url.searchParams.get('range'), selected_account_ids: url.searchParams.getAll('account'), accounts: [], holdings: [], sectors: [], warnings: [], methodology: [],
      summary: { value: 0, cash: 0, unrealized_pnl: null, day_move_estimate: null, holdings_count: 0, selected_accounts: 2, known_accounts: 1, missing_accounts: 1, valued_holdings: 0, priced_holdings: 0, classified_holdings: 0, partial: true },
    });
    if (url.pathname === '/api/investments/analysis/coverage') {
      requests.push(url); coverageAttempts += 1;
      if (options.failCoverageOnce && coverageAttempts === 1) return route.fulfill({ status: 503, body: 'PRIVATE UPSTREAM RESPONSE' });
      return json(coverageResponse(url));
    }
    if (url.pathname === '/api/investments/connected/activities/accounts') return json({ accounts: [...accounts.map(account => ({
      id: account.id, name: account.name, institution: account.institution, is_paper: account.is_paper, stored_activities: account.id === 'zero-account' ? 32 : 1,
    })), { id: 'closed-account', name: 'Closed account', institution: 'Demo broker', is_paper: false, stored_activities: 1 }] });
    if (url.pathname === '/api/investments/connected/activities') {
      requests.push(url); archiveAttempts += 1;
      if (options.failArchiveOnce && archiveAttempts === 1) return route.fulfill({ status: 503, body: 'PRIVATE ARCHIVE RESPONSE' });
      const accountId = url.searchParams.get('account_id')!;
      const offset = Number(url.searchParams.get('offset')), total = accountId === 'zero-account' ? 32 : 1;
      return json({ activities: Array.from({ length: Math.min(25, Math.max(0, total - offset)) }, (_, i) => ({ id: `${accountId}-${offset + i}`,
        type: 'DIVIDEND', description: `Saved ${accountId} row ${offset + i + 1}`, symbol: 'DEMO', date: '2026-09-17',
        amount: offset + i === 0 ? 0 : 10, currency: 'GBP', quantity: null,
      })), pagination: { limit: 25, offset, total, has_more: offset + 25 < total }, coverage: archiveCoverage(accountId, total) });
    }
    if (url.pathname === '/api/wealth') return json({ generated_at: savedAt, currency: 'GBP', selected_account_ids: url.searchParams.getAll('account'), accounts: [], points: [], summary: { selected_accounts: 2 }, warnings: [] });
    return json({ value: {}, revision: 'synthetic' });
  });
  await page.goto('/#connected-portfolio');
  await expect(page.getByRole('heading', { name: /^Holdings performance/ })).toBeVisible();
  return { requests, writes, errors };
}

test('connection coverage separates zero and unknown totals, source dates, retained rows and fetched windows', async ({ page }, testInfo) => {
  const { requests, writes, errors } = await fixtures(page);
  await page.getByRole('tab', { name: 'Connections', exact: true }).click();
  const zero = page.getByRole('heading', { name: 'Zero account · Demo broker', exact: true }).locator('../..');
  const unknown = page.getByRole('heading', { name: 'Unknown account · Demo broker', exact: true }).locator('../..');
  await expect(zero).toContainText('Account total available');
  await expect(unknown).toContainText('Account total unknown');
  const holdings = zero.getByRole('heading', { name: 'Holdings', exact: true }).locator('..');
  await expect(holdings).toContainText('Retrieval incomplete · truncated · retained data');
  await expect(holdings).toContainText('Source synced: 9 Sept 2026');
  await expect(holdings).toContainText('Received: 18 Sept 2026');
  await expect(zero.getByRole('heading', { name: 'Cash balances' }).locator('..')).toContainText('No records returned');
  await expect(zero.getByRole('heading', { name: 'Broker returns' }).locator('..')).toContainText('Not supported');
  await zero.getByText('Saved coverage timeline · 32 activity records', { exact: true }).click();
  await expect(zero).toContainText('20 Jun 2026 – 18 Sept 2026');
  await expect(zero).toContainText('Fetched windows show completed requests; they do not prove the broker returned every transaction.');
  await expect(page.getByText('Funding history has not been verified. Personal returns remain unavailable.', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('connection-coverage.png'), fullPage: true });
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection', { exact: true }).selectOption('unknown-account');
  await expect.poll(() => requests.at(-1)?.searchParams.getAll('account')).toEqual(['unknown-account']);
  await expect(page.getByRole('heading', { name: 'Zero account · Demo broker', exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Portfolio', exact: true }).click();
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment reporting currency', { exact: true }).selectOption('USD');
  await page.getByRole('tab', { name: 'Connections', exact: true }).click();
  await expect.poll(() => requests.at(-1)?.searchParams.get('currency')).toBe('USD');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('connection coverage retries a failed cached read without leaking the upstream response', async ({ page }) => {
  const { requests, writes, errors } = await fixtures(page, { failCoverageOnce: true });
  await page.getByRole('tab', { name: 'Connections', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('503');
  await expect(page.getByText('PRIVATE UPSTREAM RESPONSE', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry coverage', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Connection data coverage', exact: true })).toBeVisible();
  expect(requests.filter(url => url.pathname === '/api/investments/analysis/coverage')).toHaveLength(2);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('saved activity paginates, resets by account, retains archived accounts and separates paper data', async ({ page }, testInfo) => {
  const { requests, writes, errors } = await fixtures(page);
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('button', { name: 'Saved history', exact: true }).click();
  const selector = page.getByRole('combobox', { name: 'Saved activity account', exact: true });
  await expect(selector).toHaveValue('zero-account');
  await expect(selector.locator('option')).toHaveCount(3);
  await expect(selector.locator('option[value="paper-account"]')).toHaveCount(0);
  const history = page.getByRole('region', { name: 'Saved investment activity', exact: true });
  await expect(history.getByRole('row').filter({ hasText: 'Saved zero-account row 1' }).first()).toContainText('£0.00');
  await expect(page.getByText('Showing 1–25 of 32', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Next saved activity', exact: true }).click();
  await expect(page.getByText('Showing 26–32 of 32', { exact: true })).toBeVisible();
  await expect(page.getByText('Saved zero-account row 26', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Next saved activity', exact: true })).toBeDisabled();
  await selector.selectOption('unknown-account');
  await expect(page.getByText('Showing 1–1 of 1', { exact: true })).toBeVisible();
  await expect.poll(() => requests.at(-1)?.searchParams.get('offset')).toBe('0');
  await expect(page.getByRole('button', { name: 'Previous saved activity', exact: true })).toBeDisabled();
  await selector.selectOption('closed-account');
  await expect(page.getByText('Saved closed-account row 1', { exact: true })).toBeVisible();
  await page.getByText('Activity coverage limits', { exact: true }).click();
  await expect(page.getByText('Fetched windows do not establish complete lifetime funding history.', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('saved-activity.png'), fullPage: true });
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection', { exact: true }).selectOption('paper');
  await expect(selector).toHaveValue('paper-account');
  await expect(selector.locator('option')).toHaveCount(1);
  await expect(page.getByText('Saved paper-account row 1', { exact: true })).toBeVisible();
  await expect(page.getByText('Saved closed-account row 1', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('saved activity retries unavailable history without showing previous account rows', async ({ page }) => {
  const { requests, writes, errors } = await fixtures(page, { failArchiveOnce: true });
  await page.getByRole('tab', { name: 'Activity', exact: true }).click();
  await page.getByRole('button', { name: 'Saved history', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('503');
  await expect(page.getByText('PRIVATE ARCHIVE RESPONSE', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry saved history', exact: true }).click();
  await expect(page.getByText('Saved zero-account row 1', { exact: true })).toBeVisible();
  expect(requests.filter(url => url.pathname === '/api/investments/connected/activities')).toHaveLength(2);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});
