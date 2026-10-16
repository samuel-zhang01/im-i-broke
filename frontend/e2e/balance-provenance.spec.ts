import { revealMobileControls } from './navigation-helpers';
import { expect, test } from '@playwright/test';

const check = {
  status: 'discrepancy', requires_review: true, currency: 'GBP',
  message: 'Reported card balance differs from the statement and subsequent activity. Check the bank balance setting.',
  statement_date: '2026-09-01', statement_balance: -2200, transaction_estimate: -1450,
  reported_balance: -7100, difference: -5650, pending_amount: 15, pending_count: 1,
  review_count: 0, potential_repayments: [], bank_as_of: null,
};
const estimatedValuation = {
  basis: 'statement_activity_estimate', estimated: true, current_unknown: false,
  effective_value: -1450, effective_as_of: '2026-09-18', reported_balance: -7100,
  reported_as_of: '2026-09-18', explanation: 'Uses the reconciled statement and subsequent booked activity. The provider balance is inconsistent.',
};
const unknownValuation = {
  basis: 'unknown', estimated: false, current_unknown: true,
  effective_value: null, effective_as_of: null, reported_balance: -1100,
  reported_as_of: '2026-08-01', explanation: 'A later repayment makes this old statement unsuitable for a current balance. Import a newer statement.',
};

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('net assets uses supported estimates, excludes unknown debt and reversibly archives investment sources', async ({ page }) => {
  let archived = false;
  const writes: unknown[] = [];
  const base = { institution: 'Demo', currency: 'GBP', default_included: true, exclusion_reason: null,
    as_of: '2026-09-18', stale: false, estimated: false, history_points: 1 };
  await page.route('**/api/wealth?**', route => {
    const accounts = [
      { ...base, id: 'card', name: 'Synthetic card', kind: 'credit', source: 'lunchflow', native_value: -1450, value: -1450, reported_native_value: -7100, reported_value: -7100, estimated: true, valuation: estimatedValuation, balance_check: check },
      { ...base, id: 'amex', name: 'Synthetic Amex', kind: 'credit', source: 'statement', native_value: null, value: null, reported_native_value: -1100, reported_value: -1100, valuation: unknownValuation, balance_check: { ...check, status: 'statement_only', reported_balance: -1100, transaction_estimate: null, potential_repayments: [{ date: '2026-09-04', amount: 1100 }] } },
      { ...base, id: 'investment', name: 'Synthetic investment', kind: 'investment', source: 'snaptrade', native_value: 10000, value: 10000, archived_by_user: archived, default_included: !archived },
      { ...base, id: 'unknown', name: 'Unknown connected value', kind: 'investment', source: 'snaptrade', native_value: null, value: null },
      { ...base, id: 'history', name: 'Historical activity', kind: 'investment', source: 'statement', native_value: null, value: null, history_only: true, default_included: false },
      { ...base, id: 'alias', name: 'Linked duplicate', kind: 'investment', source: 'statement', native_value: 10000, value: 10000, source_status: 'linked', linked_to: 'investment', default_included: false },
    ];
    return route.fulfill({ json: { generated_at: '2026-09-18T12:00:00Z', currency: 'GBP', currencies: ['GBP'], range: 'all',
      accounts, selected_account_ids: archived ? ['card', 'amex', 'unknown'] : ['card', 'amex', 'investment', 'unknown'],
      summary: { net_worth: archived ? -1450 : 8550, assets: archived ? 0 : 10000, liabilities: 1450, known_accounts: archived ? 1 : 2,
        selected_accounts: archived ? 3 : 4, missing_accounts: 2, stale_accounts: 0, partial: true, balance_check_accounts: 2, estimated_accounts: 1, unknown_liability_accounts: 1,
        change: null, change_pct: null, change_comparable: false },
      points: [], warnings: [], methodology: [], cache: { saved_at: null, stale: false, refresh_error: null } } });
  });
  await page.route('**/api/wealth/sources/archive', async route => {
    const body = route.request().postDataJSON();
    writes.push(body); archived = body.archived;
    await route.fulfill({ json: { ...body, changed: true } });
  });
  await page.goto('/#net-worth');
  await expect(page.getByRole('heading', { name: 'Estimated known net assets · GBP' })).toBeVisible();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£8,550.00');
  await expect(page.getByText('Estimated debt & negative balances', { exact: true })).toBeVisible();
  await expect(page.getByText('Partial total · includes estimated balances · debts subtracted', { exact: true })).toBeVisible();
  await expect(page.getByText('Current debt unknown for Synthetic Amex; excluded from this partial total, not treated as paid off.', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Debt balance details');
  await expect(page.getByRole('heading', { name: 'Debt balances' })).toBeVisible();
  const cardDebt = page.locator('li').filter({ has: page.getByRole('button', { name: 'Synthetic card', exact: true }) });
  await expect(cardDebt.locator('details')).not.toHaveAttribute('open');
  await cardDebt.getByText('Balance details', { exact: true }).click();
  await expect(cardDebt).toContainText('Provider-reported balance: -£7,100.00');
  await expect(cardDebt).toContainText('This estimate is used in the total.');
  await expect(cardDebt).toContainText('Pending activity: £15.00');
  await expect(page.locator('li').filter({ has: page.getByRole('button', { name: 'Synthetic Amex', exact: true }) })).toContainText('Previous statement balance: -£1,100.00');
  await expect(page.getByRole('link', { name: 'Check Lunch Flow balance settings ↗' })).toHaveAttribute('href', 'https://www.lunchflow.app/docs/guides/configuration/balance');
  await revealMobileControls(page, 'Accounts & currency');
  await page.locator('summary').filter({ hasText: /^Accounts · 4/ }).click();
  const selector = page.getByRole('group', { name: 'Accounts included in net assets' });
  await expect(selector.getByRole('checkbox', { name: /Unknown connected value/ })).toBeVisible();
  await expect(selector.getByRole('checkbox', { name: /Historical activity|Linked duplicate/ })).toHaveCount(0);
  await selector.getByRole('button', { name: 'Archive Synthetic investment', exact: true }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('-£1,450.00');
  await expect(selector).toBeHidden();
  await page.getByText('Archived sources · 2', { exact: true }).click();
  await expect(page.getByText('Synthetic investment archived.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Reinstate Synthetic investment', exact: true }).click();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£8,550.00');
  expect(writes).toEqual([{ source_id: 'investment', archived: true }, { source_id: 'investment', archived: false }]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('banking uses the same effective card estimate and makes unknown statement debt explicit', async ({ page }) => {
  const provider = (id: string, amount: number) => ({ id, account_id: id, institution_name: 'Demo Bank', name: id === 'card' ? 'Synthetic card' : 'Synthetic cash', currency: 'GBP',
    status: 'active', excluded: false, present: true, mapping_status: 'mapped', mapping_candidates: [], last_error: null,
    last_synced_at: '2026-09-18T12:00:00Z', balance: { amount, currency: 'GBP', fetched_at: '2026-09-18T12:00:00Z' },
    counts: { cached: 0, imported: 0, matched: 0, pending: 0, review: 0 } });
  await page.route('**/api/banking', route => route.fulfill({ json: {
    configured: true, accounts: [provider('cash', 2400), { ...provider('card', -1450), valuation: estimatedValuation }], local_accounts: [
      { id: 'cash', bank: 'Demo Bank', account_type: 'current', display_name: 'Synthetic cash', connected: true, currency: 'GBP', n_txns: 0 },
      { id: 'card', bank: 'Demo Bank', account_type: 'credit', display_name: 'Synthetic card', connected: true, currency: 'GBP', n_txns: 0 },
      { id: 'manual', bank: 'American Express', account_type: 'credit', display_name: 'Synthetic Amex', connected: false, balance: null, reported_balance: -1100, valuation: unknownValuation, balance_asof: '2026-08-01', currency: 'GBP', n_txns: 0 },
    ], institutions: [], totals_by_currency: { GBP: 950 },
    balance_totals_by_currency: { GBP: { assets: 2400, liabilities: 1450, net: 950 } },
    statement_liabilities_by_currency: {}, statement_reported_liabilities_by_currency: { GBP: 1100 }, estimated_accounts: 1, unknown_balance_accounts: 1, balance_checks: [{ account_id: 'card', name: 'Synthetic card', ...check }],
    sync: { last_attempt_at: null, last_success_at: null, error: null, running: false, interval_seconds: 3600 },
    stats: { accounts: 2, mapped: 2, pending: 0, review: 0, cached: 0, imported: 0 },
  } }));
  await page.goto('/#banking');
  const totals = page.getByRole('region', { name: 'Connected bank balances' });
  await expect(totals.getByRole('heading', { name: /^Estimated connected net balances/ })).toBeVisible();
  await expect(totals).toContainText('£2,400.00 − £1,450.00 = £950.00');
  await expect(totals).toContainText('Excludes investments and statement-only accounts.');
  await expect(totals).not.toContainText('£7,100.00');
  await expect(totals).not.toContainText('£1,100.00');
  await expect(totals).toContainText('Current balance unknown for Synthetic Amex. Unknown balances are excluded from totals, not treated as zero.');
  await expect(page.getByRole('button', { name: 'Open Synthetic card, GBP', exact: true })).toContainText('Estimated booked balance');
  await expect(page.getByRole('button', { name: 'Open Synthetic Amex, GBP', exact: true })).toContainText('Unknown');
  const warning = page.getByRole('region', { name: 'Bank balance checks' });
  await expect(warning).toContainText(estimatedValuation.explanation);
  await expect(warning.getByRole('link', { name: 'Check Lunch Flow balance settings ↗' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await totals.getByRole('button', { name: 'View full net assets and debt sources' }).click();
  await expect(page).toHaveURL(/#net-worth$/);
});

const availableCreditValuation = {
  basis: 'available_credit', estimated: false, current_unknown: false,
  effective_value: -1604.35, effective_as_of: '2026-09-18', reported_balance: -7145.65,
  reported_as_of: '2026-09-18', reported_source: 'lunchflow', available_credit: 7145.65,
  credit_limit: 8750, credit_limit_confirmed_at: '2026-09-18T12:00:00Z',
  explanation: 'Credit used equals the confirmed limit minus available credit.',
};
const availableCreditCheck = { ...check, status: 'available_credit', requires_review: false, reported_balance: -7145.65 };

test('confirmed available credit shows the source equation without a statement estimate or double-counted pending charges', async ({ page }) => {
  const base = { institution: 'Demo', currency: 'GBP', default_included: true, exclusion_reason: null, as_of: '2026-09-18', stale: false, estimated: false, history_points: 1 };
  await page.route('**/api/wealth?**', route => route.fulfill({ json: {
    generated_at: '2026-09-18T12:00:00Z', currency: 'GBP', currencies: ['GBP'], range: 'all', selected_account_ids: ['card', 'cash', 'amex'],
    accounts: [
      { ...base, id: 'card', name: 'Synthetic card', kind: 'credit', source: 'lunchflow', native_value: -1604.35, value: -1604.35, reported_native_value: -7145.65, reported_value: -7145.65, valuation: availableCreditValuation, balance_check: availableCreditCheck },
      { ...base, id: 'cash', name: 'Synthetic cash', kind: 'cash', source: 'lunchflow', native_value: 2447.60, value: 2447.60 },
      { ...base, id: 'amex', name: 'Synthetic Amex', kind: 'credit', source: 'statement', native_value: null, value: null, reported_native_value: -1100, valuation: unknownValuation, balance_check: { ...check, status: 'statement_only' } },
    ], summary: { net_worth: 843.25, assets: 2447.60, liabilities: 1604.35, known_accounts: 2, selected_accounts: 3, missing_accounts: 1, stale_accounts: 0, partial: true, balance_check_accounts: 1, estimated_accounts: 0, unknown_liability_accounts: 1, change: null, change_pct: null, change_comparable: false },
    points: [], warnings: [], methodology: [], cache: { saved_at: null, stale: false, refresh_error: null },
  } }));
  await page.goto('/#net-worth');
  await expect(page.getByRole('heading', { name: 'Known net assets · GBP', exact: true })).toBeVisible();
  await expect(page.getByLabel('Net assets value', { exact: true })).toHaveText('£843.25');
  await expect(page.getByText('Debt & negative balances', { exact: true })).toBeVisible();
  await revealMobileControls(page, 'Debt balance details');
  const card = page.locator('li').filter({ has: page.getByRole('button', { name: 'Synthetic card', exact: true }) });
  await expect(card.locator('details')).not.toHaveAttribute('open');
  await card.getByText('Balance details', { exact: true }).click();
  await expect(card).toContainText('£8,750.00 credit limit − £7,145.65 available credit = £1,604.35 credit used.');
  await expect(card).toContainText('Credit limit confirmed by you on');
  await expect(card).toContainText('Available credit may already reflect pending charges; they are not added again.');
  await expect(card).not.toContainText('estimate');
  await expect(card).not.toContainText('-£7,145.65');
  await expect(card).not.toContainText('excluded');
  await expect(card.getByRole('link', { name: 'Check Lunch Flow balance settings ↗' })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('available-credit account settings update the limit and can disable the rule while history stays available credit', async ({ page }) => {
  let creditLimit: number | null = 8750;
  const writes: unknown[] = [];
  const makeAccount = () => ({ id: 'card', account_id: 'card', institution_name: 'Demo Bank', name: 'Synthetic card', currency: 'GBP',
    status: 'active', excluded: false, present: true, mapping_status: 'mapped', mapping_candidates: [], last_error: null, balance_multiplier: -1,
    last_synced_at: '2026-09-18T12:00:00Z', counts: { cached: 0, imported: 0, matched: 0, pending: 1, review: 0 },
    balance: { amount: creditLimit === null ? -1450 : 7145.65 - creditLimit, currency: 'GBP', fetched_at: '2026-09-18T12:00:00Z' },
    balance_rule: { balance_type: creditLimit === null ? 'reported' : 'available_credit', provider_id: 'card', account_id: 'card', currency: 'GBP', credit_limit: creditLimit, confirmed_at: '2026-09-18T12:00:00Z' },
    valuation: creditLimit === null ? estimatedValuation : { ...availableCreditValuation, credit_limit: creditLimit, effective_value: 7145.65 - creditLimit },
    balance_check: creditLimit === null ? check : availableCreditCheck,
  });
  const summary = () => ({ configured: true, accounts: [makeAccount()], local_accounts: [{ id: 'card', bank: 'Demo Bank', account_type: 'credit', display_name: 'Synthetic card', connected: true, currency: 'GBP', n_txns: 0 }], institutions: [], totals_by_currency: { GBP: makeAccount().balance.amount }, balance_checks: [], sync: { last_attempt_at: null, last_success_at: null, error: null, running: false, interval_seconds: 3600 }, stats: { accounts: 1, mapped: 1, pending: 1, review: 0, cached: 0, imported: 0 } });
  await page.route('**/api/banking', route => route.fulfill({ json: summary() }));
  await page.route('**/api/banking/accounts/card?**', route => route.fulfill({ json: { account: makeAccount(), transactions: [], total: 0, reviews: [], balance_history: [{ amount: -7145.65, currency: 'GBP', fetched_at: '2026-09-18T12:00:00Z' }] } }));
  await page.route('**/api/transactions?**', route => route.fulfill({ json: { items: [], total: 0, sum_in: 0, sum_out: 0 } }));
  await page.route('**/api/banking/accounts/card/balance-rule', route => {
    const body = route.request().postDataJSON();
    writes.push(body); creditLimit = body.balance_type === 'reported' ? null : body.credit_limit;
    return route.fulfill({ json: summary() });
  });
  await page.goto('/#banking');
  await page.getByRole('button', { name: 'Open Synthetic card, GBP', exact: true }).click();
  const balance = page.getByRole('region', { name: 'Account balance', exact: true });
  await expect(balance).toContainText('£8,750.00 credit limit − £7,145.65 available credit = £1,604.35 credit used.');
  await expect(balance).not.toContainText('estimate');
  await expect(balance).not.toContainText('-£7,145.65');
  await expect(page.getByText('Available credit history', { exact: true })).toBeVisible();
  await page.getByText('Exact observations', { exact: true }).click();
  await expect(page.locator('figure').filter({ hasText: 'Available credit history' })).toContainText('£7,145.65');
  await expect(page.locator('figure').filter({ hasText: 'Available credit history' })).not.toContainText('-£7,145.65');
  await page.getByText('Credit card balance settings', { exact: true }).click();
  await page.getByLabel('Confirmed credit limit · GBP', { exact: true }).fill('8751');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(balance).toContainText('£8,751.00 credit limit − £7,145.65 available credit = £1,605.35 credit used.');
  await page.getByRole('combobox', { name: 'Bank feed balance type', exact: true }).selectOption('reported');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(balance).toContainText('Estimated booked balance');
  await expect(page.getByRole('combobox', { name: 'Bank feed balance type', exact: true })).toHaveValue('reported');
  await page.reload();
  await page.getByRole('button', { name: 'Open Synthetic card, GBP', exact: true }).click();
  await page.getByText('Credit card balance settings', { exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Bank feed balance type', exact: true })).toHaveValue('reported');
  await expect(page.getByLabel('Confirmed credit limit · GBP', { exact: true })).toHaveCount(0);
  expect(writes).toEqual([{ balance_type: 'available_credit', credit_limit: 8751, currency: 'GBP' }, { balance_type: 'reported' }]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});
