import { expect, test, type Page } from '@playwright/test';

const providers = [
  { id: 'lunchflow', label: 'Lunch Flow', auth_methods: ['personal_api_key'], capabilities: ['accounts', 'transactions'], credential_fields: [{ name: 'api_key', label: 'API key', secret: true }] },
  { id: 'snaptrade', label: 'SnapTrade', auth_methods: ['personal_api_key'], capabilities: ['accounts', 'holdings'], credential_fields: [{ name: 'client_id', label: 'Client ID', secret: true }, { name: 'consumer_key', label: 'Personal API key', secret: true }] },
];

async function openConnections(page: Page) {
  await page.route('**/api/connections/authorization/providers', route => route.fulfill({ json: { providers: [] } }));
  await page.goto('/#settings');
  await page.getByText('Provider connections', { exact: true }).click();
  await page.getByRole('button', { name: 'Add provider connection', exact: true }).click();
}

test('a personal provider connects with its default name and one refresh, without a separate check', async ({ page }, info) => {
  let connections: Record<string, unknown>[] = []; const operations: string[] = []; let saved: unknown;
  let releaseRefresh!: () => void;
  const refreshing = new Promise<void>(resolve => { releaseRefresh = resolve; });
  await page.route('**/api/connections', async route => {
    if (route.request().method() === 'POST') {
      saved = route.request().postDataJSON();
      connections = [{ id: 'onboarding-example', provider: 'lunchflow', label: 'Lunch Flow', source: 'saved', status: 'unverified', enabled: true, capabilities: [], last_error: null }];
      await route.fulfill({ json: connections[0] });
    } else await route.fulfill({ json: { providers, connections } });
  });
  await page.route('**/api/connections/onboarding-example/*', async route => {
    operations.push(new URL(route.request().url()).pathname.split('/').at(-1)!);
    await refreshing;
    connections[0] = { ...connections[0], status: 'ready', last_synced_at: new Date().toISOString() };
    await route.fulfill({ json: { connection: connections[0] } });
  });
  await openConnections(page);
  await expect(page.getByRole('link', { name: 'Open Lunch Flow setup guide' })).toHaveAttribute('href', 'https://www.lunchflow.app/docs/guides/destinations/api');
  await page.getByLabel('API key', { exact: true }).fill('synthetic-onboarding-key');
  await page.screenshot({ path: info.outputPath('provider-form.png'), fullPage: true });
  await page.getByRole('button', { name: 'Connect and refresh', exact: true }).click();
  try {
    await expect(page.getByRole('status').filter({ hasText: 'Reading your provider' })).toBeVisible();
    await expect(page.getByRole('combobox', { name: 'Provider', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Refresh data', exact: true })).toBeDisabled();
  } finally { releaseRefresh(); }
  await expect(page.getByRole('status').filter({ hasText: 'Connected and refreshed.' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Review bank accounts', exact: true })).toHaveAttribute('href', '#banking');
  expect(saved).toEqual({ provider: 'lunchflow', label: 'Lunch Flow', credentials: { api_key: 'synthetic-onboarding-key' } });
  expect(operations).toEqual(['sync']);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-onboarding-key');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath('provider-connected.png'), fullPage: true });
});

test('a failed first refresh keeps one saved connection and offers retry without re-entering its key', async ({ page }) => {
  let connections: Record<string, unknown>[] = []; let creates = 0; let refreshes = 0;
  await page.route('**/api/connections', async route => {
    if (route.request().method() === 'POST') {
      creates++;
      connections = [{ id: 'retry-example', provider: 'lunchflow', label: 'Lunch Flow', source: 'saved', status: 'unverified', enabled: true, capabilities: [], last_error: null }];
      await route.fulfill({ json: connections[0] });
    } else await route.fulfill({ json: { providers, connections } });
  });
  await page.route('**/api/connections/retry-example/sync', async route => {
    refreshes++;
    if (refreshes === 1) await route.fulfill({ status: 503, json: { detail: 'The provider is temporarily unavailable.' } });
    else {
      connections[0] = { ...connections[0], status: 'ready', last_synced_at: new Date().toISOString() };
      await route.fulfill({ json: { connection: connections[0] } });
    }
  });
  await openConnections(page);
  await page.getByLabel('API key', { exact: true }).fill('synthetic-retry-key');
  await page.getByRole('button', { name: 'Connect and refresh', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Connection saved, but the refresh needs attention.');
  await expect(page.getByRole('button', { name: 'Connect and refresh', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Edit connection', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Refresh data', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Connection refreshed.' })).toBeVisible();
  expect(creates).toBe(1); expect(refreshes).toBe(2);
});

test('switching to SnapTrade clears the previous provider secret and sends only Personal fields', async ({ page }) => {
  let connections: Record<string, unknown>[] = []; let saved: unknown;
  await page.route('**/api/connections', async route => {
    if (route.request().method() === 'POST') {
      saved = route.request().postDataJSON();
      connections = [{ id: 'snaptrade-example', provider: 'snaptrade', label: 'SnapTrade', source: 'saved', status: 'unverified', enabled: true, capabilities: [], last_error: null }];
      await route.fulfill({ json: connections[0] });
    } else await route.fulfill({ json: { providers, connections } });
  });
  await page.route('**/api/connections/snaptrade-example/sync', route => route.fulfill({ json: { connection: { ...connections[0], status: 'ready' } } }));
  await openConnections(page);
  await page.getByLabel('API key', { exact: true }).fill('discarded-lunchflow-secret');
  await page.getByRole('combobox', { name: 'Provider', exact: true }).selectOption('snaptrade');
  await expect(page.getByText('Brokerage accounts, holdings and activity', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Open SnapTrade setup guide' })).toBeVisible();
  await page.getByLabel('Client ID', { exact: true }).fill('synthetic-client');
  await page.getByLabel('Personal API key', { exact: true }).fill('synthetic-personal-key');
  await page.getByRole('button', { name: 'Connect and refresh', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Review investments', exact: true })).toHaveAttribute('href', '#connected-portfolio');
  expect(saved).toEqual({ provider: 'snaptrade', label: 'SnapTrade', credentials: { client_id: 'synthetic-client', consumer_key: 'synthetic-personal-key' } });
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toMatch(/discarded-lunchflow-secret|synthetic-personal-key/);
});
