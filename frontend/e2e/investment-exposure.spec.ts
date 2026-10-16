import { revealMobileControls } from './navigation-helpers';
import { expect, test, type Page } from '@playwright/test';
import type { InvestmentExposureData } from '../src/investmentExposureApi';

const savedAt = '2026-09-18T12:00:00Z';
const section = () => ({ status: 'empty', data: [], error: null, truncated: false });
const accounts = [
  { id: 'first', name: 'First account', total_value: 600 }, { id: 'second', name: 'Second account', total_value: 400 },
  { id: 'zero', name: 'Zero account', total_value: 0 }, { id: 'withheld', name: 'Conflicting account', total_value: null },
].map(account => ({ ...account, institution: 'Demo', currency: account.id === 'second' ? 'USD' : 'GBP', is_paper: false,
  connection_id: 'demo', holdings_synced_at: savedAt, activities_synced_at: savedAt, initial_sync_complete: true,
  positions: section(), balances: section(), orders: section(), activities: section(), history: section(),
}));

function exposure(url: URL): InvestmentExposureData {
  const selected = url.searchParams.getAll('account'), zero = selected.length === 1 && selected[0] === 'zero';
  const withheld = selected.length === 1 && selected[0] === 'withheld';
  const unavailable = zero || withheld;
  return { currency: url.searchParams.get('currency')!, selected_account_ids: selected, paper: false, retrieved_at: savedAt,
    status: unavailable ? 'unavailable' : 'ready',
    summary: { gross_security_value: zero ? 0 : withheld ? null : 1000, holdings: unavailable ? 0 : 22, known_holdings: unavailable ? 0 : 22,
      instruments: unavailable ? 0 : 11, selected_accounts: selected.length, accounts_with_securities: unavailable ? 0 : 2,
      largest_weight_pct: unavailable ? null : 40, top5_weight_pct: unavailable ? null : 64,
      hhi_fraction: unavailable ? null : .196, effective_instruments: unavailable ? null : 1 / .196 },
    instruments: unavailable ? [] : Array.from({ length: 11 }, (_, index) => {
      const grossValue = index === 0 ? 400 : 60;
      return { instrument_id: `listing-${index}`, symbol: `DEMO${index}`, name: `Combined company ${index}`, listing_currency: 'USD', exchange: 'XNYS',
        asset_type: 'stock', identity_verified: true, gross_value: grossValue, net_value: grossValue, weight_pct: grossValue / 10, missing_values: 0,
        accounts: accounts.slice(0, 2).map((account, i) => ({ id: account.id, name: account.name, institution: 'Demo', gross_value: grossValue * (i ? .4 : .6), positions: 1 })),
      };
    }),
    account_overlap: unavailable ? [] : [{ first_account_id: 'first', first_account_name: 'First account', second_account_id: 'second', second_account_name: 'Second account', overlap_pct: 100, shared_instruments: 11, reason: null }],
    scenarios: { shock_pct: -10, largest_gross_impact_pct: unavailable ? null : -4, top5_gross_impact_pct: unavailable ? null : -6.4,
      largest_value_change: unavailable ? null : -40, top5_value_change: unavailable ? null : -64 },
    warnings: withheld ? ['Conflicting listing identities prevent a reliable concentration calculation.'] : [],
    methodology: ['Weights use known gross securities after conversion to one reporting currency.'],
  };
}

async function fixtures(page: Page, failOnce = false) {
  const requests: URL[] = [], writes: string[] = [], errors: string[] = [];
  let attempts = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url()), json = (data: unknown) => route.fulfill({ json: data });
    if (url.pathname === '/api/auth/status') return json({ enabled: false, configured: true, authenticated: true });
    if (url.pathname === '/api/wealth/sources') return json({ sources: accounts.map(account => ({ id: `connected:${account.id}`, default_included: true, archived_by_user: false, linked_to: null })) });
    if (url.pathname === '/api/investments/connected') return json({ configured: true, retrieved_at: savedAt, accounts, connections: section(), error: null });
    if (url.pathname === '/api/investments/analysis') return json({ retrieved_at: savedAt, market_as_of: savedAt, currency: url.searchParams.get('currency'), range: url.searchParams.get('range'),
      selected_account_ids: url.searchParams.getAll('account'), accounts: [], holdings: [], sectors: [], warnings: [], methodology: [],
      summary: { value: 1000, cash: 0, unrealized_pnl: null, day_move_estimate: null, holdings_count: 0, selected_accounts: 4, known_accounts: 3, missing_accounts: 1, valued_holdings: 0, priced_holdings: 0, classified_holdings: 0, partial: true },
    });
    if (url.pathname === '/api/investments/analysis/exposure') {
      requests.push(url); attempts += 1;
      if (failOnce && attempts === 1) return route.fulfill({ status: 503, body: 'PRIVATE EXPOSURE RESPONSE' });
      return json(exposure(url));
    }
    if (url.pathname === '/api/wealth') return json({ generated_at: savedAt, currency: 'GBP', selected_account_ids: url.searchParams.getAll('account'), accounts: [], points: [], summary: { selected_accounts: 4 }, warnings: [] });
    return json({ value: {}, revision: 'synthetic' });
  });
  await page.goto('/#connected-portfolio');
  await page.getByRole('tab', { name: 'Analysis', exact: true }).click();
  return { requests, writes, errors };
}

test('combined exposure displays concentration, account overlap, scenarios and expanded holdings', async ({ page }, testInfo) => {
  const { requests, writes, errors } = await fixtures(page);
  const view = page.getByRole('region', { name: 'Combined security exposure', exact: true });
  await expect(view.getByText('Largest instrument', { exact: true }).locator('..')).toContainText('40.0%');
  await expect(view.getByText('Top five instruments', { exact: true }).locator('..')).toContainText('64.0%');
  await expect(view.getByText('Effective instrument count', { exact: true }).locator('..')).toContainText('5.10');
  await expect(view).toContainText('100.0%');
  await expect(view).toContainText('11 shared instruments');
  await view.getByText('Combined holdings · 11 instruments', { exact: true }).click();
  await expect(view.getByText('Combined company 10', { exact: true })).toHaveCount(0);
  await view.getByRole('button', { name: 'Show more combined holdings', exact: true }).click();
  await expect(view.getByText('Combined company 10', { exact: true })).toBeVisible();
  await view.getByText('What if the largest holdings fall 10%?', { exact: true }).click();
  await expect(view.getByText('Largest instrument falls', { exact: true }).locator('..')).toContainText('-£40.00');
  await expect(view.getByText('Top five instruments fall', { exact: true }).locator('..')).toContainText('-6.40%');
  await view.getByText('Exposure calculation details', { exact: true }).click();
  await expect(view).toContainText('HHI: 0.1960');
  await page.screenshot({ path: testInfo.outputPath('combined-exposure.png'), fullPage: true });
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Investment reporting currency', { exact: true }).selectOption('USD');
  await expect.poll(() => requests.at(-1)?.searchParams.get('currency')).toBe('USD');
  await expect(view).toContainText('US$1,000.00');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('empty and unreliable exposure keeps zero currency separate from unavailable percentages', async ({ page }) => {
  const { requests, writes, errors } = await fixtures(page);
  const view = page.getByRole('region', { name: 'Combined security exposure', exact: true });
  await expect(view.getByText('Largest instrument', { exact: true }).locator('..')).toContainText('40.0%');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection', { exact: true }).selectOption('zero');
  await expect.poll(() => requests.at(-1)?.searchParams.getAll('account')).toEqual(['zero']);
  await expect(view).toContainText('£0.00');
  await expect(view.getByText('Largest instrument', { exact: true }).locator('..')).toContainText('—');
  await expect(view.getByText('Effective instrument count', { exact: true }).locator('..')).toContainText('—');
  await expect(view).not.toContainText('40.0%');
  await revealMobileControls(page, 'Portfolio filters');
  await page.getByLabel('Account selection', { exact: true }).selectOption('withheld');
  await expect(view).toContainText('Conflicting listing identities prevent a reliable concentration calculation.');
  await expect(view).not.toContainText('£0.00');
  await view.getByText('What if the largest holdings fall 10%?', { exact: true }).click();
  await expect(view).toContainText('A complete identity basis and positive gross exposure without short positions are needed for these scenarios.');
  await expect(view.getByText('Largest instrument falls', { exact: true }).locator('..')).toContainText('—');
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test('exposure retries failed cached reads without leaking raw provider details', async ({ page }) => {
  const { requests, writes, errors } = await fixtures(page, true);
  const view = page.getByRole('region', { name: 'Combined security exposure', exact: true });
  await expect(view.getByRole('status')).toContainText('503');
  await expect(page.getByText('PRIVATE EXPOSURE RESPONSE', { exact: true })).toHaveCount(0);
  await view.getByRole('button', { name: 'Retry exposure', exact: true }).click();
  await expect(view).toContainText('£1,000.00');
  expect(requests).toHaveLength(2);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});
