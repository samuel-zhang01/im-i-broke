import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

test('skip to content keeps the current banking route and focuses its content', async ({ page }) => {
  await page.goto('/#transactions');
  await expect(page.getByRole('textbox', { name: 'Search transactions', exact: true })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#transactions$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Transactions', exact: true })).toBeVisible();
  await expect(page.locator('#main-content')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('navigation', { name: 'Activity sections' }).getByRole('button', { name: 'Transactions', exact: true })).toBeFocused();
});

test('recurring evidence receives keyboard focus and closing returns to its opener', async ({ page }) => {
  await page.route('**/api/recurring?**', route => route.fulfill({ json: {
    patterns: [{
      key: 'keyboard-payment', merchant: 'QA Keyboard bill', account: 'QA Current', category: 'Bills',
      cadence: 'monthly', cadence_days: 30, typical_amount: 12, current_amount: 12, monthly_cost: 12,
      first_seen: '2026-06-01', last_seen: '2026-08-01', n_occurrences: 3, txn_ids: [1, 2, 3],
      pattern_type: 'regular_payment', forecast_eligible: false, data_stale: true, status: 'active',
      payments: [{ id: 3, txn_date: '2026-08-01', description: 'QA BILL DIRECT DEBIT', category: 'Bills', amount: -12 }],
    }], candidates: [],
  } }));
  await page.goto('/#recurring');
  const opener = page.getByRole('button', { name: 'View payments for QA Keyboard bill in QA Current', exact: true });
  await opener.focus();
  await page.keyboard.press('Enter');
  const history = page.getByRole('region', { name: 'Payment history', exact: true });
  await expect(history).toBeFocused();
  await page.keyboard.press('Tab');
  const close = history.getByRole('button', { name: 'Close history', exact: true });
  await expect(close).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(history).toHaveCount(0);
  await expect(opener).toBeFocused();
  await expect(page).toHaveURL(/#recurring$/);
});

test('review pagination returns to the results and filtering keeps focus in its field', async ({ page }) => {
  await page.route('**/api/anomalies?status=open', route => route.fulfill({ json: Array.from({ length: 30 }, (_, index) => ({
    id: index + 1, account: 'QA Current', txn_date: '2026-09-18', description: `QA flag ${index}`,
    merchant: `QA flag ${index}`, amount: -40, category: 'Shopping', reason: 'Compare earlier payments', z: 4, outcome: null,
  })) }));
  await page.goto('/#anomalies');
  const pager = page.getByRole('navigation', { name: 'Review queue pages', exact: true });
  const results = page.getByText('30 of 30 open flags', { exact: false });
  await pager.getByRole('button', { name: 'Next page →', exact: true }).click();
  await expect(pager).toContainText('13–24 of 30');
  await expect(results).toBeFocused();
  await expect(results).toContainText('Showing 13–24');
  const box = await results.boundingBox();
  expect(box && box.y).toBeGreaterThanOrEqual(55);
  expect(box && box.y + box.height).toBeLessThan(page.viewportSize()!.height - 70);
  await pager.getByRole('button', { name: '← Previous page', exact: true }).click();
  await expect(pager).toContainText('1–12 of 30');
  await expect(results).toBeFocused();
  // Model a delayed paint: typing into search can finish before paging's focus callback.
  await page.evaluate(() => {
    const request = window.requestAnimationFrame;
    const cancel = window.cancelAnimationFrame;
    const pending = new Map<number, FrameRequestCallback>();
    let nextId = -1;
    window.requestAnimationFrame = callback => {
      const id = nextId--;
      pending.set(id, callback);
      return id;
    };
    window.cancelAnimationFrame = id => {
      if (!pending.delete(id)) cancel.call(window, id);
    };
    (window as typeof window & { flushReviewFrames: () => number }).flushReviewFrames = () => {
      window.requestAnimationFrame = request;
      window.cancelAnimationFrame = cancel;
      const callbacks = [...pending.values()];
      pending.clear();
      callbacks.forEach(callback => callback(performance.now()));
      return callbacks.length;
    };
  });
  await pager.getByRole('button', { name: 'Next page →', exact: true }).click();
  await expect(pager).toContainText('13–24 of 30');
  const search = page.getByRole('textbox', { name: 'Search anomaly reason or merchant', exact: true });
  await search.fill('QA flag 29');
  await expect(pager).toContainText('1–1 of 1');
  await expect(search).toBeFocused();
  await page.evaluate(() => (window as typeof window & { flushReviewFrames: () => number }).flushReviewFrames());
  await expect(search).toBeFocused();
  await expect(page.getByRole('button', { name: 'Resolve QA flag 29', exact: true })).toBeVisible();
});
