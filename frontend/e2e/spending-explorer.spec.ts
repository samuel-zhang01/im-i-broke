import { test, expect } from '@playwright/test';

test('weekly spending and merchants follow category selection', async ({ page, request }) => {
  // A preceding preference-persistence test may select the empty credit account.
  // Establish this scenario's own filter scope before checking category evidence.
  const saved = await request.put('/api/preferences/spending', {
    headers: { 'X-Ledger-Request': '1' }, data: { query: { exclude_outliers: false } },
  });
  expect(saved.ok()).toBe(true);
  await page.goto('/#analytics');
  const explorer = page.getByRole('region', { name: 'Spending explorer' });
  await expect(explorer.getByRole('heading', { name: 'Monthly spending · selected categories' })).toBeVisible();
  await explorer.getByRole('button', { name: /^Legal services/ }).click();
  await expect(explorer.getByRole('status')).toContainText('1 categories selected: Legal services');
  await expect(explorer.locator('li')).toContainText(['QA Legal']);
  // Other workflow tests can correctly classify additional legal payments.
  const merchantNames = await explorer.locator('li').allTextContents();
  expect(merchantNames.length).toBeGreaterThan(0);
  await explorer.getByRole('button', { name: 'Weeks', exact: true }).click();
  await expect(explorer.getByRole('heading', { name: 'Weekly spending · selected categories' })).toBeVisible();
  await expect(explorer.getByRole('button', { name: 'Weeks', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(explorer.locator('li')).toHaveText(merchantNames);
  await explorer.getByRole('button', { name: /^Legal services/ }).click();
  await expect(explorer.getByText('Select a category above to see its trend.')).toBeVisible();
  await expect(explorer.getByText('No merchants match this selection.')).toBeVisible();
  await explorer.getByRole('button', { name: 'All categories', exact: true }).click();
  await explorer.getByRole('button', { name: 'Months', exact: true }).click();
  await expect(explorer.getByRole('heading', { name: 'Monthly spending · selected categories' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Where you spend most' })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});
