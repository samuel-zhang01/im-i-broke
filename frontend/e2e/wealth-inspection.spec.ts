import { expect, test } from '@playwright/test';
import { wealth } from './fixtures/wealth';

test.use({ timezoneId: 'Europe/London' });

test('net assets history excludes earlier partial-account values and matches the headline', async ({ page }) => {
  const older = wealth.points.map(point => ({ ...point, net_worth: 2500, assets: 2500,
    liabilities: 0, known_accounts: 1, missing_accounts: 0, coverage_key: 'portfolio-only' }));
  const full = { ...wealth.points[1], coverage_key: 'all-accounts' };
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    ...wealth, history: { ...wealth.history, mode: 'net_assets', account_ids: ['investment', 'cash', 'card'], excluded_account_ids: [],
      account_count: 3, points: [full], end_value: 3400, net_start_date: '2026-09-21', net_points: [full],
      cohort: { account_ids: ['investment'], excluded_account_ids: ['cash', 'card'], points: older } },
  } }));
  await page.goto('/#net-worth');
  await expect(page.getByRole('heading', { name: 'Net assets over time' })).toBeVisible();
  await expect(page.getByText('Full selected-account coverage from 21 Sept 2026', { exact: false })).toBeVisible();
  await expect(page.getByText('Older records cover fewer selected accounts', { exact: false })).toBeVisible();
  await expect(page.getByLabel('Inspected net assets', { exact: true })).toContainText('£3,400.00');
  await page.getByText('Dated values & chart details', { exact: true }).click();
  const observations = page.getByRole('region', { name: 'Net assets observations' });
  await expect(observations.getByRole('row')).toHaveCount(2);
  await expect(observations).not.toContainText('£2,500.00');
  await expect(observations).toContainText('£3,400.00');
});

const points = [
  { date: '2026-09-15', net_worth: 100 },
  { date: '2026-09-16', net_worth: null },
  { date: '2026-09-17', net_worth: 0 },
  { date: '2026-09-18', net_worth: 125 },
].map((point, index) => ({ ...point, assets: point.net_worth, liabilities: 0, known_accounts: point.net_worth === null ? 0 : 1,
  missing_accounts: point.net_worth === null ? 1 : 0, complete: point.net_worth !== null,
  estimated: index === 3, carried_accounts: index === 3 ? 1 : 0, source_break: index === 3, coverage_key: 'demo' }));

test('shared history inspection supports keyboard, known zero, gaps and precise date windows', async ({ page }, testInfo) => {
  if (testInfo.project.name === 'mobile') await page.setViewportSize({ width: 320, height: 844 });
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    generated_at: '2026-09-18T12:00:00Z', currency: 'GBP', currencies: ['GBP'], range: 'all', selected_account_ids: ['demo'],
    accounts: [{ id: 'demo', name: 'Synthetic investment', institution: 'Demo', kind: 'investment', source: 'snaptrade', currency: 'GBP',
      default_included: true, exclusion_reason: null, native_value: 125, value: 125, as_of: '2026-09-18', stale: false, estimated: false, history_points: 4 }],
    summary: { net_worth: 125, assets: 125, liabilities: 0, known_accounts: 1, selected_accounts: 1, missing_accounts: 0,
      stale_accounts: 0, partial: false, change: 25, change_pct: 25, change_comparable: true },
    history: { mode: 'comparable_cohort', account_ids: ['demo'], excluded_account_ids: [], account_count: 1, selected_accounts: 1,
      start_date: '2026-09-15', end_date: '2026-09-18', end_value: 125, current_value: 125, change: 25, change_pct: 25,
      change_comparable: true, points, coverage_note: 'Synthetic observations with one gap.' },
    points, warnings: [], methodology: [], cache: { saved_at: null, stale: false, refresh_error: null },
  } }));
  await page.goto('/#net-worth');
  const chart = page.getByRole('group', { name: 'Interactive net assets chart', exact: true });
  const readout = page.getByLabel('Inspected net assets', { exact: true });
  await expect(page.getByLabel('History visual links', { exact: true })).toContainText('Dashed links bridge missing dates or a change in valuation source.');
  await chart.focus();
  await chart.press('Home');
  await expect(readout).toContainText('£100.00');
  await chart.press('ArrowRight');
  await expect(readout).toContainText('£0.00');
  await expect(readout).toContainText('17');
  await chart.press('End');
  await expect(readout).toContainText('£125.00');
  const tooltipState = () => chart.locator('[_echarts_instance_]').evaluate(element => {
    const node = element as unknown as Record<string, any>;
    const key = Object.keys(node).find(key => key.startsWith('__reactFiber$'));
    let fiber = key ? node[key] : null;
    while (fiber) {
      if (typeof fiber.stateNode?.getEchartsInstance === 'function') {
        const instance = fiber.stateNode.getEchartsInstance();
        const tooltip = instance.getViewOfComponentModel(instance.getModel().getComponent('tooltip'))._tooltipContent?.el;
        if (!tooltip || tooltip.ignore) return null;
        const bounds = tooltip.getBoundingRect().clone();
        bounds.applyTransform(tooltip.getComputedTransform());
        return { text: tooltip.style.text, x: bounds.x, y: bounds.y, right: bounds.x + bounds.width, bottom: bounds.y + bounds.height,
          width: instance.getWidth(), height: instance.getHeight() };
      }
      fiber = fiber.return;
    }
    return null;
  });
  // Keyboard inspection stays in the visible readout so the popup cannot cover the plot.
  expect(await tooltipState()).toBeNull();
  // Pointer inspection still uses a tooltip, which must fit the chart on phones.
  const pointPixel = await chart.locator('[_echarts_instance_]').evaluate(element => {
    const node = element as unknown as Record<string, any>;
    const key = Object.keys(node).find(key => key.startsWith('__reactFiber$'));
    let fiber = key ? node[key] : null;
    while (fiber) {
      if (typeof fiber.stateNode?.getEchartsInstance === 'function') {
        return fiber.stateNode.getEchartsInstance().convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [Date.parse('2026-09-18T00:00:00Z'), 125]);
      }
      fiber = fiber.return;
    }
    return null;
  });
  expect(pointPixel).not.toBeNull();
  if (testInfo.project.name === 'mobile') {
    await chart.press('Home');
    const box = (await chart.boundingBox())!;
    await page.touchscreen.tap(box.x + pointPixel![0], box.y + pointPixel![1]);
    await expect(readout).toContainText('£125.00');
  } else {
    await chart.hover({ position: { x: pointPixel![0], y: pointPixel![1] } });
    await expect.poll(async () => (await tooltipState())?.text ?? '').toContain('Recorded balance · estimated conversion or value');
    const tooltip = (await tooltipState())!;
    expect(tooltip.x).toBeGreaterThanOrEqual(0);
    expect(tooltip.y).toBeGreaterThanOrEqual(0);
    expect(tooltip.right).toBeLessThanOrEqual(tooltip.width);
    expect(tooltip.bottom).toBeLessThanOrEqual(tooltip.height);
  }
  await chart.evaluate(element => element.scrollIntoView({ block: 'center' }));
  await chart.screenshot({ path: testInfo.outputPath(testInfo.project.name === 'mobile' ? 'history-touch-inspection.png' : 'history-tooltip-fits.png') });
  await page.getByRole('button', { name: 'Previous value observation', exact: true }).click();
  await expect(readout).toContainText('£0.00');
  await page.getByText('Choose exact dates', { exact: true }).click();
  await page.getByLabel('History start date', { exact: true }).fill('2026-09-17');
  await page.getByLabel('History end date', { exact: true }).fill('2026-09-17');
  await expect(readout).toContainText('£0.00');
  await expect(page.getByRole('button', { name: 'Previous value observation', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Next value observation', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Reset chart zoom', exact: true }).click();
  await page.getByLabel('History start date', { exact: true }).fill('2026-09-16');
  await page.getByLabel('History end date', { exact: true }).fill('2026-09-16');
  await expect(readout).toHaveText('No observations inside this date window');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.getByText('Dated values & chart details', { exact: true }).click();
  await expect(page.getByLabel('Net assets observations').getByRole('row')).toHaveCount(points.length + 1);
  await expect(page.getByLabel('Net assets observations')).toContainText('Recorded balance · estimated conversion or value');
  expect(errors).toEqual([]);
});

test('daily valuations remain usable across a long range, missing days and source transitions', async ({ page }, testInfo) => {
  const errors: string[] = [], writes: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes.push(request.url()); });
  const history = Array.from({ length: 366 }, (_, index) => {
    const date = new Date(Date.UTC(2025, 8, 18 + index)).toISOString().slice(0, 10);
    const current = index === 365, provider = index >= 362, missing = index === 361;
    const value = missing ? null : current ? 2000 : Math.round((1000 + index + Math.sin(index / 8) * 20) * 100) / 100;
    return { date, net_worth: value, assets: value, liabilities: 0, known_accounts: missing ? 0 : 1,
      missing_accounts: missing ? 1 : 0, complete: !missing, estimated: !current, carried_accounts: 0,
      coverage_key: 'demo', valuation_key: current ? 'recorded' : provider ? 'provider' : 'daily',
      valuation_basis: current ? 'recorded_balance' : provider ? 'provider_estimate' : 'daily_holdings',
      reconstructed_accounts: !provider && !missing ? 1 : 0, source_break: current };
  });
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    generated_at: '2026-09-18T12:00:00Z', currency: 'GBP', currencies: ['GBP'], range: 'all', selected_account_ids: ['demo'],
    accounts: [{ id: 'demo', name: 'Synthetic investment', institution: 'Demo', kind: 'investment', source: 'snaptrade', currency: 'GBP',
      default_included: true, exclusion_reason: null, native_value: 2000, value: 2000, as_of: '2026-09-18', stale: false, estimated: false, history_points: 366 }],
    summary: { net_worth: 2000, assets: 2000, liabilities: 0, known_accounts: 1, selected_accounts: 1, missing_accounts: 0,
      stale_accounts: 0, partial: false, change: null, change_pct: null, change_comparable: false },
    history: { mode: 'comparable_cohort', account_ids: ['demo'], excluded_account_ids: [], account_count: 1, selected_accounts: 1,
      start_date: history[0].date, end_date: history[365].date, end_value: 2000, current_value: 2000, change: null, change_pct: null,
      change_comparable: false, points: history, coverage_note: 'Synthetic daily holdings, an unavailable day, provider estimates and current recorded value.' },
    points: history, warnings: [], methodology: [], cache: { saved_at: null, stale: false, refresh_error: null },
  } }));
  await page.goto('/#net-worth');
  const chart = page.getByRole('group', { name: 'Interactive net assets chart', exact: true });
  const readout = page.getByLabel('Inspected net assets', { exact: true });
  await expect(readout).toContainText('£2,000.00');
  await chart.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('daily-history-full-range.png'), fullPage: false });
  await page.getByText('Choose exact dates', { exact: true }).click();
  await page.getByLabel('History start date', { exact: true }).fill('2026-09-12');
  await chart.focus();
  await chart.press('Home');
  await expect(readout).toContainText('12 Sept 2026');
  await expect(readout).toContainText('reconstructed estimate');
  await chart.press('ArrowRight');
  await expect(readout).toContainText('13 Sept 2026');
  await chart.press('ArrowRight');
  await expect(readout).toContainText('15 Sept 2026');
  await chart.press('End');
  await expect(readout).toContainText('£2,000.00');
  await page.screenshot({ path: testInfo.outputPath('daily-history-source-transitions.png'), fullPage: false });
  await page.getByText('Dated values & chart details', { exact: true }).click();
  await expect(page.getByText('Broker-provided history · estimate', { exact: true })).toHaveCount(3);
  await expect(page.getByText('Recorded balance · valuation source changed', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
