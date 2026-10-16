import { expect, test } from '@playwright/test';

test('net assets starts at one month and mobile account controls expand accessibly', async ({ page }, info) => {
  const queries: string[] = [];
  page.on('request', request => { if (request.url().includes('/api/wealth?')) queries.push(request.url()); });
  await page.goto('/#net-worth');
  await expect(page.getByRole('button', { name: '1M', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(queries.length).toBeGreaterThan(0);
  expect(new URL(queries[0]).searchParams.get('range')).toBe('1m');
  const toggle = page.getByRole('button', { name: 'Accounts & currency', exact: true });
  if (info.project.name === 'mobile') {
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.focus();
    await toggle.press('Enter');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await toggle.press('Space');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  } else await expect(toggle).toBeHidden();
  await page.getByRole('group', { name: 'Net assets time range', exact: true }).getByRole('button', { name: 'All', exact: true }).click();
  await expect.poll(() => new URL(queries.at(-1)!).searchParams.get('range')).toBe('all');
});

test('mobile filters preserve selection when collapsed and remain visible on desktop resize', async ({ page }, info) => {
  await page.goto('/#transactions');
  const search = page.getByRole('textbox', { name: 'Search transactions', exact: true });
  const direction = page.getByRole('combobox', { name: 'Money direction', exact: true });
  const toggle = page.getByRole('button', { name: 'Transaction filters', exact: true });
  await expect(search).toBeVisible();
  if (info.project.name === 'mobile') {
    await expect(direction).toBeHidden();
    await toggle.click();
  }
  await direction.selectOption('out');
  if (info.project.name === 'mobile') {
    await toggle.click();
    await expect(direction).toBeHidden();
    await expect(toggle).toContainText('1 active');
    await expect(search).toBeVisible();
    await page.setViewportSize({ width: 1100, height: 844 });
    await expect(direction).toBeVisible();
    await expect(direction).toHaveValue('out');
    await page.setViewportSize({ width: 320, height: 844 });
    await expect(direction).toBeHidden();
    await toggle.click();
    await expect(direction).toHaveValue('out');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  } else await expect(toggle).toBeHidden();
});
