import { expect, test } from '@playwright/test';
import { wealth } from './fixtures/wealth';

test('net assets follows background revisions and changes in another tab', async ({ page }) => {
  let value = 3400; let revision = 'one'; let polls = 0; let posts = 0;
  page.on('request', request => { if (request.url().includes('/api/') && request.method() === 'POST') posts++; });
  await page.route('**/api/wealth?**', route => route.fulfill({ json: { ...wealth, summary: { ...wealth.summary, net_worth: value } } }));
  await page.route('**/api/revision', route => { polls++; return route.fulfill({ json: { revision } }); });
  await page.goto('/#net-worth');
  const total = page.getByLabel('Net assets value', { exact: true });
  await expect(total).toHaveText('£3,400.00');
  await expect.poll(() => polls).toBeGreaterThan(0);
  value = 4400; revision = 'two';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(total).toHaveText('£4,400.00');
  value = 5400;
  await page.evaluate(() => { const channel = new BroadcastChannel('ledger-data'); channel.postMessage('changed'); channel.close(); });
  await expect(total).toHaveText('£5,400.00');
  expect(posts).toBe(0);
});

test('new banks needing account setup are visible in net assets', async ({ page }) => {
  await page.route('**/api/wealth?**', route => route.fulfill({ json: { ...wealth, summary: { ...wealth.summary, partial: true, unmapped_bank_accounts: 2 } } }));
  await page.goto('/#net-worth');
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  await page.getByRole('button', { name: 'Review bank accounts', exact: true }).click();
  await expect(page).toHaveURL(/#banking$/);
});

test('brokerage directory bundles every published logo and handles unknown institutions', async ({ page }, info) => {
  await page.route('**/api/connections', route => route.fulfill({ json: { providers: [], connections: [] } }));
  await page.route('**/api/connections/authorization/providers', route => route.fulfill({ json: { providers: [] } }));
  await page.goto('/#settings');
  await page.getByText('Provider connections', { exact: true }).click();
  await page.getByText('Browse supported brokerages', { exact: true }).click();
  const directory = page.getByRole('list', { name: 'Supported brokerage directory' });
  await expect(directory.getByRole('listitem')).toHaveCount(43);
  await expect.poll(() => directory.locator('img').evaluateAll(images => images.every(image => {
    const img = image as HTMLImageElement;
    return img.complete && img.naturalWidth > 0 && new URL(img.src).origin === location.origin;
  }))).toBe(true);
  await expect(directory.locator('img')).toHaveCount(43);
  await page.getByLabel('Find a brokerage').fill('Schwab');
  await expect(directory.getByRole('listitem')).toHaveCount(1);
  await expect(directory).toContainText('Schwab');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('brokerage-directory.png'), fullPage: true });
  await page.getByLabel('Find a brokerage').fill('Future institution');
  await expect(page.getByText(/No catalog match/)).toBeVisible();
});
