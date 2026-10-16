import { expect, test } from '@playwright/test';

test('planning keeps warning titles visible and opens their evidence with keyboard focus', async ({ page }) => {
  const title = 'Synthetic missing account history';
  const detail = 'This synthetic account has no recent observation. Check its dated evidence before using the scenario.';
  await page.route('**/api/planning?**', async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: {
      ...data,
      wealth: { ...data.wealth, is_partial: true },
      insights: [...data.insights, { severity: 'warning', title, detail }],
    } });
  });
  await page.goto('/#planning');
  await expect(page.getByText('Partial financial picture', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: title })).toBeVisible();
  const evidence = page.getByText(detail, { exact: true });
  await expect(evidence).toBeHidden();
  const disclosure = page.locator('details').filter({ has: page.getByText(/^Planning checks & insights \(/) });
  await expect(disclosure).not.toHaveAttribute('open');
  const action = page.getByRole('button', { name: 'Review planning checks', exact: true });
  await action.focus();
  await action.press('Enter');
  await expect(disclosure).toHaveAttribute('open', '');
  await expect(disclosure.locator('summary')).toBeFocused();
  await expect(evidence).toBeVisible();
  await disclosure.locator('summary').press('Enter');
  await expect(evidence).toBeHidden();
  await expect(page.getByRole('status').filter({ hasText: title })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('opening planning evidence keeps the adjacent closed section compact', async ({ page }) => {
  await page.goto('/#planning');
  const baseline = page.locator('details').filter({ has: page.getByText(/^Baseline behind the maths/) });
  const statements = page.locator('details').filter({ has: page.getByText('Statement checks', { exact: true }) });
  await baseline.locator(':scope > summary').click();
  await expect(baseline.locator('dl')).toBeVisible();
  await expect(statements).not.toHaveAttribute('open');
  const summaryBox = (await statements.locator(':scope > summary').boundingBox())!;
  const sectionBox = (await statements.boundingBox())!;
  expect(summaryBox.height).toBeGreaterThanOrEqual(44);
  expect(sectionBox.height - summaryBox.height).toBeLessThanOrEqual(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});
