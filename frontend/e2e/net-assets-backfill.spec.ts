import { expect, test } from '@playwright/test';
import { wealth } from './fixtures/wealth';

test('older complete investment values appear separately from later full net assets', async ({ page }, testInfo) => {
  const dates = ['2026-08-06', '2026-08-07', '2026-09-17', '2026-09-18', '2026-09-19'];
  const investments = dates.map((date, index) => ({ date, net_worth: 2500 + index * 10,
    assets: 2500 + index * 10, liabilities: 0, known_accounts: 1, missing_accounts: 0,
    complete: true, estimated: index < 4, carried_accounts: 0, coverage_key: 'investment',
    valuation_key: index < 4 ? 'historical' : 'current', valuation_basis: index < 4 ? 'daily_holdings' : 'recorded_balance' }));
  const net = dates.slice(-2).map((date, index) => ({ date, net_worth: 3350 + index * 50,
    assets: 3450 + index * 50, liabilities: 100, known_accounts: 3, missing_accounts: 0,
    complete: true, estimated: false, carried_accounts: 0, coverage_key: 'all-accounts', valuation_key: 'current' }));
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    ...wealth, points: net, summary: { ...wealth.summary, net_worth: 3400 },
    history: { ...wealth.history, mode: 'net_assets', points: net, net_points: net,
      net_start_date: net[0].date, start_date: net[0].date, end_date: net[1].date,
      end_value: 3400, current_value: 3400,
      investment_history: { account_ids: ['investment'], start_date: dates[0], end_date: dates[4], points: investments } },
  } }));
  await page.goto('/#net-worth');
  await expect(page.getByText('Full selected-account coverage from 18 Sept 2026', { exact: false })).toBeVisible();
  await expect(page.getByLabel('History series legend')).toContainText('Investments only · 1 selected investment account');
  const chart = page.getByRole('group', { name: 'Interactive net assets chart' });
  await chart.focus();
  await chart.press('Home');
  await expect(page.getByLabel('Inspected net assets')).toContainText('Investments only £2,500.00');
  await expect(page.getByLabel('Inspected net assets')).toContainText('full net assets unavailable');
  await chart.press('End');
  await expect(page.getByLabel('Inspected net assets')).toContainText('Net assets £3,400.00');
  const colours = await chart.locator('[_echarts_instance_]').evaluate(element => {
    const node = element as unknown as Record<string, any>;
    const key = Object.keys(node).find(key => key.startsWith('__reactFiber$'));
    let fiber = key ? node[key] : null;
    while (fiber) {
      if (typeof fiber.stateNode?.getEchartsInstance === 'function') {
        return fiber.stateNode.getEchartsInstance().getOption().series
          .filter((entry: any) => entry.id.startsWith('history-observation-') || entry.id.startsWith('investment-observation-'))
          .map((entry: any) => ({ id: entry.id, colour: entry.lineStyle.color }));
      }
      fiber = fiber.return;
    }
    return [];
  });
  expect(colours.some((entry: { id: string }) => entry.id.startsWith('history-observation-'))).toBe(true);
  expect(colours.some((entry: { id: string }) => entry.id.startsWith('investment-observation-'))).toBe(true);
  expect(new Set(colours.map((entry: { colour: string }) => entry.colour)).size).toBe(2);
  await chart.screenshot({ path: testInfo.outputPath('investment-and-net-assets.png') });
  await page.getByText('Dated values & chart details', { exact: true }).click();
  const observations = page.getByRole('region', { name: 'Net assets observations' });
  await expect(observations.getByRole('row')).toHaveCount(6);
  const oldest = observations.getByRole('row').nth(1);
  await expect(oldest.getByRole('cell').nth(1)).toHaveText('—');
  await expect(oldest.getByRole('cell').nth(2)).toContainText('£2,500.00');
});

test('complete historical net assets remain visible across a current credit valuation change', async ({ page }, testInfo) => {
  const points = Array.from({ length: 11 }, (_, index) => {
    const date = new Date(Date.UTC(2026, 8, 18 + index)).toISOString().slice(0, 10);
    const current = index === 10;
    const value = current ? 1000 : 950 + index * 5;
    return { date, net_worth: value, assets: value + 100, liabilities: 100,
      known_accounts: 3, missing_accounts: 0, complete: true, estimated: !current,
      carried_accounts: current ? 0 : 1, coverage_key: 'same-three-accounts',
      valuation_key: current ? 'credit-limit-basis' : 'statement-basis',
      valuation_basis: current ? 'mixed' : 'recorded_balance', source_break: false };
  });
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    ...wealth, points, generated_at: '2026-09-28T12:00:00Z', range: '1m',
    summary: { ...wealth.summary, net_worth: 1000, assets: 1100, liabilities: 100, change: null, change_pct: null, change_comparable: false },
    history: { ...wealth.history, mode: 'net_assets', points, net_points: points,
      net_start_date: points[0].date, start_date: points[0].date, end_date: points[10].date,
      end_value: 1000, current_value: 1000, change: null, change_pct: null, change_comparable: false },
  } }));
  await page.goto('/#net-worth');
  await expect(page.getByText('Full selected-account coverage from 18 Sept 2026', { exact: false })).toBeVisible();
  await expect(page.getByText('11 dated values shown')).toBeVisible();
  await expect(page.getByText('The latest point uses a different valuation method.', { exact: false })).toBeVisible();
  await page.getByText('Dated values & chart details', { exact: true }).click();
  await expect(page.getByLabel('Net assets observations').getByRole('row')).toHaveCount(12);
  const chart = page.getByRole('group', { name: 'Interactive net assets chart' });
  await chart.focus();
  await chart.press('Home');
  await expect(page.getByLabel('Inspected net assets')).toContainText('18 Sept 2026');
  await page.getByText('Choose exact dates', { exact: true }).click();
  await page.getByLabel('History start date').fill('2026-09-28');
  await expect(page.getByText('Showing 1 of 11 dated values')).toBeVisible();
  await page.getByRole('button', { name: 'Show all dates' }).click();
  await expect(page.getByText('11 dated values shown')).toBeVisible();
  const series = await chart.locator('[_echarts_instance_]').evaluate(element => {
    const node = element as unknown as Record<string, any>;
    const key = Object.keys(node).find(key => key.startsWith('__reactFiber$'));
    let fiber = key ? node[key] : null;
    while (fiber) {
      if (typeof fiber.stateNode?.getEchartsInstance === 'function') {
        return fiber.stateNode.getEchartsInstance().getOption().series.map((entry: any) => ({
          id: entry.id, nonNull: entry.data.filter((item: any) => (Array.isArray(item) ? item[1] : item.value[1]) !== null).length,
        }));
      }
      fiber = fiber.return;
    }
    return [];
  });
  expect(series.find((item: { id: string }) => item.id === 'history-observation-0')?.nonNull).toBe(10);
  expect(series.find((item: { id: string }) => item.id === 'history-observation-1')?.nonNull).toBe(1);
  await chart.screenshot({ path: testInfo.outputPath('net-assets-backfill.png') });
});

test('an open chart expands when older complete dates arrive', async ({ page }) => {
  const dates = Array.from({ length: 11 }, (_, index) =>
    new Date(Date.UTC(2026, 8, 18 + index)).toISOString().slice(0, 10));
  const points = dates.map((date, index) => ({ date, net_worth: 900 + index * 10,
    assets: 1000 + index * 10, liabilities: 100, known_accounts: 3,
    missing_accounts: 0, complete: true, estimated: index !== 10,
    carried_accounts: index === 10 ? 0 : 1, coverage_key: 'same-three-accounts',
    valuation_key: index === 10 ? 'today' : 'older' }));
  let complete = false;
  await page.route('**/api/wealth?**', route => {
    const shown = complete ? points : points.slice(-1);
    return route.fulfill({ json: { ...wealth, points: shown, range: '1m',
      history: { ...wealth.history, mode: 'net_assets', points: shown, net_points: shown,
        net_start_date: shown[0].date, start_date: shown[0].date, end_date: shown[shown.length - 1].date,
        end_value: 1000, current_value: 1000 },
      summary: { ...wealth.summary, net_worth: 1000, assets: 1100, liabilities: 100 },
    } });
  });
  await page.goto('/#net-worth');
  await expect(page.getByText('Full selected-account coverage from 28 Sept 2026', { exact: false })).toBeVisible();
  complete = true;
  await page.evaluate(() => {
    const channel = new BroadcastChannel('ledger-data');
    channel.postMessage('changed');
    channel.close();
  });
  await expect(page.getByText('Full selected-account coverage from 18 Sept 2026', { exact: false })).toBeVisible();
  const chart = page.getByRole('group', { name: 'Interactive net assets chart' });
  await chart.focus();
  await chart.press('Home');
  await expect(page.getByLabel('Inspected net assets')).toContainText('18 Sept 2026');
});
