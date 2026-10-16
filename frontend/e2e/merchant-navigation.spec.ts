import { expect, test } from '@playwright/test';

test('saved merchant pages reveal new names and clearing empty filters restores focus', async ({ page }, info) => {
  const items = Array.from({ length: 49 }, (_, index) => ({
    id: 1000 + index, canonical_name: `QA Directory ${String(index).padStart(2, '0')}`,
    kind: 'merchant', category: 'Shopping', spending_treatment: 'normal', source_url: null,
    notes: null, enabled: true, aliases: [], n_transactions: index + 1,
  }));
  await page.route('**/api/merchants', route => route.fulfill({ json: { items, suggestions: [], privacy_note: '' } }));
  await page.goto('/#merchants');
  await page.getByRole('button', { name: /^Saved merchants & aliases/ }).click();
  const records = page.getByRole('region', { name: 'Saved merchant records', exact: true });
  await expect(records.getByRole('button', { name: 'View QA Directory 00 activity', exact: true })).toBeVisible();
  const paging = page.getByRole('navigation', { name: 'Saved merchant pages', exact: true });
  await paging.getByRole('button', { name: 'Next page →', exact: true }).click();
  await expect(records).toBeFocused();
  await expect(paging).toContainText('25–48 of 49');
  await expect(records.getByRole('button', { name: 'View QA Directory 24 activity', exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath('merchant-pagination.png') });

  const search = page.getByRole('searchbox', { name: 'Search people and merchants', exact: true });
  await search.fill('unfindable xyzzy');
  await expect(page.getByText('No saved names match these filters.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear directory filters', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(search).toBeFocused();
  await expect(paging).toContainText('1–24 of 49');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('merchant-filter-recovery.png') });
});
