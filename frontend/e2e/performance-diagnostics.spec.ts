import { expect, test } from '@playwright/test';
import { navigateFinance } from './navigation-helpers';

test('private diagnostics show actual browser support and measured values after delayed loading', async ({ page }, info) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  // Keep the geometry fixture explicit: an unconfigured server or unknown
  // balance legitimately inserts extra warning cards before the bank list.
  const counts = { cached: 0, imported: 0, matched: 0, pending: 0, review: 0 };
  await page.route('**/api/banking', async route => {
    await pending;
    await route.fulfill({ json: { configured: true, accounts: [], local_accounts: [], institutions: [], totals_by_currency: {},
      sync: { last_attempt_at: null, last_success_at: null, error: null, running: false, interval_seconds: 3600 },
      stats: { accounts: 0, mapped: 0, ...counts } } });
  });
  await page.goto('/#banking');
  const skeleton = page.getByRole('status', { name: 'Opening your banks and saved history…', exact: true });
  await expect(skeleton).toBeVisible();
  await expect(page.getByText('Performance diagnostics', { exact: true })).toHaveCount(0);
  const startY = await skeleton.locator('.skeleton-block').first().evaluate(element => element.getBoundingClientRect().top + scrollY);
  await page.waitForTimeout(250);
  release();
  await expect(page.getByLabel('Find bank or account', { exact: true })).toBeVisible();
  const headingY = await page.getByRole('heading', { name: 'Your banks', exact: true }).evaluate(element => element.getBoundingClientRect().top + scrollY);
  expect(Math.abs(headingY - startY)).toBeLessThanOrEqual(2);
  const support = await page.evaluate(() => {
    const types = PerformanceObserver.supportedEntryTypes;
    const timing = window as Window & { PerformanceEventTiming?: { prototype: object } };
    return { LCP: types.includes('largest-contentful-paint'), CLS: types.includes('layout-shift'),
      INP: types.includes('event') && Boolean(timing.PerformanceEventTiming && 'interactionId' in timing.PerformanceEventTiming.prototype) };
  });
  await navigateFinance(page, 'settings', 'Settings');
  const disclosure = page.locator('details').filter({ has: page.getByText('Performance diagnostics', { exact: true }) });
  await expect(disclosure).not.toHaveAttribute('open');
  await expect(page.locator('[data-performance-metric]')).toHaveCount(0);
  await disclosure.locator('summary').click();
  await expect(page.locator('[data-performance-metric]')).toHaveCount(3);
  for (const [name, supported] of Object.entries(support)) {
    const value = page.locator(`[data-performance-metric="${name}"] [data-performance-value]`);
    if (!supported) await expect(value).toHaveText('Not supported by this browser');
    else if (name === 'LCP') await expect(value).toHaveText(/^\d+\.\d{2} s$/);
    else if (name === 'CLS') await expect(value).toHaveText(/^\d+\.\d{3}$/);
    else await expect(value).toHaveText(/^(\d+ ms|No interaction measured yet)$/);
  }
  await expect(page.getByText(/nothing is saved or sent/)).toBeVisible();
  const readings = await page.locator('[data-performance-metric]').allTextContents();
  await info.attach('private-performance-readings.json', { body: JSON.stringify({ support, readings, contentPositionDelta: headingY - startY }), contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('performance-diagnostics.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('a browser without performance observer support does not show invented zeros', async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(window, 'PerformanceObserver', { value: undefined, configurable: true }); });
  await page.goto('/#settings');
  await page.getByText('Performance diagnostics', { exact: true }).click();
  await expect(page.locator('[data-performance-value]')).toHaveText([
    'Not supported by this browser', 'Not supported by this browser', 'Not supported by this browser',
  ]);
});
