import { expect, test } from '@playwright/test';
import { navigateFinance, revealMobileControls } from './navigation-helpers';

test('page titles, named content and the skip link follow navigation and history', async ({ page }) => {
  await page.goto('/#transactions');
  await expect(page).toHaveTitle('Transactions · Am I Broke?');
  await expect(page.getByRole('main', { name: 'Transactions', exact: true })).toBeVisible();
  const skip = page.getByRole('link', { name: 'Skip to content', exact: true });
  await skip.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('main')).toBeFocused();
  await navigateFinance(page, 'settings', 'Settings');
  await expect(page).toHaveTitle('Settings · Am I Broke?');
  await page.goBack();
  await expect(page).toHaveTitle('Transactions · Am I Broke?');
  await expect(page.getByRole('main')).toHaveAccessibleName('Transactions');
});

test('loading surfaces honour motion and contrast preferences without changing size', async ({ page, browserName }, info) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/banking', async route => { await pending; await route.continue(); });
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/#banking');
  const loading = page.getByRole('status', { name: 'Opening your banks and saved history…', exact: true });
  const block = loading.locator('.skeleton-block').first();
  try {
    await expect(block).toBeVisible();
    expect(await block.evaluate(element => getComputedStyle(element, '::after').animationName)).toBe('skeleton-sheen');
    const before = await block.boundingBox();
    await page.screenshot({ path: info.outputPath('loading-motion.png') });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    expect(await block.evaluate(element => getComputedStyle(element, '::after').animationName)).toBe('none');
    expect(await block.boundingBox()).toEqual(before);
    if (browserName !== 'webkit') {
      await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' });
      await expect(block).toHaveCSS('border-top-width', '1px');
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: info.outputPath('loading-reduced-motion.png') });
  } finally { release(); }
  await expect(page.getByRole('heading', { name: 'Your banks', exact: true })).toBeVisible();
});

test('narrow settings retain readable controls and recover from an unavailable preferences service', async ({ page }, info) => {
  let fail = true;
  const message = `Preferences temporarily unavailable. Reference: ${'synthetic-reference-'.repeat(15)}`;
  await page.route('**/api/workspace/preferences', route => fail
    ? route.fulfill({ status: 503, json: { detail: message } }) : route.continue());
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('/#settings');
  await page.getByText('Workspace preferences', { exact: true }).click();
  const panel = page.locator('#settings-preferences');
  await expect(panel.getByRole('alert')).toHaveText(`${message}Try again`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  fail = false;
  await panel.getByRole('button', { name: 'Try again' }).click();
  await expect(panel.getByLabel('Reporting currency', { exact: true })).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  if (await page.evaluate(() => matchMedia('(pointer: coarse)').matches)) {
    for (const control of [panel.getByLabel('Reporting currency', { exact: true }), panel.getByLabel('Locale', { exact: true }), page.getByRole('combobox', { name: 'Go to page', exact: true })]) {
      await expect(control).toHaveCSS('font-size', '16px');
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
  }
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: info.outputPath(`settings-preferences-${width}.png`) });
  }
  await page.screenshot({ path: info.outputPath('settings-recovered.png') });
});

test('foreign CSV activity is reachable from Transactions and a bankless account', async ({ page, request }, info) => {
  const headers = { 'X-Ledger-Request': '1' };
  const name = `Euro audit ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers, data: {
    display_name: name, institution: 'Synthetic European Bank', account_type: 'current', currency: 'EUR',
  } });
  expect(created.ok(), await created.text()).toBe(true);
  const account = await created.json();
  const description = `Euro groceries ${info.project.name}`;
  const day = new Date().toISOString().slice(0, 10);
  const data = { account_id: account.id, source_name: 'Synthetic audit CSV',
    csv_text: `Date,Description,Amount\n${day},${description},-20.00\n`,
    mapping: { date_column: 'Date', description_column: 'Description', amount_column: 'Amount', currency: 'EUR' },
  };
  const preview = await request.post('/api/workspace/imports/preview', { headers, data });
  expect(preview.ok(), await preview.text()).toBe(true);
  const committed = await request.post('/api/workspace/imports/commit', { headers, data: { ...data, preview_token: (await preview.json()).preview_token } });
  expect(committed.ok(), await committed.text()).toBe(true);
  expect((await committed.json()).imported_count).toBe(1);
  await page.goto('/#transactions');
  await revealMobileControls(page, 'Transaction filters');
  await page.getByRole('combobox', { name: 'Account', exact: true }).selectOption(account.id);
  await expect(page.getByText('This list only shows GBP.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Open workspace reports →', exact: true }).click();
  await expect(page).toHaveURL(/#settings$/);
  const report = page.locator('#settings-reports');
  await expect(report).toHaveAttribute('open', '');
  await report.getByLabel('From', { exact: true }).fill(day);
  await report.getByLabel('To', { exact: true }).fill(day);
  await report.getByText(/^Amounts and conversion evidence/).click();
  const entry = report.getByRole('row').filter({ hasText: description });
  await expect(entry).toContainText(name);
  await expect(entry).toContainText('€20.00');
  await page.evaluate(id => window.dispatchEvent(new CustomEvent('ledger:navigate', { detail: { page: 'banking', accountId: id } })), account.id);
  await expect(page.getByRole('heading', { name: name, exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Imported activity', exact: true })).toBeVisible();
  await expect(page.getByText('No activity is available in this history yet.', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Open workspace reports →', exact: true }).click();
  await expect(page.locator('#settings-reports')).toHaveAttribute('open', '');
});

test('currency report marks retained totals during a slow filter change and a failed update', async ({ page, request }) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let filtered = false;
  await page.route('**/api/workspace/reports/cashflow?**', async route => {
    if (new URL(route.request().url()).searchParams.has('date_from')) {
      filtered = true;
      await pending;
      await route.fulfill({ status: 503, json: { detail: 'Synthetic report update failed.' } });
    } else await route.continue();
  });
  const headers = { 'X-Ledger-Request': '1' };
  const original = await request.get('/api/workspace/preferences');
  expect(original.ok(), await original.text()).toBe(true);
  const originalPreferences = await original.json();
  try {
    const seeded = await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'GBP' } });
    expect(seeded.ok(), await seeded.text()).toBe(true);
    await page.goto('/#settings');
    await page.locator('#settings-reports > summary').click();
    const report = page.locator('#settings-reports');
    await expect(report.getByText('Spending · GBP', { exact: true })).toBeVisible();
    const original = await report.getByText('Spending · GBP', { exact: true }).locator('..').innerText();
    await report.getByLabel('From', { exact: true }).fill('2026-08-01');
    try {
      await expect(report.getByRole('status').filter({ hasText: 'Updating report… Previous results remain visible' })).toBeVisible();
      expect(await report.getByText('Spending · GBP', { exact: true }).locator('..').innerText()).toBe(original);
    } finally { release(); }
    expect(filtered).toBe(true);
    await expect(report.getByRole('alert')).toContainText('Synthetic report update failed.');
    await expect(report.getByRole('status').filter({ hasText: 'may not match your current filters' })).toBeVisible();
    expect(await report.getByText('Spending · GBP', { exact: true }).locator('..').innerText()).toBe(original);
  } finally {
    release();
    const restored = await request.patch('/api/workspace/preferences', { headers, data: originalPreferences });
    expect(restored.ok(), await restored.text()).toBe(true);
  }
});

test('expanded manual account forms reflow with long input at four widths', async ({ page }, info) => {
  await page.goto('/#settings');
  await page.locator('#settings-accounts > summary').click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await page.getByLabel('Account name', { exact: true }).fill('Synthetic household everyday account with a deliberately long descriptive name');
  await page.getByLabel('Institution', { exact: true }).fill('Synthetic international institution with a long name');
  await page.getByRole('combobox', { name: 'Account type', exact: true }).selectOption('credit');
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 700 });
    await page.getByLabel('Account name', { exact: true }).scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await expect(page.getByRole('button', { name: 'Create manual account', exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`settings-account-${width}.png`) });
  }
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByLabel('Account name', { exact: true })).toHaveCount(0);
});
