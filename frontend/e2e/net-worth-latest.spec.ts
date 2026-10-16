import { expect, test } from '@playwright/test';

import { wealth } from './fixtures/wealth';
const items = Array.from({ length: 10 }, (_, i) => ({ id: 9000 + i, account_id: 'cash', account: 'Demo current', txn_date: `2026-09-${String(21 - i).padStart(2, '0')}`, description: `Recent purchase ${i + 1}`, merchant: null, amount: i === 0 ? 50 : -(i + 1), category: 'Shopping', category_source: null, balance: null, recurring_id: null, transfer_group: null, is_anomaly: 0 }));

test('net assets requests the latest ten alongside balances and keeps account actions compact', async ({ page }, info) => {
  let recentRequested = false;
  let releaseChart!: () => void;
  const chartGate = new Promise<void>(resolve => { releaseChart = resolve; });
  await page.route(/\/assets\/LedgerChart-.*\.js/, async route => { await chartGate; await route.continue(); });
  await page.route('**/api/wealth?**', async route => {
    await new Promise(resolve => setTimeout(resolve, 200));
    await route.fulfill({ json: wealth });
  });
  await page.route('**/api/transactions?**', async route => {
    const query = new URL(route.request().url()).searchParams;
    expect(query.get('limit')).toBe('10'); expect(query.get('sort')).toBe('date'); expect(query.get('order')).toBe('desc');
    recentRequested = true;
    await route.fulfill({ json: { items, total: 30, offset: 0, limit: 10, sum_in: 50, sum_out: -54 } });
  });
  await page.goto('/#net-worth', { waitUntil: 'domcontentloaded' });
  await expect.poll(() => recentRequested).toBe(true);
  // Chart downloading cannot delay the account figures or activity request.
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  const recent = page.getByRole('region', { name: 'Latest transactions', exact: true });
  await expect(recent.getByRole('button', { name: /^Review Recent purchase/ })).toHaveCount(10);
  await expect(recent).toContainText('all accounts · GBP');
  await expect(recent).toContainText('£50.00');
  await expect(recent).toContainText('-£10.00');
  // Account evidence is available before the related recent activity.
  expect(await recent.evaluate(element => !!(element.compareDocumentPosition([...document.querySelectorAll('h2')].find(h => h.textContent?.startsWith('Account breakdown'))!) & Node.DOCUMENT_POSITION_PRECEDING))).toBe(true);
  releaseChart();
  await expect(page.getByRole('group', { name: 'Interactive net assets chart' })).toBeVisible();
  if (info.project.name === 'desktop') {
    const region = page.getByRole('region', { name: 'Net assets account sources' });
    const row = region.getByRole('row').filter({ has: page.getByRole('button', { name: 'Demo portfolio', exact: true }) });
    expect((await row.boundingBox())!.height).toBeLessThanOrEqual(64);
    const open = (await row.getByRole('button', { name: 'Open Demo portfolio' }).boundingBox())!;
    const archive = (await row.getByRole('button', { name: 'Archive Demo portfolio' }).boundingBox())!;
    expect(Math.abs(open.y - archive.y)).toBeLessThan(2);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await recent.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath('latest-transactions.png') });
  await page.unroute('**/api/transactions?**');
  await recent.getByRole('button', { name: 'View activity →' }).click();
  await expect(page).toHaveURL(/#transactions$/);
});

test('latest transaction failures retain net assets and provide an independent retry', async ({ page }) => {
  let fail = true;
  await page.route('**/api/wealth?**', route => route.fulfill({ json: wealth }));
  await page.route('**/api/transactions?**', route => fail ? route.fulfill({ status: 503, json: { detail: 'Unavailable' } }) : route.fulfill({ json: { items: [], total: 0, limit: 10, offset: 0, sum_in: 0, sum_out: 0 } }));
  await page.goto('/#net-worth');
  const recent = page.getByRole('region', { name: 'Latest transactions', exact: true });
  await expect(recent.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  fail = false;
  await recent.getByRole('button', { name: 'Try again' }).click();
  await expect(recent).toContainText('No saved transactions yet.');
  await expect(page.getByRole('heading', { name: /^Account breakdown/ })).toBeVisible();
});
