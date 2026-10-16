import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

test.use({ timezoneId: 'Asia/Tokyo' });
const headers = { 'X-Ledger-Request': '1' };

async function patchPreferences(request: APIRequestContext, data: Record<string, string>) {
  const response = await request.patch('/api/workspace/preferences', { headers, data });
  expect(response.ok(), await response.text()).toBe(true);
  return response.json();
}

async function preferencesPanel(page: Page) {
  await page.goto('/#settings');
  const panel = page.locator('#settings-preferences');
  await panel.locator(':scope > summary').click();
  await expect(panel.getByLabel('Time zone', { exact: true })).toBeVisible();
  return panel;
}

async function suggestion(field: Locator, value: string) {
  return field.evaluate((element, selected) => ((element as HTMLInputElement).list
    ?.querySelector(`option[value="${selected}"]`) as HTMLOptionElement | null)?.label, value);
}

test('named suggestions and the explicit device helper save supported preferences', async ({ page, request }, info) => {
  const original = await (await request.get('/api/workspace/preferences')).json();
  try {
    await patchPreferences(request, { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' });
    const writes: Record<string, string>[] = [];
    page.on('request', request => { if (request.method() === 'PATCH' && request.url().endsWith('/workspace/preferences')) writes.push(request.postDataJSON()); });
    const panel = await preferencesPanel(page);
    const currency = panel.getByLabel('Reporting currency', { exact: true });
    const locale = panel.getByLabel('Locale', { exact: true });
    const zone = panel.getByLabel('Time zone', { exact: true });
    expect(await suggestion(currency, 'KWD')).toBe('Kuwaiti dinar');
    expect(await suggestion(locale, 'de-DE')).toContain('German (Germany) · 1.234.567,89');
    expect(await suggestion(zone, 'Asia/Tokyo')).toBe('Tokyo · Asia');
    // A device in Tokyo never changes the saved London setting on page load.
    await expect(zone).toHaveValue('Europe/London');
    await currency.fill('kwd');
    await expect(currency).toHaveValue('KWD');
    await locale.fill('de-DE');
    await expect(panel.getByText('Example: 1.234.567,89', { exact: true })).toBeVisible();
    await panel.getByRole('button', { name: 'Use this device’s time zone (Asia/Tokyo)', exact: true }).click();
    await expect(zone).toHaveValue('Asia/Tokyo');
    expect(writes).toEqual([]);
    await panel.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
    await expect(panel.getByRole('status')).toHaveText('Preferences saved.');
    expect(writes).toEqual([{ reporting_currency: 'KWD', locale: 'de-DE', timezone: 'Asia/Tokyo' }]);
    expect(await (await request.get('/api/workspace/preferences')).json()).toEqual(writes[0]);
    await page.setViewportSize({ width: 320, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: info.outputPath('preferences-named-choices.png') });
  } finally { await patchPreferences(request, original); }
});

test('fallback suggestions and custom entries work when the device zone is unavailable', async ({ page, request }) => {
  const original = await (await request.get('/api/workspace/preferences')).json();
  await page.addInitScript(() => {
    Object.defineProperty(Intl, 'supportedValuesOf', { value: undefined, configurable: true });
    const resolved = Intl.DateTimeFormat.prototype.resolvedOptions;
    Object.defineProperty(Intl.DateTimeFormat.prototype, 'resolvedOptions', {
      value: function () { return { ...resolved.call(this), timeZone: undefined }; }, configurable: true,
    });
  });
  try {
    await patchPreferences(request, { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' });
    const panel = await preferencesPanel(page);
    const zone = panel.getByLabel('Time zone', { exact: true });
    await expect(panel.getByRole('button', { name: 'Use this device’s time zone', exact: true })).toBeDisabled();
    await expect(panel.getByText('Your device’s time zone is unavailable.', { exact: false })).toBeVisible();
    expect(await suggestion(zone, 'Asia/Tokyo')).toBe('Tokyo · Asia');
    await expect(zone).toHaveValue('Europe/London');
    // Suggestions remain optional: supported choices outside them still save.
    await panel.getByLabel('Reporting currency', { exact: true }).fill('KZT');
    await panel.getByLabel('Locale', { exact: true }).fill('fr-CA');
    await zone.fill('Asia/Kathmandu');
    await panel.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
    await expect(panel.getByRole('status')).toHaveText('Preferences saved.');
    expect(await (await request.get('/api/workspace/preferences')).json()).toEqual({ reporting_currency: 'KZT', locale: 'fr-CA', timezone: 'Asia/Kathmandu' });
  } finally { await patchPreferences(request, original); }
});

test('a device-zone edit rebases untouched preferences and locks helpers during save', async ({ page, request }) => {
  const original = await (await request.get('/api/workspace/preferences')).json();
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  try {
    await patchPreferences(request, { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' });
    const panel = await preferencesPanel(page);
    const helper = panel.getByRole('button', { name: 'Use this device’s time zone (Asia/Tokyo)', exact: true });
    await helper.click();
    await patchPreferences(request, { reporting_currency: 'EUR', locale: 'de-DE', timezone: 'America/Los_Angeles' });
    await page.evaluate(() => { const channel = new BroadcastChannel('ledger-data'); channel.postMessage('changed'); channel.close(); });
    await expect(panel.getByLabel('Reporting currency', { exact: true })).toHaveValue('EUR');
    await expect(panel.getByLabel('Locale', { exact: true })).toHaveValue('de-DE');
    await expect(panel.getByLabel('Time zone', { exact: true })).toHaveValue('Asia/Tokyo');
    let submitted: unknown;
    await page.route('**/api/workspace/preferences', async route => {
      if (route.request().method() === 'PATCH') { submitted = route.request().postDataJSON(); await pending; }
      await route.continue();
    });
    await panel.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
    await expect(helper).toBeDisabled();
    await expect(panel.getByLabel('Time zone', { exact: true })).toBeDisabled();
    await expect(panel.getByLabel('Locale', { exact: true })).toBeDisabled();
    expect(submitted).toEqual({ timezone: 'Asia/Tokyo' });
    release();
    await expect(panel.getByRole('status')).toHaveText('Preferences saved.');
    expect(await (await request.get('/api/workspace/preferences')).json()).toEqual({ reporting_currency: 'EUR', locale: 'de-DE', timezone: 'Asia/Tokyo' });
  } finally { release(); await patchPreferences(request, original); }
});
