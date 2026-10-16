import { expect, test } from '@playwright/test';

test('keyboard focus stays clear of fixed navigation in portrait and landscape', async ({ page }, info) => {
  const sizes = info.project.name === 'mobile' ? [{ width: 390, height: 844 }, { width: 844, height: 390 }] : [{ width: 1440, height: 960 }];
  for (const size of sizes) {
    await page.setViewportSize(size);
    await page.goto('/#transactions');
    await expect(page.getByLabel('Search transactions', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Review QA/ }).first()).toBeVisible();
    await page.locator('main').focus();
    for (let i = 0; i < 35; i++) {
      await page.keyboard.press('Tab');
      const state = await page.evaluate(() => {
        const active = document.activeElement;
        if (!active || !document.querySelector('main')?.contains(active)) return null;
        const box = active.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return { name: active.getAttribute('aria-label') || active.textContent,
          coveredByNavigation: !!hit?.closest('aside,nav[aria-label="Quick navigation"]') };
      });
      if (!state) break;
      expect(state.coveredByNavigation, `Focused control hidden: ${state.name}`).toBe(false);
    }
    await page.screenshot({ path: info.outputPath(`focus-${size.width}.png`) });
  }
});

test('transfer pagination starts at the new records and filters recover from empty results', async ({ page }, info) => {
  const records = Array.from({ length: 61 }, (_, i) => ({
    group: `transfer-${i}`, date: '2026-09-21', from: `Source account ${i}`, to: 'Savings',
    from_desc: `Own transfer ${i}`, to_desc: 'Received', amount: i + 1, matched: true,
  }));
  await page.route('**/api/transfers', route => route.fulfill({ json: records }));
  await page.goto('/#transfers');
  const region = page.getByRole('region', { name: 'Transfer records', exact: true });
  await expect(region.getByText('Source account 0', { exact: false })).toBeVisible();
  const evidence = region.locator('details').first();
  await expect(evidence).not.toHaveAttribute('open');
  if (info.project.name === 'desktop') expect((await region.getByRole('row').nth(1).boundingBox())!.height).toBeLessThanOrEqual(64);
  await evidence.locator('summary').click();
  await expect(evidence.getByText('Outgoing: Own transfer 0', { exact: true })).toBeVisible();
  await expect(evidence.getByText('Incoming: Received', { exact: true })).toBeVisible();
  await evidence.locator('summary').click();
  await expect(evidence.getByText('Outgoing: Own transfer 0', { exact: true })).toBeHidden();
  await region.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await page.getByRole('button', { name: 'Next page →', exact: true }).click();
  await expect(region).toBeFocused();
  await expect.poll(() => region.evaluate(element => element.scrollTop)).toBe(0);
  await expect(region.getByText('Source account 30', { exact: false })).toBeVisible();
  await page.getByLabel('Search transfer accounts or descriptions').fill('No matching account');
  await expect(page.getByText('No transfers match this view.')).toBeVisible();
  await page.getByRole('button', { name: 'Clear transfer filters' }).click();
  await expect(page.getByLabel('Search transfer accounts or descriptions')).toHaveValue('');
  await expect(page.getByLabel('Search transfer accounts or descriptions')).toBeFocused();
  await expect(page.getByRole('navigation', { name: 'Transfer pages' })).toContainText('1–30 of 61');
  await expect.poll(() => region.evaluate(element => getComputedStyle(element.closest('.card')!).padding)).toBe('0px');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('transfer-records.png') });
});

test('collapsed mobile filter descriptions expose the current selection', async ({ page }, info) => {
  await page.goto('/#transactions');
  await expect(page.getByLabel('Search transactions', { exact: true })).toBeVisible();
  if (info.project.name !== 'mobile') return;
  const toggle = page.getByRole('button', { name: 'Transaction filters', exact: true });
  await expect(toggle).toHaveAccessibleDescription('All accounts · all activity');
  await toggle.click();
  await page.getByLabel('Money direction', { exact: true }).selectOption('out');
  await toggle.click();
  await expect(toggle).toHaveAccessibleDescription('1 active · All accounts');
  await expect(page.getByLabel('Money direction', { exact: true })).toBeHidden();
});
