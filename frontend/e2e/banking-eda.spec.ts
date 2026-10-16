import { test, expect } from '@playwright/test';

test('spending groups retain breakdown, cashflow and account evidence', async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await page.goto('/#analytics');
  await expect(page.getByRole('heading', { level: 1, name: 'Spending', exact: true })).toBeVisible();
  const sections = page.getByRole('group', { name: 'Spending sections', exact: true });
  await expect(sections.getByRole('button', { name: 'Categories & merchants', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('heading', { name: 'Merchant breakdown', exact: true })).toBeVisible();
  await expect(page.getByText('Monthly ledger', { exact: true })).toHaveCount(0);
  await sections.getByRole('button', { name: 'Cashflow & history', exact: true }).click();
  await expect(page.getByText('Monthly ledger', { exact: true })).toBeVisible();
  await expect(page.getByText('What changed', { exact: true })).toBeVisible();
  await sections.getByRole('button', { name: 'Accounts & data', exact: true }).click();
  await expect(page.getByText('What is excluded', { exact: true })).toBeVisible();
  await expect(page.getByText('Cash flow by account', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.getByRole('button', { name: 'Inspect bank connections', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Banks & accounts', exact: true })).toBeVisible();
});
