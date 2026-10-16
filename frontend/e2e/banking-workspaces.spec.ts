import { revealMobileControls } from './navigation-helpers';
import { test, expect } from '@playwright/test';
import { navigateFinance } from './navigation-helpers';
import type { BankingData } from '../src/bankingApi';

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('grouped banking navigation preserves every destination and browser history', async ({ page }) => {
  await page.goto('/#transactions');
  const primary = page.locator('nav[aria-label="Main navigation"]');
  await expect(primary).toBeAttached();
  expect(await primary.locator('button').count()).toBeLessThanOrEqual(9);
  for (const [id, title] of [['anomalies', 'Review'], ['transfers', 'Transfers'], ['merchants', 'People & merchants'], ['recurring', 'Recurring'], ['planning', 'Planning'], ['advice', 'Savings']] as const) {
    await navigateFinance(page, id, title);
    await expect(page).toHaveURL(new RegExp(`#${id}$`));
  }
  await page.goBack();
  await expect(page).toHaveURL(/#recurring$/);
  await navigateFinance(page, 'transfers', 'Transfers');
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Transfers', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Activity sections', exact: true }).getByRole('button', { name: 'Transfers', exact: true })).toHaveAttribute('aria-current', 'page');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('bank directory is immediately searchable and health loads only on request', async ({ page }, info) => {
  let healthReads = 0;
  // Newly added cards have no observed balance. Their warnings must not displace search.
  const missingCards = Array.from({ length: 3 }, (_, index) => ({
    id: `qa-directory-missing-card-${index}`, bank: 'QA Manual cards', account_type: 'credit',
    display_name: `QA missing card ${index + 1}`, connected: false, currency: 'GBP', balance: null,
  }));
  await page.route('**/api/banking', async route => {
    const response = await route.fetch();
    const data = await response.json() as BankingData;
    const withMissingCards: BankingData = {
      ...data, configured: false, local_accounts: [...data.local_accounts, ...missingCards],
      balance_checks: [...(data.balance_checks ?? []), ...missingCards.map(card => ({
        account_id: card.id, name: card.display_name, currency: card.currency, reported_balance: null,
        status: 'unverified' as const, requires_review: true, statement_date: null, statement_balance: null,
        transaction_estimate: null, difference: null, pending_amount: 0, pending_count: 0, review_count: 0,
        potential_repayments: [], bank_as_of: null,
        message: 'No supported balance or debt observation is available. This balance is excluded from totals, not treated as zero.',
      }))],
    };
    await route.fulfill({ response, json: withMissingCards });
  });
  await page.route('**/api/banking/health', route => { healthReads++; return route.fulfill({ json: {
    configured: true, schema_ready: true, checked_at: '2026-09-18T10:00:00Z', read_only: true,
    freshness_basis: 'Fetch times are local observations; bank update time is unavailable.',
    cache: { total: 24 }, connections: { no_balance: 0, failed: 0, missing: 0 },
    review_queue: { awaiting_review: 1, awaiting_mapping: 0, pending_authorizations: 2, native_currency_records: 3 },
    freshness: { last_success_at: '2026-09-18T10:00:00Z', last_attempt_failed: false },
    scheduler: { enabled: true, running: false, expired_lease: false, interval_seconds: 900 },
    integrity_issues: { broken_links: 0 }, timings: { scope: 'This process only.', operations: {} },
  } }); });
  await page.goto('/#banking');
  const search = page.getByRole('searchbox', { name: 'Find bank or account', exact: true });
  await expect(search).toBeVisible();
  const box = await search.boundingBox();
  expect(box && box.y + box.height).toBeLessThan(page.viewportSize()!.height);
  const checks = page.getByRole('region', { name: 'Bank balance checks', exact: true });
  for (const card of missingCards) await expect(checks.getByText(card.display_name, { exact: true })).toBeVisible();
  expect(healthReads).toBe(0);
  await page.getByText('Connection health', { exact: true }).click();
  await expect(page.getByText('Local history link checks passed.', { exact: true })).toBeVisible();
  await expect(page.getByText('Fetch times are local observations; bank update time is unavailable.', { exact: true })).toBeVisible();
  expect(healthReads).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `../output/playwright/eda-banking-${info.project.name}.png`, fullPage: true });
});

test('savings evidence opens a private in-memory payment search', async ({ page }) => {
  await page.route('**/api/advice', route => route.fulfill({ json: [{ type: 'subscriptions', title: 'Recorded subscriptions', detail: 'Review saved payments before changing a service.', items: [{ merchant: 'QA Stream', monthly: 12 }], potential_monthly_saving: 12 }] }));
  await page.goto('/#advice');
  await page.getByRole('button', { name: /^QA Stream ·/ }).click();
  await expect(page).toHaveURL(/#recurring$/);
  await expect(page.getByRole('searchbox', { name: 'Find a payment', exact: true })).toHaveValue('QA Stream');
  expect(page.url()).not.toContain('QA');
});

test('review prioritizes stronger evidence and retains low-priority payments', async ({ page }, info) => {
  const row = { account: 'QA Current', txn_date: '2026-09-18', description: 'Synthetic purchase', amount: -600, category: 'Shopping', reason: 'Compare prior saved payments.', z: 4, kind: 'large_amount', outcome: null, history_count: 5, baseline_amount: 20, threshold_amount: 120, evidence_note: 'Only earlier dates inform this comparison.', comparison_transaction_ids: [11] };
  await page.route('**/api/anomalies?status=open', route => route.fulfill({ json: [
    { ...row, id: 1, merchant: 'QA Low', priority: 'low', priority_reason: 'An ordinary small repeated amount.' },
    { ...row, id: 2, merchant: 'QA High', priority: 'high', priority_reason: 'A substantial departure from earlier payments.' },
  ] }));
  await page.goto('/#anomalies');
  await expect(page.getByRole('button', { name: /^Resolve QA/ }).first()).toHaveAttribute('aria-label', 'Resolve QA High');
  const firstTitle = page.getByText('QA High', { exact: true });
  const firstPriority = page.getByText('Higher priority · A substantial departure from earlier payments.', { exact: true });
  const titleBox = await firstTitle.boundingBox(), priorityBox = await firstPriority.boundingBox();
  const quickNavigation = page.getByRole('navigation', { name: 'Quick navigation', exact: true });
  const visibleBottom = await quickNavigation.isVisible()
    ? (await quickNavigation.boundingBox())!.y : page.viewportSize()!.height;
  const warning = page.getByText('Flags can be ordinary spending.', { exact: false });
  await expect(warning).toBeVisible();
  const warningBox = await warning.boundingBox();
  expect(warningBox && warningBox.y + warningBox.height).toBeLessThan(visibleBottom);
  expect(titleBox && titleBox.y).toBeGreaterThanOrEqual(0);
  expect(titleBox && titleBox.y + titleBox.height).toBeLessThan(visibleBottom);
  expect(priorityBox && priorityBox.y).toBeGreaterThanOrEqual(0);
  expect(priorityBox && priorityBox.y + priorityBox.height).toBeLessThan(visibleBottom);
  await info.attach('first-review-viewport', {
    body: JSON.stringify({ viewport: page.viewportSize(), visibleBottom, titleBox, priorityBox, warningBox }),
    contentType: 'application/json',
  });
  await expect(page.getByRole('combobox', { name: 'Account', exact: true })).not.toBeVisible();
  await page.screenshot({ path: `../output/playwright/review-compact-${info.project.name}.png`, fullPage: false });
  await revealMobileControls(page, 'Review filters & sort');
  await page.getByText('More filters', { exact: true }).click();
  await page.getByRole('combobox', { name: 'Account', exact: true }).selectOption('QA Current');
  await page.getByRole('spinbutton', { name: 'Minimum amount in pounds', exact: true }).fill('700');
  await expect(page.getByText('No open reviews match these filters.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  await revealMobileControls(page, 'Review filters & sort');
  await page.getByText('More filters', { exact: true }).click();

  await page.getByText('View the earlier evidence', { exact: true }).first().click();
  await expect(page.getByText('Only earlier dates inform this comparison.', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Earlier payment 1', exact: true }).first()).toBeVisible();
  await revealMobileControls(page, 'Review filters & sort');
  await page.getByRole('combobox', { name: 'Review priority', exact: true }).selectOption('low');
  await expect(page.getByRole('button', { name: 'Resolve QA Low', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Resolve QA High', exact: true })).toHaveCount(0);
});
