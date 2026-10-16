import { expect, test } from '@playwright/test';

async function invalidateFromAnotherTab(page: import('@playwright/test').Page) {
  await page.evaluate(() => {
    const channel = new BroadcastChannel('ledger-data');
    channel.postMessage('changed');
    channel.close();
  });
}

for (const section of ['preferences', 'categories', 'accounts'] as const) {
  test(`${section} draft survives failed background refresh and retry`, async ({ page }) => {
    let fail = false;
    const endpoint = section === 'preferences' ? 'preferences' : section;
    await page.route(`**/api/workspace/${endpoint}`, route => fail && route.request().method() === 'GET'
      ? route.fulfill({ status: 503, json: { detail: 'Synthetic background refresh failed.' } }) : route.continue());
    await page.goto('/#settings');
    const panel = page.locator(`#settings-${section}`);
    await panel.locator(':scope > summary').click();
    if (section === 'categories') await panel.getByRole('button', { name: 'Add category', exact: true }).click();
    if (section === 'accounts') await panel.getByRole('button', { name: 'Add manual account', exact: true }).click();
    const field = panel.getByLabel(section === 'preferences' ? 'Locale' : section === 'categories' ? 'Category name' : 'Account name', { exact: true });
    const draft = section === 'preferences' ? 'de-DE' : 'Synthetic unsaved draft';
    await field.fill(draft);
    fail = true;
    await invalidateFromAnotherTab(page);
    await expect(panel.getByRole('alert')).toContainText('Synthetic background refresh failed.');
    await expect(field).toBeVisible();
    await expect(field).toHaveValue(draft);
    fail = false;
    await panel.getByRole('button', { name: 'Try again', exact: true }).click();
    await expect(panel.getByRole('alert')).toHaveCount(0);
    await expect(field).toHaveValue(draft);
  });
}

test('currency report retries the failed preferences request', async ({ page }) => {
  let fail = true;
  let reads = 0;
  await page.route('**/api/workspace/preferences', route => {
    reads++;
    return fail ? route.fulfill({ status: 503, json: { detail: 'Synthetic report preferences unavailable.' } })
      : route.continue();
  });
  await page.goto('/#settings');
  const panel = page.locator('#settings-reports');
  await panel.locator(':scope > summary').click();
  await expect(panel.getByRole('alert')).toContainText('Synthetic report preferences unavailable.');
  const before = reads;
  fail = false;
  await panel.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  expect(reads).toBeGreaterThan(before);
});

test('manual balance waits for workspace calendar, retries and preserves a chosen date', async ({ page, request }, info) => {
  const name = `Calendar account ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers: { 'X-Ledger-Request': '1' }, data: { display_name: name, institution: 'Synthetic Calendar Bank', account_type: 'cash', currency: 'GBP' } });
  expect(created.ok(), await created.text()).toBe(true);
  let fail = true;
  let today = '2026-01-02';
  let balanceWrites = 0;
  page.on('request', request => { if (request.method() === 'POST' && /\/balances$/.test(request.url())) balanceWrites++; });
  await page.route('**/api/workspace/calendar', route => route.fulfill(fail
    ? { status: 503, json: { detail: 'Synthetic calendar unavailable.' } }
    : { json: { today, timezone: 'Pacific/Kiritimati' } }));
  await page.goto('/#settings');
  const panel = page.locator('#settings-accounts');
  await panel.locator(':scope > summary').click();
  await panel.getByRole('button', { name: `Record balance for ${name}`, exact: true }).click();
  const date = panel.getByLabel('Balance date', { exact: true });
  await expect(panel.getByRole('alert')).toContainText('Synthetic calendar unavailable.');
  await expect(date).toHaveValue('');
  await expect(date).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Save balance', exact: true })).toBeDisabled();
  fail = false;
  await panel.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(date).toHaveValue(today);
  expect(balanceWrites).toBe(0);
  await expect(date).toBeEnabled();
  await date.fill('2026-01-01');
  today = '2026-01-03';
  const fetched = page.waitForResponse('**/api/workspace/calendar');
  await invalidateFromAnotherTab(page);
  await fetched;
  await expect(date).toHaveValue('2026-01-01');
});

for (const reverted of [false, true]) {
  test(`preference saves preserve remote untouched fields${reverted ? ' when a local edit returns to its initial value' : ' after background rebasing'}`, async ({ page, request }) => {
    const headers = { 'X-Ledger-Request': '1' };
    const original = await request.get('/api/workspace/preferences');
    expect(original.ok(), await original.text()).toBe(true);
    const originalPreferences = await original.json();
    try {
      const seeded = await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' } });
      expect(seeded.ok(), await seeded.text()).toBe(true);
      await page.goto('/#settings');
      const panel = page.locator('#settings-preferences');
      await panel.locator(':scope > summary').click();
      const locale = panel.getByLabel('Locale', { exact: true });
      await locale.fill('de-DE');
      if (reverted) await locale.fill('en-GB');
      const remote = await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'EUR', locale: 'fr-FR', timezone: 'Asia/Tokyo' } });
      expect(remote.ok(), await remote.text()).toBe(true);
      if (!reverted) {
        await invalidateFromAnotherTab(page);
        await expect(panel.getByLabel('Reporting currency', { exact: true })).toHaveValue('EUR');
        await expect(panel.getByLabel('Time zone', { exact: true })).toHaveValue('Asia/Tokyo');
      }
      await expect(locale).toHaveValue(reverted ? 'en-GB' : 'de-DE');
      await panel.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
      await expect(panel.getByRole('status').filter({ hasText: 'Preferences saved.' })).toBeVisible();
      const result = await request.get('/api/workspace/preferences');
      expect(await result.json()).toEqual({ reporting_currency: 'EUR', locale: reverted ? 'en-GB' : 'de-DE', timezone: 'Asia/Tokyo' });
    } finally {
      const restored = await request.patch('/api/workspace/preferences', { headers, data: originalPreferences });
      expect(restored.ok(), await restored.text()).toBe(true);
    }
  });
}
