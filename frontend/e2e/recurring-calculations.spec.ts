import { revealMobileControls } from './navigation-helpers';
import { test, expect } from '@playwright/test';

test('regular payments keep open timing windows and explain their observed amounts', async ({ page }, info) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
  await page.clock.setFixedTime(new Date('2026-09-18T12:00:00Z'));
  const base = {
    key: 'waiting', merchant: 'QA Utility', account: 'QA Current', category: 'Bills',
    cadence: 'monthly', cadence_days: 30, typical_amount: 23, current_amount: 27.97,
    monthly_cost: 27.97, historical_monthly_cost: 23, first_seen: '2026-01-01',
    last_seen: '2026-08-17', n_occurrences: 8, txn_ids: [1, 2, 3, 4, 5, 6, 7, 8],
    next_expected: '2026-09-17', next_expected_earliest: '2026-09-15', next_expected_latest: '2026-09-20',
    pattern_type: 'regular_payment', forecast_eligible: true, status: 'price_change',
    amount_min: 27.9, amount_max: 28.04,
    amount_basis: 'Median of the two payments after the confirmed amount change.',
    schedule_basis: 'Observed day of the month, adjusted for shorter months.',
    schedule_evidence_count: 6, schedule_start: '2026-03-17',
    price_change_confirmed: true, price_change_percent: 21.6,
    price_change_evidence: 2,
    merchant_labels: ['QA UTILITY LTD', 'QA BILLING LIMITED'],
    forecast_reason: 'Recent regular payment evidence supports this estimate.',
    payments: ['2026-01-01', '2026-02-03', '2026-03-17', '2026-04-17', '2026-05-17', '2026-06-17', '2026-07-17', '2026-08-17']
      .map((txn_date, index) => ({ id: index + 1, txn_date, description: `QA UTILITY LTD DD ${String(index + 1).padStart(2, '0')}26`, category: 'Bills', amount: index === 7 ? -28.04 : index === 6 ? -27.90 : -23 }))
      .reverse(),
  };
  await page.route('**/api/recurring?**', route => route.fulfill({ json: {
    patterns: [base, { ...base, key: 'ended', merchant: 'QA Ended', status: 'ended',
      forecast_eligible: false, next_expected: null, monthly_cost: 0,
      historical_monthly_cost: 18.5, merchant_labels: ['QA ENDED ORIGINAL'], missed_cycles: 2,
    }, { ...base, key: 'expired', merchant: 'QA Expired', next_expected_latest: '2026-09-17' }],
    candidates: [],
  } }));
  await page.addInitScript(() => sessionStorage.setItem('ledger.tab', 'recurring'));
  await page.goto('/');

  const upcoming = page.getByRole('button', { name: 'View upcoming QA Utility payment', exact: true });
  await expect(upcoming).toBeVisible();
  await expect(upcoming).toContainText('Checking for payment');
  await expect(upcoming).toContainText('15 Sept');
  await expect(upcoming).toContainText('20 Sept');
  await expect(page.getByRole('button', { name: 'View upcoming QA Expired payment', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'View upcoming QA Ended payment', exact: true })).toHaveCount(0);
  if (info.project.name === 'desktop') {
    const row = page.getByRole('region', { name: 'Recurring payment records', exact: true }).getByRole('row')
      .filter({ has: page.getByRole('button', { name: 'View payments for QA Utility in QA Current', exact: true }) });
    await expect(row).toContainText('QA Current · Bills');
    await expect(row).toContainText('8 dates');
    expect((await row.boundingBox())!.height).toBeLessThanOrEqual(64);
  }
  await upcoming.click();
  await expect(page.getByText('Amount estimate: Median of the two payments after the confirmed amount change.', { exact: true })).toBeVisible();
  await expect(page.getByText(/Observed amounts supporting this estimate: £27.90 – £28.04/)).toBeVisible();
  await expect(page.getByText('Confirmed amount change (+21.6%): supported by 2 recent payments.', { exact: true })).toBeVisible();
  await expect(page.getByText(/Timing is supported by 6 of 8 payment dates, from 17 Mar 26/)).toBeVisible();
  await expect(page.getByRole('cell', { name: 'QA UTILITY LTD DD 0826 Bills', exact: true })).toBeVisible();
  await page.screenshot({ path: `../output/recurring-evidence-${info.project.name}.png`, fullPage: true });
  await page.getByRole('button', { name: 'Close history', exact: true }).click();

  await page.getByRole('searchbox', { name: 'Find a payment', exact: true }).fill('QA BILLING LIMITED');
  await expect(page.getByRole('button', { name: 'View payments for QA Utility in QA Current', exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Find a payment', exact: true }).fill('');
  await revealMobileControls(page, 'Payment filters & sort');
  await page.getByRole('combobox', { name: 'Evidence', exact: true }).selectOption('ended');
  await expect(page.getByText('£18.50')).toBeVisible();
  await page.getByRole('button', { name: 'View payments for QA Ended in QA Current', exact: true }).click();
  await expect(page.getByText(/2 expected cycles have no matching payment/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);

  await page.getByText('Detection settings', { exact: true }).first().click();
  await expect(page.getByRole('combobox', { name: 'Price-change threshold', exact: true })).toHaveValue('0.1');
});
