import { revealMobileControls } from './navigation-helpers';
import { test, expect, type Page } from '@playwright/test';

async function recordTransactionQueries(page: Page) {
  const invalidRequests: string[] = [];
  await page.route('**/*', route => {
    const request = route.request();
    return new URL(request.url()).hostname === '127.0.0.1' && ['GET', 'HEAD'].includes(request.method()) ? route.continue() : route.abort();
  });
  await page.route('**/api/transactions?**', route => {
    const query = new URL(route.request().url()).searchParams;
    const from = query.get('date_from'), to = query.get('date_to');
    const min = query.get('min_amount'), max = query.get('max_amount');
    if ((from && to && from > to) || (min !== null && max !== null && Number(min) > Number(max))) {
      invalidRequests.push(query.toString());
      return route.fulfill({ status: 422, json: { detail: 'Invalid range reached the API.' } });
    }
    return route.fulfill({ json: { total: 1, offset: 0, limit: Number(query.get('limit') || 50), sum_in: 0, sum_out: -25, items: [
      { id: 90001, account_id: 'qa', account: 'QA Current', txn_date: '2026-09-15', description: 'QA saved transaction', merchant: 'QA saved transaction', amount: -25, category: 'Shopping', transfer_group: null },
    ] } });
  });
  await page.goto('/#transactions');
  await expect(page.getByText('QA saved transaction', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Transaction filters');
  await page.getByRole('button', { name: /^Date & amount/ }).click();
  return invalidRequests;
}

test('invalid dates explain the fix without requesting or showing stale results', async ({ page }) => {
  const invalidRequests = await recordTransactionQueries(page);
  await page.getByLabel('From date', { exact: true }).fill('2026-09-20');
  await page.getByLabel('To date', { exact: true }).fill('2026-09-01');
  await expect(page.getByRole('alert')).toHaveText('Choose an end date on or after the start date.');
  await expect(page.getByLabel('To date', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByText('QA saved transaction', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Correct the filters above to see matching transactions.')).toBeVisible();
  expect(invalidRequests).toEqual([]);
  await page.getByLabel('To date', { exact: true }).fill('2026-09-30');
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByText('QA saved transaction', { exact: true })).toBeVisible();
  expect(invalidRequests).toEqual([]);
});

test('signed amount ranges validate locally and Clear filters restores the list', async ({ page }) => {
  const invalidRequests = await recordTransactionQueries(page);
  await page.getByLabel('Max amount (£)', { exact: true }).fill('-50');
  await page.getByLabel('Min amount (£)', { exact: true }).fill('-10');
  await expect(page.getByRole('alert')).toHaveText('The maximum amount must be at least the minimum amount.');
  await expect(page.getByLabel('Min amount (£)', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByText('QA saved transaction', { exact: true })).toHaveCount(0);
  expect(invalidRequests).toEqual([]);
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await expect(page.getByLabel('Min amount (£)', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('Max amount (£)', { exact: true })).toHaveValue('');
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByText('QA saved transaction', { exact: true })).toBeVisible();
  expect(invalidRequests).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});
