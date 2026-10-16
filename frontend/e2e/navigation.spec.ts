import { navigateFinance } from "./navigation-helpers";
import { test, expect, type Page } from '@playwright/test';

async function go(page: Page, id: string, name: string) {
  await page.locator('nav[aria-label="Main navigation"]').waitFor({ state: 'attached' });
  await navigateFinance(page, id, name);
}

test('net assets opens first; URLs, reload and browser history keep page navigation usable', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Net assets', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#net-worth$/);
  await expect(page.getByText(/latest supported balances · debts subtracted/)).toBeVisible();
  await go(page, 'connected-portfolio', 'Investments');
  await expect(page).toHaveURL(/#connected-portfolio$/);
  await go(page, 'banking', 'Banks & accounts');
  await expect(page).toHaveURL(/#banking$/);
  await page.goBack();
  await expect(page.getByRole('heading', { level: 1, name: 'Investments', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('heading', { level: 1, name: 'Net assets', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Net assets', exact: true })).toBeVisible();
  await page.goto('/#planning');
  await expect(page.getByRole('heading', { level: 1, name: 'Planning', exact: true })).toBeVisible();
});

test('wealth controls stay compact and primary mobile destinations remain directly reachable', async ({ page }, info) => {
  await page.goto('/#net-worth');
  await expect(page.getByText(/latest supported balances · debts subtracted/)).toBeVisible();
  const main = page.locator('main');
  expect((await main.boundingBox())!.y).toBeLessThan(170);
  await expect(page.getByRole('group', { name: 'Accounts included in net assets' })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  if (info.project.name === 'mobile') {
    const quick = page.getByRole('navigation', { name: 'Quick navigation', exact: true });
    for (const [name, heading] of [['Investments','Investments'], ['Banks','Banks & accounts'], ['Activity','Transactions'], ['Net assets','Net assets']]) {
      await quick.getByRole('button', { name, exact: true }).click();
      await expect(page.getByRole('heading', { level: 1, name: heading, exact: true })).toBeVisible();
    }
  }
  await page.getByRole('group', { name: 'Net assets time range', exact: true }).getByRole('button', { name: '1M', exact: true }).click();
  await expect(page.getByRole('button', { name: '1M', exact: true })).toHaveAttribute('aria-pressed', 'true');
});

test('investment navigation fits the narrowest supported phone without wrapping', async ({ page }, info) => {
  test.skip(info.project.name !== 'mobile');
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('/#connected-portfolio');
  const tabs = page.getByRole('tablist', { name: 'Investment views' });
  await expect(tabs.getByRole('tab', { name: 'Connections' }).locator('span').first()).toHaveText('Connect');
  const boxes = await tabs.getByRole('tab').evaluateAll(elements => elements.map(element => ({
    height: element.getBoundingClientRect().height,
    lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
    width: element.getBoundingClientRect().width,
    scrollWidth: element.scrollWidth,
  })));
  expect(boxes.every(box => box.height >= 44 && box.scrollWidth <= box.width + 1)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('legacy statement links open the current portfolio after retiring duplicate views', async ({ page }) => {
  await page.goto('/#investments');
  await expect(page.getByRole('group', { name: 'Portfolio sections' }).getByRole('button')).toHaveText(['Holdings', 'Accounts']);
  await expect(page.getByRole('button', { name: /Saved snapshots|Statement archive/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Add statements', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 1, name: 'Investments', exact: true })).toBeVisible();
  await expect(page.getByText('No investment accounts are available in the saved portfolio.')).toBeVisible();
  await page.getByRole('button', { name: 'Set up an investment connection' }).click();
  await expect(page).toHaveURL(/#settings$/);
  await expect(page.locator('#settings-connections')).toHaveAttribute('open', '');
  await page.goto('/#investments');
  await expect(page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('button', { name: 'Statement archive', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('No investment accounts are available in the saved portfolio.')).toBeVisible();
});


test('retired upload bookmarks open connected accounts and leave saved history unchanged', async ({ page, request }) => {
  const before = await (await request.get('/api/transactions')).json();
  await page.goto('/#import');
  await expect(page.getByRole('heading', { level: 1, name: 'Banks & accounts', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#banking$/);
  await expect(page.locator('input[type="file"]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Import', exact: true })).toHaveCount(0);
  await expect(page.getByRole('option', { name: 'Import', exact: true })).toHaveCount(0);
  await go(page, 'transactions', 'Transactions');
  await expect(page.getByRole('button', { name: 'Add statements', exact: true })).toHaveCount(0);
  await page.goBack();
  await expect(page.getByRole('heading', { level: 1, name: 'Banks & accounts', exact: true })).toBeVisible();
  const rejected = await request.post('/api/upload', { multipart: { files: { name: 'test.csv', mimeType: 'text/csv', buffer: Buffer.from('Action,Time\n') } } });
  expect(rejected.status()).toBe(410);
  expect((await (await request.get('/api/transactions')).json()).total).toBe(before.total);
});

test('saved import preference opens connected accounts after the upload page is retired', async ({ page }) => {
  await page.goto('/');
  await page.evaluate(() => sessionStorage.setItem('ledger.tab', 'import'));
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Banks & accounts', exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#banking$/);
});
