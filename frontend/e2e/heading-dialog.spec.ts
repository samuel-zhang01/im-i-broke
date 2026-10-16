import { expect, test } from '@playwright/test';

test('long saved names wrap safely in directory and dialog titles', async ({ page, request }, info) => {
  if (info.project.name === 'mobile') await page.setViewportSize({ width: 320, height: 844 });
  const prefix = `QA ${info.project.name} `;
  const suffix = ' <img src=x onerror=alert(1)>';
  const name = prefix + 'LongMerchantName'.repeat(5).slice(0, 120 - prefix.length - suffix.length) + suffix;
  const created = await request.post('/api/merchants', {
    headers: { 'X-Ledger-Request': '1' },
    data: { canonical_name: name, kind: 'merchant', category: 'Shopping', spending_treatment: 'normal', aliases: [{ pattern: `QA long title ${info.project.name}`, match_mode: "exact", context: "spending" }] },
  });
  expect(created.ok(), await created.text()).toBe(true);
  await page.goto('/#merchants');
  await page.getByRole('button', { name: /^Saved merchants & aliases/ }).click();
  await page.getByLabel('Search people and merchants', { exact: true }).fill(name);
  const opener = page.getByRole('button', { name: `View ${name} activity`, exact: true });
  await expect(opener).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await opener.click();
  const dialog = page.getByRole('dialog', { name, exact: true });
  const title = dialog.getByRole('heading', { name, exact: true });
  await expect(title).toBeVisible();
  await expect(dialog.locator('img[src="x"]')).toHaveCount(0);
  const box = (await title.boundingBox())!;
  const closeBox = (await dialog.getByRole('button', { name: 'Close dialog' }).boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(closeBox.x);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  expect(await dialog.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('long-title.png') });
  await dialog.getByRole('button', { name: 'Close dialog' }).click();
  await expect(opener).toBeFocused();
});

test('dialog keyboard loop excludes collapsed, hidden and disabled controls', async ({ page }) => {
  await page.goto('/#transactions');
  await expect(page.getByRole('heading', { level: 1, name: 'Transactions', exact: true })).toHaveCSS('font-size', '24px');
  await page.getByLabel('Search transactions', { exact: true }).fill('QA Unclassified 01');
  const opener = page.getByRole('button', { name: 'Review QA Unclassified 01', exact: true });
  await opener.click();
  const dialog = page.getByRole('dialog', { name: 'Review transaction', exact: true });
  await expect(dialog.getByRole('button', { name: 'Close dialog' })).toBeVisible();
  // Exercise the shared dialog contract with controls that native tab navigation skips.
  await dialog.evaluate(element => {
    const group = document.createElement('div');
    const details = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Extra keyboard evidence';
    const concealed = document.createElement('button'); concealed.textContent = 'Collapsed action';
    details.append(summary, concealed);
    const last = document.createElement('button'); last.textContent = 'Last visible action';
    const hidden = document.createElement('button'); hidden.textContent = 'Hidden action'; hidden.style.visibility = 'hidden';
    const disabled = document.createElement('fieldset'); disabled.disabled = true;
    const child = document.createElement('button'); child.textContent = 'Disabled action'; disabled.append(child);
    group.append(details, last, hidden, disabled);
    element.lastElementChild!.append(group);
  });
  const first = dialog.getByRole('button', { name: 'Close dialog' });
  const last = dialog.getByRole('button', { name: 'Last visible action' });
  await first.focus(); await page.keyboard.press('Shift+Tab'); await expect(last).toBeFocused();
  await page.keyboard.press('Tab'); await expect(first).toBeFocused();
  await dialog.getByText('Extra keyboard evidence', { exact: true }).focus();
  await page.keyboard.press('Tab'); await expect(last).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});
