import { expect, test } from '@playwright/test';

test('manual account configuration, CSV review and history coverage survive reload', async ({ page }, info) => {
  const name = `Product account ${info.project.name}`;
  await page.goto('/#settings');
  await page.getByText('Account settings', { exact: true }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await page.getByLabel('Account name', { exact: true }).fill(name);
  await page.getByLabel('Account currency', { exact: true }).fill('GBP');
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  await page.getByRole('button', { name: `Edit account ${name}`, exact: true }).click();
  await page.getByLabel('Your ownership share (%)', { exact: true }).fill('50');
  let releasePreview!: () => void;
  const previewPending = new Promise<void>(resolve => { releasePreview = resolve; });
  await page.route('**/api/workspace/accounts/*/preview', async route => { await previewPending; await route.continue(); });
  await page.getByRole('button', { name: 'Review account changes', exact: true }).click();
  await expect(page.getByLabel('Your ownership share (%)', { exact: true })).toBeDisabled();
  releasePreview();
  await expect(page.getByRole('status').filter({ hasText: 'Review account changes' })).toContainText('ownership share: 1 → 0.5');
  await page.getByRole('button', { name: 'Save account changes', exact: true }).click();
  await expect(page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) })).toContainText('50% ownership');

  await page.getByText('Import a CSV', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Import into account', exact: true }).selectOption({ label: `${name} · GBP` });
  const csv = Buffer.from('Date,Description,Amount,Reference\n2026-08-01,Product groceries,-12.34,product-row-1\n2026-08-02,Product income,50.00,product-row-2\n');
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'example.csv', mimeType: 'text/csv', buffer: csv });
  await page.getByRole('combobox', { name: 'Transaction ID column (optional)', exact: true }).selectOption('Reference');
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  const preview = page.getByRole('region', { name: 'CSV import preview', exact: true });
  await expect(preview).toContainText('2 new');
  await expect(preview).toContainText('-12.34 GBP');
  await page.getByRole('button', { name: 'Confirm CSV import', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Import complete.' })).toBeVisible();
  await page.getByLabel('CSV file', { exact: true }).setInputFiles({ name: 'example-copy.csv', mimeType: 'text/csv', buffer: csv });
  await page.getByRole('button', { name: 'Preview CSV import', exact: true }).click();
  await expect(preview).toContainText('2 already imported');
  await page.getByRole('combobox', { name: 'Date format', exact: true }).selectOption('%d/%m/%Y');
  await expect(preview).toHaveCount(0);

  await page.getByRole('button', { name: `History coverage for ${name}`, exact: true }).click();
  await page.getByLabel('Coverage start', { exact: true }).fill('2026-08-01');
  await page.getByLabel('Coverage end', { exact: true }).fill('2026-08-31');
  await page.getByRole('combobox', { name: 'Coverage status', exact: true }).selectOption('complete');
  await page.getByLabel('Evidence or reason', { exact: true }).fill('Checked the complete August CSV export against the statement.');
  await page.getByRole('button', { name: 'Save history coverage', exact: true }).click();
  await expect(page.getByRole('region', { name: `History coverage for ${name}`, exact: true })).toContainText('Confirmed complete');
  await page.reload();
  await page.getByText('Account settings', { exact: true }).click();
  const account = page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) });
  await expect(account).toContainText('2 transactions');
  await expect(account).toContainText('50% ownership');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('workspace preferences and category catalog persist without restoring archived built-ins', async ({ page }, info) => {
  await page.goto('/#settings');
  await page.getByText('Workspace preferences', { exact: true }).click();
  await page.getByLabel('Reporting currency', { exact: true }).fill('EUR');
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Preferences saved.' })).toBeVisible();
  await page.getByText('Transaction categories', { exact: true }).click();
  await page.getByRole('button', { name: 'Add category', exact: true }).click();
  const label = `Pet supplies ${info.project.name}`;
  await page.getByLabel('Category name', { exact: true }).fill(label);
  await page.getByRole('button', { name: 'Create category', exact: true }).click();
  await page.getByRole('button', { name: `Edit category ${label}`, exact: true }).click();
  await page.getByLabel('Archive category', { exact: true }).check();
  await page.getByRole('button', { name: 'Save category', exact: true }).click();
  await expect(page.locator('li').filter({ has: page.getByRole('button', { name: `Edit category ${label}`, exact: true }) })).toContainText('Archived');
  const response = await page.request.get('/api/category-options');
  expect(await response.json()).not.toContain(label);
  await page.reload();
  await page.getByText('Workspace preferences', { exact: true }).click();
  await expect(page.getByLabel('Reporting currency', { exact: true })).toHaveValue('EUR');
});

test('provider credentials stay out of storage and disconnect is explicitly confirmed', async ({ page }) => {
  let connections: Record<string, unknown>[] = []; let saved: unknown; let disconnected = false;
  const providers = [{ id: 'lunchflow', label: 'Lunch Flow', auth_methods: ['personal_api_key'], capabilities: ['accounts', 'transactions'], credential_fields: [{ name: 'api_key', label: 'API key', secret: true }] }];
  await page.route('**/api/connections', async route => {
    if (route.request().method() === 'POST') {
      saved = route.request().postDataJSON();
      connections = [{ id: 'test-connection', provider: 'lunchflow', label: 'Example banks', source: 'saved', status: 'configured', enabled: true, capabilities: [], last_error: null }];
      await route.fulfill({ json: connections[0] });
    } else await route.fulfill({ json: { providers, connections } });
  });
  await page.route('**/api/connections/test-connection/sync', route => route.fulfill({ json: { connection: connections[0] } }));
  await page.route('**/api/connections/test-connection/disconnect', async route => { disconnected = true; connections[0] = { ...connections[0], status: 'disconnected', enabled: false }; await route.fulfill({ json: connections[0] }); });
  await page.goto('/#settings');
  await page.getByText('Provider connections', { exact: true }).click();
  await page.getByRole('button', { name: 'Add provider connection', exact: true }).click();
  await page.getByLabel('Connection name', { exact: true }).fill('Example banks');
  await page.getByLabel('API key', { exact: true }).fill('synthetic-private-api-key');
  await page.getByRole('button', { name: 'Connect and refresh', exact: true }).click();
  await expect(page.getByText('Example banks', { exact: true })).toBeVisible();
  expect(saved).toEqual({ provider: 'lunchflow', label: 'Example banks', credentials: { api_key: 'synthetic-private-api-key' } });
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-private-api-key');
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  expect(disconnected).toBe(false);
  await page.getByRole('button', { name: 'Confirm disconnect', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Disconnected.' })).toBeVisible();
  expect(disconnected).toBe(true);
});

test('token-authorized first-run setup holds recovery codes before opening the workspace', async ({ page }) => {
  let configured = false; let enrollments = 0;
  await page.route('**/api/auth/status', route => route.fulfill({ json: { enabled: true, configured, authenticated: configured } }));
  await page.route('**/api/setup/status', route => route.fulfill({ json: { available: true, configured: false } }));
  await page.route('**/api/setup/begin', route => {
    expect(route.request().headers()['x-ledger-request']).toBe('1');
    return route.fulfill({ json: { challenge_id: 'synthetic-challenge', secret: 'SYNTHETICSETUPKEY', provisioning_uri: 'otpauth://totp/example', expires_at: Date.now() / 1000 + 600 } });
  });
  await page.route('**/api/setup/enroll', route => { configured = true; enrollments++; return route.fulfill({ json: { ok: true, recovery_codes: ['synthetic-recovery-code'] } }); });
  await page.goto('/#settings');
  await page.getByLabel('Setup token', { exact: true }).fill('a-synthetic-setup-token-longer-than-32-characters');
  await page.getByRole('button', { name: 'Begin secure setup', exact: true }).click();
  await page.getByText('Can’t scan? Use a setup key', { exact: true }).click();
  await expect(page.getByLabel('Authenticator setup key', { exact: true })).toHaveText('SYNTHETICSETUPKEY');
  await page.getByLabel('New password', { exact: true }).fill('Synthetic password for browser!');
  await page.getByLabel('Confirm password', { exact: true }).fill('Different synthetic password!');
  await page.getByLabel('Authenticator code', { exact: true }).fill('123456');
  await page.getByRole('button', { name: 'Create owner account', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveText('The passwords do not match.');
  expect(enrollments).toBe(0);
  await page.getByLabel('Confirm password', { exact: true }).fill('Synthetic password for browser!');
  await page.getByRole('button', { name: 'Create owner account', exact: true }).click();
  await expect(page.getByText('synthetic-recovery-code', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Open your workspace', exact: true })).toBeDisabled();
  await page.getByRole('checkbox', { name: 'I saved these codes securely.' }).check();
  await page.getByRole('button', { name: 'Open your workspace', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Account & security', exact: true })).toBeVisible();
});

test('an existing unconfigured installation is not offered browser enrollment', async ({ page }) => {
  await page.route('**/api/auth/status', route => route.fulfill({ json: { enabled: true, configured: false, authenticated: false } }));
  await page.route('**/api/setup/status', route => route.fulfill({ json: { available: false, configured: false } }));
  await page.goto('/');
  await expect(page.getByText('Browser setup isn’t ready yet.', { exact: false })).toBeVisible();
  await expect(page.getByLabel('Setup token', { exact: true })).toHaveCount(0);
});

test('provider authorization callback clears URL secrets and exchanges only once', async ({ page }) => {
  let exchanges = 0;
  await page.route('**/connect/callback/lunchflow?**', async route => {
    const response = await route.fetch({ url: new URL('/', route.request().url()).href });
    await route.fulfill({ response });
  });
  await page.route('**/api/connections/authorization/complete', async route => {
    exchanges++;
    expect(route.request().postDataJSON()).toEqual({ provider: 'lunchflow', state: 'synthetic-state', code: 'synthetic-code' });
    expect(route.request().headers()['x-ledger-request']).toBe('1');
    await route.fulfill({ json: { connection: { id: 'authorized-connection' } } });
  });
  await page.goto('/connect/callback/lunchflow?state=synthetic-state&code=synthetic-code');
  await expect(page.getByRole('heading', { name: 'Your provider is connected', exact: true })).toBeVisible();
  expect(new URL(page.url()).search).toBe('');
  expect(exchanges).toBe(1);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-code');
  await page.getByRole('button', { name: 'Open Settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  expect(exchanges).toBe(1);
});

test('session management keeps this session while signing out other devices', async ({ page }) => {
  await page.route('**/api/auth/status', route => route.fulfill({ json: { enabled: true, configured: true, authenticated: true } }));
  let sessions = [{ id: 'current', current: true, remembered: false, created_at: 1780000000, last_active_at: 1780000010 }, { id: 'other', current: false, remembered: true, created_at: 1770000000, last_active_at: 1780000000 }];
  await page.route('**/api/auth/sessions', route => route.fulfill({ json: { sessions } }));
  await page.route('**/api/auth/sessions/revoke-others', route => { sessions = sessions.filter(session => session.current); return route.fulfill({ json: { ok: true } }); });
  await page.goto('/#settings');
  await page.getByText('Signed-in sessions', { exact: true }).click();
  await expect(page.getByText('Another session · Remembered', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out other sessions', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Other sessions have been signed out.' })).toBeVisible();
  await expect(page.getByText('This session', { exact: true })).toBeVisible();
  await expect(page.getByText('Another session · Remembered', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Sign out other sessions', exact: true })).toBeDisabled();
});

test('hosted connections keep label editing available when provider authorization is unavailable', async ({ page }) => {
  let connection = { id: 'hosted-example', provider: 'snaptrade', auth_method: 'snaptrade_commercial', label: 'My investments', source: 'saved', status: 'ready', enabled: true, last_error: null };
  let saved: unknown;
  await page.route('**/api/connections', route => route.fulfill({ json: { providers: [{ id: 'snaptrade', label: 'SnapTrade', auth_methods: ['personal_api_key'], capabilities: [], credential_fields: [{ name: 'consumer_key', label: 'API key', secret: true }] }], connections: [connection] } }));
  await page.route('**/api/connections/authorization/providers', route => route.fulfill({ json: { providers: [] } }));
  await page.route('**/api/connections/hosted-example', route => { saved = route.request().postDataJSON(); connection = { ...connection, ...route.request().postDataJSON() }; return route.fulfill({ json: connection }); });
  await page.goto('/#settings');
  await page.getByText('Provider connections', { exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reconnect with provider', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Check connection', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Refresh data', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Edit connection', exact: true }).click();
  await expect(page.getByLabel('Replace credentials', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('API key', { exact: true })).toHaveCount(0);
  await page.getByLabel('Connection name', { exact: true }).fill('Renamed investments');
  await page.getByRole('button', { name: 'Save connection', exact: true }).click();
  await expect(page.getByText('Renamed investments', { exact: true })).toBeVisible();
  expect(saved).toEqual({ label: 'Renamed investments' });
});

test('cancelled brokerage authorization clears callback parameters without exchanging credentials', async ({ page }) => {
  let exchanges = 0;
  await page.route('**/connect/callback/snaptrade?**', async route => {
    await route.fulfill({ response: await route.fetch({ url: new URL('/', route.request().url()).href }) });
  });
  await page.route('**/api/connections/authorization/complete', async route => { exchanges++; await route.fulfill({ status: 400, json: { detail: 'Should not be called' } }); });
  await page.goto('/connect/callback/snaptrade?state=synthetic-state&status=ABANDONED');
  await expect(page.getByRole('heading', { name: 'Connection needs attention', exact: true })).toBeVisible();
  expect(new URL(page.url()).search).toBe('');
  await page.getByRole('button', { name: 'Open Settings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  expect(exchanges).toBe(0);
});

test('foreign CSV reports require dated FX and retain original amounts when reviewed', async ({ page, request }, info) => {
  const headers = { 'X-Ledger-Request': '1' };
  // Use separate historical dates so each viewport starts without an FX reference.
  const date = info.project.name === 'desktop' ? '2001-02-03' : '2002-02-03';
  const prefs = await request.patch('/api/workspace/preferences', { headers, data: { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' } });
  expect(prefs.ok()).toBeTruthy();
  const created = await request.post('/api/workspace/accounts', { headers, data: { display_name: `EUR report ${info.project.name}`, currency: 'EUR' } });
  expect(created.ok()).toBeTruthy();
  const account = await created.json();
  const input = { account_id: account.id, csv_text: `Date,Description,Amount,Reference\n${date},EUR groceries,-10,foreign-expense\n${date},EUR salary,40,foreign-income\n`, mapping: { date_column: 'Date', description_column: 'Description', amount_column: 'Amount', id_column: 'Reference', currency: 'EUR' }, source_name: 'CSV import', source_filename: 'foreign-example.csv' };
  const previewResponse = await request.post('/api/workspace/imports/preview', { headers, data: input });
  expect(previewResponse.ok()).toBeTruthy();
  const preview = await previewResponse.json();
  expect(preview.new_count).toBe(2);
  const committed = await request.post('/api/workspace/imports/commit', { headers, data: { ...input, preview_token: preview.preview_token, confirm_overlaps: false } });
  expect(committed.ok()).toBeTruthy();
  await page.goto('/#settings');
  await page.getByText('Workspace reports', { exact: true }).click();
  await page.getByLabel('From', { exact: true }).fill(date);
  await page.getByLabel('To', { exact: true }).fill(date);
  await expect(page.getByRole('status').filter({ hasText: 'Partial totals: 2 transactions need dated exchange rates' })).toBeVisible();
  const income = page.getByText('Income · GBP', { exact: true }).locator('..');
  const spending = page.getByText('Spending · GBP', { exact: true }).locator('..');
  await expect(income).toContainText('£0.00');
  await expect(spending).toContainText('£0.00');
  await page.getByText('Supply a dated exchange reference', { exact: true }).click();
  await page.getByLabel('Currency', { exact: true }).fill('EUR');
  await page.getByLabel('Observed date', { exact: true }).fill(date);
  await page.getByLabel('GBP per unit', { exact: true }).fill('0.8');
  await page.getByLabel('Source or evidence', { exact: true }).fill('Synthetic dated rate for isolated browser test.');
  await page.getByRole('button', { name: 'Save reference', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Exchange reference saved.' })).toBeVisible();
  await expect(income).toContainText('£32.00');
  await expect(spending).toContainText('£8.00');
  await expect(page.getByRole('status').filter({ hasText: 'Partial totals:' })).toHaveCount(0);
  await page.getByText('Amounts and conversion evidence (2 transactions)', { exact: true }).click();
  const expense = page.getByRole('row').filter({ hasText: 'EUR groceries' });
  await expect(expense).toContainText('€10.00');
  await expect(expense).toContainText('£8.00');
  await expense.getByRole('button', { name: 'Review', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('€10.00');
  await dialog.getByRole('button', { name: 'Notes & exclusions', exact: true }).click();
  await dialog.getByRole('checkbox', { name: /Exclude this transaction from spending analytics/ }).check();
  await dialog.getByRole('button', { name: /Save review/ }).click();
  await expect(dialog).toHaveCount(0);
  await expect(income).toContainText('£32.00');
  await expect(spending).toContainText('£0.00');
  await expect(expense).toContainText('€10.00');
  await expect(expense).toContainText('User exclusions');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('first-run guidance survives a slow authentication refresh after enrollment', async ({ page }) => {
  let configured = false; let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/auth/status', async route => {
    if (configured) await pending;
    await route.fulfill({ json: { enabled: true, configured, authenticated: configured } });
  });
  await page.route('**/api/setup/status', route => route.fulfill({ json: { available: true, configured: false } }));
  await page.route('**/api/setup/begin', route => route.fulfill({ json: { challenge_id: 'synthetic-challenge', secret: 'SYNTHETICSETUPKEY', provisioning_uri: 'otpauth://totp/example', expires_at: Date.now() / 1000 + 600 } }));
  await page.route('**/api/setup/enroll', route => { configured = true; return route.fulfill({ json: { ok: true, recovery_codes: ['synthetic-recovery-code'] } }); });
  await page.goto('/');
  await page.getByLabel('Setup token', { exact: true }).fill('a-synthetic-setup-token-longer-than-32-characters');
  await page.getByRole('button', { name: 'Begin secure setup', exact: true }).click();
  await page.getByLabel('New password', { exact: true }).fill('Synthetic password for browser!');
  await page.getByLabel('Confirm password', { exact: true }).fill('Synthetic password for browser!');
  await page.getByLabel('Authenticator code', { exact: true }).fill('123456');
  await page.getByRole('button', { name: 'Create owner account', exact: true }).click();
  await page.getByRole('checkbox', { name: 'I saved these codes securely.' }).check();
  await page.getByRole('button', { name: 'Continue to sign in', exact: true }).click();
  release();
  await expect(page.getByRole('heading', { name: 'Welcome to your workspace', exact: true })).toBeVisible();
});
