import { test, expect } from '@playwright/test';

test('private dashboard, sign-in, single-use recovery, rotation, and sign-out', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  expect((await request.get('/api/summary')).status()).toBe(401);
  expect((await request.get('/api/investments/analysis/insights')).status()).toBe(401);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in to Am I Broke?' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: '../output/playwright-auth/login-mobile.png' });
  const login = async (code: string) => {
    await page.getByLabel('Password', { exact: true }).fill('Synthetic browser test password!');
    await page.getByLabel('Authenticator or recovery code').fill(code);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  };
  await page.getByRole('checkbox', { name: /Keep me signed in for 30 days/ }).check();
  await login('000000000000000000000000');
  await expect(page.getByLabel('Go to page')).toBeVisible();
  const session = (await page.context().cookies()).find(cookie => cookie.name === 'ledger_session');
  expect(session?.expires).toBeGreaterThan(Date.now() / 1000 + 29 * 86400);
  await page.reload();
  await expect(page.getByLabel('Go to page')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toHaveCount(0);
  await page.getByLabel('Go to page').selectOption('settings');
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  expect((await page.request.get('/api/investments/analysis/insights')).status()).toBe(200);
  const warmReview = await page.request.get('/api/investments/analysis/insights');
  expect(warmReview.status()).toBe(200);
  expect(warmReview.headers()['cache-control']).toContain('no-store');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  expect((await page.request.get('/api/investments/analysis/insights')).status()).toBe(401);
  await login('000000000000000000000000');
  await expect(page.getByRole('alert')).toContainText('incorrect, expired, or already used');
  await page.setViewportSize({ width: 1440, height: 960 });
  await login('000000000000000000000001');
  await expect(page.getByRole('button', { name: 'Recovery codes', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Recovery codes', exact: true }).click();
  await page.getByLabel('Password', { exact: true }).fill('Synthetic browser test password!');
  await page.getByLabel('Authenticator or recovery code').fill('000000000000000000000002');
  await page.getByRole('button', { name: 'Generate new recovery codes' }).click();
  await expect(page.getByRole('heading', { name: 'Save your recovery codes' })).toBeVisible();
  const codes = (await page.locator('pre').innerText()).split('\n');
  expect(codes).toHaveLength(10);
  await expect(page.getByRole('button', { name: 'Continue to sign in' })).toBeDisabled();
  await page.getByLabel('I saved these codes securely.').check();
  await page.getByRole('button', { name: 'Continue to sign in' }).click();
  await login('000000000000000000000003');
  await expect(page.getByRole('alert')).toBeVisible();
  await login(codes[0]);
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  // Ordinary sign-ins preserve other devices. An explicit revoke-others
  // action removes this browser and returns it to sign-in on refocus.
  const another = await request.post('/api/auth/login', {
    headers: { 'X-Ledger-Request': '1' },
    data: { password: 'Synthetic browser test password!', code: codes[1] },
  });
  expect(another.status()).toBe(200);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  expect((await page.request.get('/api/auth/status')).status()).toBe(200);
  expect(await (await page.request.get('/api/auth/status')).json()).toMatchObject({ authenticated: true });
  const revocation = await request.post('/api/auth/sessions/revoke-others', { headers: { 'X-Ledger-Request': '1' } });
  expect(revocation.status()).toBe(200);
  // Refocusing an otherwise idle page must check access even if no private
  // data request happens to be pending or the revision poll is throttled.
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading', { name: 'Sign in to Am I Broke?' })).toBeVisible();
  expect(errors).toEqual([]);
});
