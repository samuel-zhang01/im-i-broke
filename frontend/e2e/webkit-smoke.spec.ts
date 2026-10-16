import { expect, test } from '@playwright/test';
import { navigateFinance, revealMobileControls } from './navigation-helpers';

test('navigation, mobile filters and history work in the browser engine', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto('/#net-worth');
  await expect(page.getByRole('heading', { level: 1, name: 'Net assets', exact: true })).toBeVisible();
  await navigateFinance(page, 'transactions', 'Transactions');
  await expect(page.getByLabel('Search transactions', { exact: true })).toBeVisible();
  const mobile = (page.viewportSize()?.width ?? 1440) < 640;
  if (mobile) {
    // Native iOS select painting used a light surface behind white text.
    // Keep its native interaction, with an explicitly painted dark control.
    const picker = page.getByRole('combobox', { name: 'Go to page', exact: true });
    await expect(picker).toHaveCSS('appearance', 'none');
    await expect(picker).toHaveCSS('background-color', 'rgb(54, 62, 55)');
    await expect(picker).toHaveCSS('color', 'rgb(255, 255, 255)');
  }
  if (mobile) await expect(page.getByLabel('Money direction', { exact: true })).toBeHidden();
  await revealMobileControls(page, 'Transaction filters');
  await page.getByLabel('Money direction', { exact: true }).selectOption('out');
  if (mobile) {
    await page.getByRole('button', { name: 'Transaction filters', exact: true }).click();
    await expect(page.getByLabel('Money direction', { exact: true })).toBeHidden();
    await expect(page.getByRole('button', { name: 'Transaction filters', exact: true })).toHaveAccessibleDescription('1 active · All accounts');
  }
  await navigateFinance(page, 'banking', 'Banks & accounts');
  await page.goBack();
  await expect(page.getByRole('heading', { level: 1, name: 'Transactions', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
  await page.screenshot({ path: info.outputPath('navigation-and-filters.png') });
});

test('transaction review remains usable at the mobile keyboard height and restores focus', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#transactions');
  const opener = page.getByRole('button', { name: /^Review QA/ }).first();
  await expect(opener).toBeVisible();
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Review transaction', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Save review', exact: true })).toBeVisible();
  if ((page.viewportSize()?.width ?? 1440) < 640) await page.setViewportSize({ width: 390, height: 460 });
  const save = dialog.getByRole('button', { name: 'Save review', exact: true });
  await save.scrollIntoViewIfNeeded();
  await expect(save).toBeInViewport();
  expect(await dialog.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('transaction-review.png') });
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  expect(errors).toEqual([]);
});
