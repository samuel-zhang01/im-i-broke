import { expect, test, type Page } from '@playwright/test';
import { revealMobileControls } from './navigation-helpers';

const patterns = Array.from({ length: 37 }, (_, index) => ({
  key: `navigation-${index}`, merchant: `QA Payment ${String(index).padStart(2, '0')}`, account: 'QA Current', category: 'Bills',
  cadence: 'monthly', cadence_days: 30, typical_amount: 100 - index, current_amount: 100 - index,
  monthly_cost: 100 - index, first_seen: '2026-01-01', last_seen: '2026-09-01',
  n_occurrences: 9, txn_ids: [], next_expected: null, pattern_type: 'regular_payment',
  forecast_eligible: true, status: 'stable', payments: [],
}));

async function mockPatterns(page: Page) {
  await page.route('**/api/recurring?**', route => route.fulfill({ json: { patterns, candidates: [] } }));
  await page.goto('/#recurring');
  await expect(page.getByRole('button', { name: 'View payments for QA Payment 00 in QA Current', exact: true })).toBeVisible();
}

test('recurring pagination returns to new records and sorting restarts the list', async ({ page }, info) => {
  await mockPatterns(page);
  const paging = page.getByRole('navigation', { name: 'Recurring pattern pages', exact: true });
  await paging.getByRole('button', { name: 'Next page →', exact: true }).click();
  const records = page.getByRole('region', { name: 'Recurring payment records', exact: true });
  await expect(records).toBeFocused();
  await expect(paging).toContainText('19–36 of 37');
  const firstPayment = page.getByRole('button', { name: 'View payments for QA Payment 18 in QA Current', exact: true });
  await expect(firstPayment).toBeInViewport();

  await revealMobileControls(page, 'Payment filters & sort');
  await page.getByRole('button', { name: 'Reverse recurring sort order', exact: true }).click();
  await expect(paging).toContainText('1–18 of 37');
  await expect(page.getByRole('button', { name: /^View payments for QA Payment/ }).first()).toHaveAccessibleName('View payments for QA Payment 36 in QA Current');
  if (info.project.name === 'desktop') {
    await paging.getByRole('button', { name: 'Next page →', exact: true }).click();
    await page.getByRole('button', { name: 'Merchant & account', exact: true }).click();
    await expect(paging).toContainText('1–18 of 37');
  }
  await page.screenshot({ path: info.outputPath('recurring-pagination.png') });
});

test('recurring empty filters recover with keyboard focus and search ignores outer whitespace', async ({ page }, info) => {
  await mockPatterns(page);
  const search = page.getByRole('searchbox', { name: 'Find a payment', exact: true });
  await search.fill('  QA Payment 00  ');
  await expect(page.getByRole('button', { name: 'View payments for QA Payment 00 in QA Current', exact: true })).toBeVisible();
  await revealMobileControls(page, 'Payment filters & sort');
  await page.getByRole('combobox', { name: 'Evidence', exact: true }).selectOption('ended');
  await expect(page.getByText('No patterns match these filters.', { exact: true })).toBeVisible();
  if (info.project.name === 'mobile') await page.getByRole('button', { name: 'Payment filters & sort', exact: true }).click();
  await page.getByRole('button', { name: 'Clear payment filters', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(search).toBeFocused();
  await expect(page.getByRole('button', { name: /^View payments for QA Payment/ })).toHaveCount(18);
  await expect(page.getByRole('navigation', { name: 'Recurring pattern pages' })).toContainText('1–18 of 37');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('recurring-filter-recovery.png') });
});
