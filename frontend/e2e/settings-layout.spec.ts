import { test, expect } from '@playwright/test';
import { navigateFinance } from './navigation-helpers';

test('settings and spending keep the mobile viewport focused on useful content', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto('/#settings');
  await expect(page.getByRole('heading', { name: 'Account & security' })).toBeVisible();
  await navigateFinance(page, 'analytics', 'Spending');
  await expect(page.getByText('Recorded spending', { exact: true })).toBeVisible();
  const stats = await page.getByText('Recorded spending', { exact: true }).boundingBox();
  expect(stats!.y).toBeLessThan(330);
  await expect(page.getByLabel('Spending account')).toBeHidden();
  await page.getByText(/^Filters ·/).click();
  await expect(page.getByLabel('Spending account')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.getByText(/^Filters ·/).click();
  await page.screenshot({ path: `../output/playwright/spending-enhanced-${info.project.name}.png`, fullPage: false });
  if (info.project.name === 'mobile') {
    await page.getByRole('navigation', { name: 'Quick navigation' }).getByRole('button', { name: 'Activity' }).click();
    await expect(page).toHaveURL(/#transactions$/);
    await expect(page.getByRole('button', { name: 'Spending stats' })).toBeVisible();
  }
  expect(errors).toEqual([]);
});
