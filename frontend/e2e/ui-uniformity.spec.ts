import { expect, test } from '@playwright/test';

const routes = [
  ['overview', 'Overview'], ['banking', 'Banks & accounts'], ['net-worth', 'Net assets'],
  ['analytics', 'Spending'], ['transactions', 'Transactions'], ['planning', 'Planning'],
  ['connected-portfolio', 'Investments'], ['merchants', 'People & merchants'],
  ['anomalies', 'Review'], ['transfers', 'Transfers'], ['recurring', 'Recurring'],
  ['advice', 'Savings'], ['settings', 'Settings'],
] as const;

for (const [route, title] of routes) {
  test(`${title} keeps readable roles and touch targets without page overflow`, async ({ page }, info) => {
    if ((page.viewportSize()?.width ?? 1440) < 640) await page.setViewportSize({ width: 320, height: 700 });
    await page.goto(`/#${route}`);
    await expect(page.getByRole('heading', { level: 1, name: title, exact: true })).toHaveCSS('font-size', '24px');
    await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    const undersized = await page.evaluate(() => {
      if (!matchMedia('(pointer: coarse)').matches) return [];
      return [...document.querySelectorAll<HTMLElement>('main button, main summary')]
        .filter(element => element.getClientRects().length && getComputedStyle(element).visibility === 'visible')
        .filter(element => element.getBoundingClientRect().height < 43.5)
        .map(element => element.getAttribute('aria-label') || element.textContent?.trim());
    });
    expect(undersized, 'Every rendered phone action needs a usable touch height').toEqual([]);
    // Viewport captures preserve touch emulation on this Chromium host.
    await page.screenshot({ path: info.outputPath(`${route}-uniformity.png`) });
  });
}

test('collapsed bank disclosures align their titles without empty framing', async ({ page }) => {
  await page.goto('/#banking');
  for (const title of ['Connection health', 'How bank feeds and statement history work together']) {
    const text = page.getByText(title, { exact: true });
    const summary = text.locator('xpath=ancestor::summary');
    const details = summary.locator('..');
    await expect(text).toHaveCSS('font-size', '14px');
    const labelBox = (await text.boundingBox())!;
    const rowBox = (await summary.boundingBox())!;
    const cardBox = (await details.boundingBox())!;
    expect(rowBox.height).toBeGreaterThanOrEqual(44);
    expect(Math.abs(labelBox.y + labelBox.height / 2 - rowBox.y - rowBox.height / 2)).toBeLessThanOrEqual(1);
    expect(cardBox.height - rowBox.height).toBeLessThanOrEqual(3);
    await summary.focus();
    await summary.press('Enter');
    await expect(details).toHaveAttribute('open', '');
    await summary.press('Enter');
    await expect(details).not.toHaveAttribute('open', '');
  }
});

test('short dialogs keep actions visible and native validation blocks an invalid rule', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 400 });
  await page.goto('/#transactions');
  await page.getByLabel('Search transactions', { exact: true }).fill('QA Unclassified 01');
  const opener = page.getByRole('button', { name: 'Review QA Unclassified 01', exact: true });
  await opener.click();
  let dialog = page.getByRole('dialog', { name: 'Review transaction', exact: true });
  await expect(dialog.getByRole('button', { name: 'Save review', exact: true })).toBeInViewport();
  await dialog.getByRole('button', { name: 'Use this name for matching payments…', exact: true }).click();
  dialog = page.getByRole('dialog', { name: 'Teach Am I Broke? this name', exact: true });
  const save = dialog.getByRole('button', { name: 'Save exact rule', exact: true });
  const name = dialog.getByLabel('Display name', { exact: true });
  await expect(save).toBeEnabled();
  await expect(save).toBeInViewport();
  let submissions = 0;
  await page.route('**/api/merchants/classify', route => {
    submissions += 1;
    return route.fulfill({ status: 503, json: { detail: 'Synthetic rule service unavailable' } });
  });
  // Exercise native required validation without invalidating the accepted API
  // preview; external footer buttons must still submit their associated form.
  await name.evaluate(element => { (element as HTMLInputElement).value = ''; });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(name).toBeFocused();
  expect(submissions).toBe(0);
  await name.fill('Synthetic retained draft');
  await expect(save).toBeEnabled();
  await expect(save).toBeInViewport();
  await save.click();
  await expect(dialog.getByRole('alert')).toContainText('Synthetic rule service unavailable');
  await expect(name).toHaveValue('Synthetic retained draft');
  expect(submissions).toBe(1);
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Review transaction', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('saved-name fields fit inside the dialog at 320px', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto('/#merchants');
  await page.getByRole('button', { name: 'Saved merchants & aliases', exact: true }).click();
  await page.getByLabel('Search people and merchants', { exact: true }).fill('QA Cafe');
  await page.getByRole('button', { name: 'Edit QA Cafe', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Edit QA Cafe', exact: true });
  await expect(dialog.getByLabel('Display name', { exact: true })).toBeVisible();
  const clipped = await dialog.evaluate(element => {
    const panel = element.getBoundingClientRect();
    return [...element.querySelectorAll<HTMLElement>('input:not([type="checkbox"]), select, textarea, button')]
      .filter(control => control.getClientRects().length)
      .filter(control => {
        const box = control.getBoundingClientRect();
        return box.left < panel.left || box.right > panel.right;
      }).map(control => control.getAttribute('aria-label') || control.textContent?.trim());
  });
  expect(clipped, 'Contained horizontal clipping must not be hidden by the modal shell').toEqual([]);
  await expect(dialog.getByRole('button', { name: 'Save rule', exact: true })).toBeInViewport();
});

for (const width of [320, 390]) test(`enlarged phone text keeps navigation and keyboard focus reachable at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 700 });
  await page.goto('/#transactions');
  await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
  const picker = page.getByRole('combobox', { name: 'Go to page', exact: true });
  await expect(picker).toBeInViewport();
  expect((await picker.boundingBox())!.width).toBeGreaterThanOrEqual(100);
  const activity = page.getByRole('navigation', { name: 'Activity sections', exact: true });
  for (const button of await activity.getByRole('button').all()) await expect(button).toHaveCSS('white-space', 'nowrap');
  await page.locator('main').focus();
  for (let i = 0; i < 18; i++) {
    await page.keyboard.press('Tab');
    await expect.poll(() => page.evaluate(() => {
      const element = document.activeElement as HTMLElement;
      const box = element.getBoundingClientRect();
      const x = (Math.max(0, box.left) + Math.min(innerWidth, box.right)) / 2;
      const y = (Math.max(0, box.top) + Math.min(innerHeight, box.bottom)) / 2;
      const hit = document.elementFromPoint(x, y);
      return element === hit || element.contains(hit);
    }), { message: 'Focused actions must stay clear of sticky navigation and horizontal clipping' }).toBe(true);
  }
  await activity.getByRole('button', { name: 'Names', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'People & merchants', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});
