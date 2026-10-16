import { expect, test, type Page } from '@playwright/test';
import { wealth } from './fixtures/wealth';
import { revealMobileControls } from './navigation-helpers';

async function setup(page: Page) {
  const mutations: { source_id: string; archived: boolean }[] = [];
  let archived = false, failUndo = false;
  await page.route('**/api/wealth?**', route => {
    const query = new URL(route.request().url()).searchParams;
    const selection = query.getAll('account');
    const accounts = wealth.accounts.map(account => ({ ...account, archived_by_user: account.id === 'investment' && archived, default_included: account.id !== 'investment' || !archived }));
    const selected = accounts.filter(account => selection.length ? selection.includes(account.id) : account.default_included);
    const total = selected.reduce((sum, account) => sum + account.value, 0);
    return route.fulfill({ json: { ...wealth, range: query.get('range'), accounts, selected_account_ids: selected.map(a => a.id), summary: { ...wealth.summary, net_worth: total, selected_accounts: selected.length } } });
  });
  await page.route('**/api/wealth/sources/archive', route => {
    const payload = route.request().postDataJSON(); mutations.push(payload);
    if (!payload.archived && failUndo) return route.fulfill({ status: 503, json: {} });
    archived = payload.archived; return route.fulfill({ json: { ok: true } });
  });
  await page.goto('/#net-worth');
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  return { mutations, failUndo: (value: boolean) => { failUndo = value; } };
}

test('archive offers immediate visible undo, retains retry after failure and restores the selected value', async ({ page }, info) => {
  const fixture = await setup(page);
  const archive = page.getByRole('button', { name: 'Archive Demo portfolio', exact: true }).filter({ visible: true });
  await archive.click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£900.00');
  const notice = page.getByRole('complementary', { name: 'Account update' });
  const undo = notice.getByRole('button', { name: 'Undo archive' });
  await expect(undo).toBeInViewport();
  await page.screenshot({ path: info.outputPath('archive-undo.png') });
  fixture.failUndo(true); await undo.click();
  await expect(notice.getByRole('alert')).toContainText('could not be reinstated');
  await expect(undo).toBeEnabled();
  fixture.failUndo(false); await undo.click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  await expect(undo).toHaveCount(0);
  expect(fixture.mutations).toEqual([{ source_id: 'investment', archived: true }, { source_id: 'investment', archived: false }, { source_id: 'investment', archived: false }]);
});

test('undo restores explicit account membership while retaining later selection changes', async ({ page }) => {
  await setup(page);
  await revealMobileControls(page, 'Accounts & currency');
  const picker = page.locator('summary').filter({ hasText: /^Accounts · 3/ });
  await picker.click();
  const group = page.getByRole('group', { name: 'Accounts included in net assets' });
  await group.getByRole('checkbox', { name: /Demo card/ }).uncheck();
  await page.getByRole('button', { name: 'Apply selection' }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,500.00');
  await page.locator('summary').filter({ hasText: /^Accounts · 2/ }).click();
  await group.getByRole('button', { name: 'Archive Demo portfolio', exact: true }).click();
  await expect(group).toBeHidden();
  await expect(page.getByRole('button', { name: 'Undo archive' })).toBeInViewport();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£1,000.00');
  await page.locator('summary').filter({ hasText: /^Accounts · 1/ }).click();
  await group.getByRole('checkbox', { name: /Demo card/ }).check();
  await page.getByRole('button', { name: 'Apply selection' }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£900.00');
  await page.getByRole('button', { name: 'Undo archive' }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
});

test('refresh starts providers together, keeps saved values, waits for FX and retries only the failed source', async ({ page }, info) => {
  await setup(page);
  await page.clock.install();
  let releaseBanks!: () => void, releaseInvestments!: () => void;
  const bankGate = new Promise<void>(resolve => { releaseBanks = resolve; });
  const investmentGate = new Promise<void>(resolve => { releaseInvestments = resolve; });
  let banks = 0, investments = 0, rates = 0;
  await page.route('**/api/banking/sync', async route => {
    banks++;
    if (banks === 1) { await bankGate; return route.fulfill({ status: 503, json: {} }); }
    return route.fulfill({ json: { configured: true, accounts: [], sync: { error: null } } });
  });
  await page.route('**/api/investments/connected/refresh', async route => {
    investments++; await investmentGate;
    return route.fulfill({ json: { configured: true, connections: { status: 'ok', items: [] }, accounts: [], cache: {} } });
  });
  await page.route('**/api/wealth/refresh', route => { rates++; return route.fulfill({ json: { updated: 2, attempted: 2, missing: 0 } }); });
  await revealMobileControls(page, 'Accounts & currency');
  await page.getByRole('button', { name: '↻ Refresh', exact: true }).click();
  await expect.poll(() => [banks, investments, rates]).toEqual([1, 1, 0]);
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£3,400.00');
  releaseBanks();
  const status = page.getByRole('region', { name: 'Refresh status' });
  await expect(status).toContainText('Banks · Update incomplete');
  expect(rates).toBe(0); releaseInvestments();
  await expect(status).toContainText('Investments · Updated');
  await expect(status).toContainText('Exchange rates · Updated');
  await expect(status.getByRole('button', { name: 'Retry banks' })).toBeDisabled();
  await page.clock.fastForward(61_000);
  await expect(status.getByRole('button', { name: 'Retry banks' })).toBeEnabled();
  await page.screenshot({ path: info.outputPath('source-refresh.png') });
  await status.getByRole('button', { name: 'Retry banks' }).click();
  await expect(status).toContainText('Banks · Updated');
  expect([banks, investments, rates]).toEqual([2, 1, 1]);
});

test('archive Undo stays clear of navigation on narrow phones and tablets', async ({ page }, info) => {
  test.skip(info.project.name !== 'mobile');
  for (const width of [320, 844]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("about:blank");
    await setup(page);
    await page.getByRole('button', { name: 'Archive Demo portfolio', exact: true }).filter({ visible: true }).click();
    const undo = page.getByRole('button', { name: 'Undo archive' });
    await expect(undo).toBeInViewport();
    const action = (await undo.boundingBox())!;
    const nav = (await page.getByRole('navigation', { name: 'Quick navigation', exact: true }).boundingBox())!;
    expect(action.y + action.height).toBeLessThan(nav.y);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: info.outputPath(`undo-${width}.png`) });
  }
});

test('phone account selection stays in the page and restores focus on close', async ({ page }, info) => {
  test.skip(info.project.name !== 'mobile');
  await page.setViewportSize({ width: 320, height: 700 });
  await setup(page);
  await revealMobileControls(page, 'Accounts & currency');
  const trigger = page.locator('summary').filter({ hasText: /^Accounts · 3/ });
  await trigger.click();
  await expect(page.getByRole('searchbox', { name: 'Find an account to include' })).toBeFocused();
  const panel = page.getByRole('group', { name: 'Accounts included in net assets' });
  await expect(panel).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(trigger).toBeFocused();
});
