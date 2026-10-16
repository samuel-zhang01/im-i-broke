import { expect, test } from '@playwright/test';
import type { Txn } from '../src/api';

test('transaction pagination reveals and focuses the new records after loading', async ({ page }, info) => {
  const records: Txn[] = Array.from({ length: 121 }, (_, index) => ({
    id: 8000 + index, account_id: 'qa-current', account: 'QA Current',
    txn_date: '2026-09-21', description: `Pagination record ${index}`,
    merchant: null, amount: -(index + 1), balance: null,
    category: 'Shopping', category_source: null, recurring_id: null,
    transfer_group: null, is_anomaly: 0,
  }));
  await page.route('**/api/transactions?**', async route => {
    const params = new URL(route.request().url()).searchParams;
    const offset = Number(params.get('offset') || 0);
    const limit = Number(params.get('limit') || 50);
    // A short response delay reproduces the disabled-pagination focus loss.
    await new Promise(resolve => setTimeout(resolve, 100));
    await route.fulfill({ json: {
      items: records.slice(offset, offset + limit), total: records.length,
      offset, limit, sum_in: 0, sum_out: -7381,
    } });
  });
  await page.goto('/#transactions');
  await expect(page.getByRole('button', { name: 'Review Pagination record 0', exact: true })).toBeVisible();
  const next = page.getByRole('button', { name: 'Next →', exact: true });
  await next.click();
  const start = info.project.name === 'mobile' ? 20 : 50;
  const first = page.getByRole('button', { name: `Review Pagination record ${start}`, exact: true });
  await expect(first).toBeInViewport();
  const region = page.getByRole('region', { name: 'Transaction records', exact: true });
  await expect(region).toBeFocused();
  await expect.poll(() => first.evaluate(element => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return !!hit && (hit === element || element.contains(hit));
  })).toBe(true);
  await page.getByRole('button', { name: '← Prev', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Review Pagination record 0', exact: true })).toBeInViewport();
  await expect(region).toBeFocused();
  await page.screenshot({ path: info.outputPath('transaction-pagination.png') });
});

test('desktop filters stay in one row while touch layouts keep accessible targets', async ({ page }, info) => {
  if (info.project.name === 'desktop') {
    for (const width of [1280, 1440, 1920]) {
      await page.setViewportSize({ width, height: 960 });
      await page.goto('/#transactions');
      await expect(page.getByRole('button', { name: /^Review QA/ }).first()).toBeVisible();
      const controls = [
        page.getByLabel('Search transactions', { exact: true }),
        page.getByLabel('Account', { exact: true }),
        page.getByLabel('Category', { exact: true }),
        page.getByLabel('Money direction', { exact: true }),
      ];
      const boxes = await Promise.all(controls.map(control => control.boundingBox()));
      expect(boxes.every(Boolean)).toBe(true);
      const top = boxes.map(box => box!.y);
      expect(Math.max(...top) - Math.min(...top), `Filters should share a row at ${width}px`).toBeLessThanOrEqual(2);
      const firstRow = await page.locator('main table tbody tr').first().boundingBox();
      expect(firstRow!.y, `Records should be available without scrolling at ${width}px`).toBeLessThan(400);
      const nav = page.getByRole('navigation', { name: 'Activity sections', exact: true });
      const navBox = await nav.boundingBox();
      const buttonWidths = await nav.getByRole('button').evaluateAll(buttons => buttons.map(button => button.getBoundingClientRect().width));
      expect(buttonWidths.reduce((total, width) => total + width, 0)).toBeLessThan(navBox!.width * 0.7);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: info.outputPath(`desktop-density-${width}.png`) });
    }
  } else {
    await page.setViewportSize({ width: 1024, height: 960 });
    await page.goto('/#transactions');
    await expect(page.getByLabel('Account', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
    for (const control of [
      page.getByLabel('Search transactions', { exact: true }),
      page.getByLabel('Account', { exact: true }),
      page.getByLabel('Category', { exact: true }),
      page.getByLabel('Money direction', { exact: true }),
      page.getByRole('button', { name: 'Spending stats', exact: true }),
      page.getByRole('navigation', { name: 'Activity sections', exact: true }).getByRole('button').first(),
    ]) {
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    await page.setViewportSize({ width: 320, height: 844 });
    await page.goto('/#transactions');
    const toggle = page.getByRole('button', { name: 'Transaction filters', exact: true });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.click();
    await expect(page.getByLabel('Account', { exact: true })).toBeVisible();
    await expect(page.getByLabel('Category', { exact: true })).toBeVisible();
    await page.getByLabel('Money direction', { exact: true }).selectOption('out');
    await toggle.click();
    await expect(toggle).toHaveAccessibleDescription('1 active · All accounts');
    await expect(page.getByLabel('Money direction', { exact: true })).toBeHidden();
    expect((await toggle.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: info.outputPath('touch-density-320.png') });
  }
});
