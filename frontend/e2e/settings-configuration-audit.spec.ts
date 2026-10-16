import { expect, test } from '@playwright/test';

test('credit cards can be configured independently from Settings and restored after reload', async ({ page }) => {
  const cards = [
    { id: 'synthetic-hsbc', account_id: 'local-card-one', name: 'Card ending 1234', institution_name: 'HSBC', currency: 'GBP' },
    { id: 'synthetic-other', account_id: 'local-card-two', name: 'Travel card', institution_name: 'HSBC', currency: 'EUR' },
  ].map(card => ({ ...card, excluded: false, balance_rule: null as null | Record<string, unknown> }));
  const banking = () => ({ accounts: cards, local_accounts: [
    { id: 'local-card-one', account_type: 'credit', display_name: 'Everyday card' },
    { id: 'local-card-two', account_type: 'credit', display_name: 'Travel card' },
  ], sync: {}, configured: true });
  let saved: unknown;
  await page.route('**/api/banking', route => route.fulfill({ json: banking() }));
  await page.route('**/api/banking/accounts/*/balance-rule', route => {
    const id = route.request().url().split('/').at(-2);
    const card = cards.find(value => value.id === id)!;
    saved = route.request().postDataJSON();
    card.balance_rule = { ...route.request().postDataJSON(), confirmed_at: '2026-10-01T12:00:00Z' };
    return route.fulfill({ json: banking() });
  });
  await page.goto('/#settings');
  await page.locator('#settings-cards > summary').click();
  const selector = page.getByRole('combobox', { name: 'Connected credit card', exact: true });
  await selector.selectOption('synthetic-hsbc');
  const mode = page.getByRole('combobox', { name: 'Bank feed balance type', exact: true });
  await mode.selectOption('available_credit');
  const limit = page.getByRole('spinbutton', { name: 'Confirmed credit limit · GBP', exact: true });
  await limit.fill('4000');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Balance setting saved.' })).toBeVisible();
  expect(saved).toEqual({ balance_type: 'available_credit', credit_limit: 4000, currency: 'GBP' });
  await selector.selectOption('synthetic-other');
  await expect(mode).toHaveValue('reported');
  await mode.selectOption('available_credit');
  const otherLimit = page.getByRole('spinbutton', { name: 'Confirmed credit limit · EUR', exact: true });
  await expect(otherLimit).toHaveValue('');
  await expect(page.getByRole('button', { name: 'Save balance setting', exact: true })).toBeDisabled();
  await otherLimit.fill('0');
  await expect(page.getByRole('button', { name: 'Save balance setting', exact: true })).toBeDisabled();
  await otherLimit.fill('6000');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Balance setting saved.' })).toBeVisible();
  expect(saved).toEqual({ balance_type: 'available_credit', credit_limit: 6000, currency: 'EUR' });
  await page.reload();
  await page.locator('#settings-cards > summary').click();
  await selector.selectOption('synthetic-hsbc');
  await expect(mode).toHaveValue('available_credit');
  await expect(limit).toHaveValue('4000');
  await selector.selectOption('synthetic-other');
  await expect(otherLimit).toHaveValue('6000');
  expect(cards[1].balance_rule?.currency).toBe('EUR');
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test('workspace preference fields stay locked until a delayed save completes', async ({ page }) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let preferences = { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' };
  await page.route('**/api/workspace/preferences', async route => {
    if (route.request().method() === 'PATCH') {
      const submitted = route.request().postDataJSON();
      await pending;
      preferences = { ...preferences, ...submitted };
    }
    await route.fulfill({ json: preferences });
  });
  await page.goto('/#settings');
  await page.getByText('Workspace preferences', { exact: true }).click();
  await page.getByLabel('Reporting currency', { exact: true }).fill('EUR');
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByLabel('Reporting currency', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('Locale', { exact: true })).toBeDisabled();
  await expect(page.getByLabel('Time zone', { exact: true })).toBeDisabled();
  release();
  await expect(page.getByRole('status').filter({ hasText: 'Preferences saved.' })).toBeVisible();
  await expect(page.getByLabel('Reporting currency', { exact: true })).toBeEnabled();
  await page.getByLabel('Reporting currency', { exact: true }).fill('USD');
  await expect(page.getByLabel('Reporting currency', { exact: true })).toHaveValue('USD');
  await expect(page.getByRole('status').filter({ hasText: 'Preferences saved.' })).toHaveCount(0);
  expect(preferences.reporting_currency).toBe('EUR');
});

test('manual credit setup explains debts, confirms the saved balance and recovers an empty search', async ({ page }, info) => {
  const name = `Manual card ${info.project.name}`;
  await page.goto('/#settings');
  await page.getByText('Account settings', { exact: true }).click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await page.getByLabel('Account name', { exact: true }).fill(name);
  await page.getByLabel('Institution', { exact: true }).fill('Example Bank');
  await page.getByRole('combobox', { name: 'Account type', exact: true }).selectOption({ label: 'Credit card' });
  await page.getByLabel('Account currency', { exact: true }).fill('EUR');
  await expect(page.getByText('A credit limit is not a balance.', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  await page.getByRole('button', { name: `Record balance for ${name}`, exact: true }).click();
  await expect(page.getByText('Do not enter your credit limit or available credit.', { exact: false })).toBeVisible();
  await page.getByLabel('Balance (EUR)', { exact: true }).fill('-250');
  await page.getByLabel('Balance date', { exact: true }).fill('2026-08-01');
  await page.getByRole('button', { name: 'Save balance', exact: true }).click();
  const card = page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) });
  await expect(card).toContainText('Latest manual balance: -€250.00');
  await expect(card).toContainText('2026-08-01');
  await page.getByLabel('Find an account', { exact: true }).fill('no-account-matches-this-query');
  await expect(page.getByRole('status').filter({ hasText: 'No accounts match your search.' })).toBeVisible();
  await page.getByRole('button', { name: 'Clear account search', exact: true }).click();
  await expect(card).toBeVisible();
  await page.getByLabel('Find an account', { exact: true }).fill(' EUR ');
  await expect(card).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

for (const outcome of ['success', 'failure'] as const) {
  test(`category fields stay locked during delayed ${outcome} and failed drafts can be retried`, async ({ page }) => {
    const category = { id: 'synthetic-category', name: 'Original category', treatment: 'normal', sort_order: 0, archived: false };
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    let attempts = 0;
    await page.route('**/api/workspace/categories', route => route.fulfill({ json: { categories: [category] } }));
    await page.route('**/api/workspace/categories/synthetic-category', async route => {
      const submitted = route.request().postDataJSON();
      attempts++;
      if (attempts === 1) await pending;
      if (outcome === 'failure' && attempts === 1) {
        await route.fulfill({ status: 503, json: { detail: 'Synthetic save unavailable. Try again.' } });
      } else {
        Object.assign(category, submitted);
        await route.fulfill({ json: category });
      }
    });
    await page.goto('/#settings');
    await page.locator('#settings-categories > summary').click();
    await page.getByRole('button', { name: 'Edit category Original category', exact: true }).click();
    await page.getByLabel('Category name', { exact: true }).fill('Retained draft');
    await page.getByRole('combobox', { name: 'Treatment', exact: true }).selectOption('income');
    await page.getByLabel('Sort order', { exact: true }).fill('42');
    await page.getByLabel('Archive category', { exact: true }).check();
    await page.getByRole('button', { name: 'Save category', exact: true }).click();
    try {
      for (const name of ['Category name', 'Treatment', 'Sort order', 'Archive category']) {
        await expect(name === 'Treatment' ? page.getByRole('combobox', { name, exact: true }) : page.getByLabel(name, { exact: true })).toBeDisabled();
      }
      await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
      await expect(page.getByRole('button', { name: 'Edit category Original category', exact: true })).toBeDisabled();
    } finally { release(); }
    if (outcome === 'failure') {
      await expect(page.getByRole('alert')).toContainText('Synthetic save unavailable. Try again.');
      await expect(page.getByLabel('Category name', { exact: true })).toBeEnabled();
      await expect(page.getByLabel('Category name', { exact: true })).toHaveValue('Retained draft');
      await expect(page.getByRole('combobox', { name: 'Treatment', exact: true })).toHaveValue('income');
      await expect(page.getByLabel('Sort order', { exact: true })).toHaveValue('42');
      await expect(page.getByLabel('Archive category', { exact: true })).toBeChecked();
      await page.getByRole('button', { name: 'Save category', exact: true }).click();
    }
    await expect(page.getByLabel('Category name', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Edit category Retained draft', exact: true })).toBeVisible();
    expect(category).toEqual({ id: 'synthetic-category', name: 'Retained draft', treatment: 'income', sort_order: 42, archived: true });
  });
}

test('failed preference save preserves the draft for retry', async ({ page }) => {
  let fail = true;
  let preferences = { reporting_currency: 'GBP', locale: 'en-GB', timezone: 'Europe/London' };
  await page.route('**/api/workspace/preferences', route => {
    if (route.request().method() === 'PATCH') {
      if (fail) return route.fulfill({ status: 503, json: { detail: 'Synthetic preference save failed.' } });
      preferences = { ...preferences, ...route.request().postDataJSON() };
    }
    return route.fulfill({ json: preferences });
  });
  await page.goto('/#settings');
  await page.locator('#settings-preferences > summary').click();
  await page.getByLabel('Locale', { exact: true }).fill('de-DE');
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Synthetic preference save failed.');
  await expect(page.getByLabel('Locale', { exact: true })).toHaveValue('de-DE');
  await expect(page.getByLabel('Locale', { exact: true })).toBeEnabled();
  fail = false;
  await page.getByRole('button', { name: 'Save workspace preferences', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Preferences saved.' })).toBeVisible();
});

test('delayed failed manual balance prevents switching accounts and preserves a retryable draft', async ({ page, request }, info) => {
  const name = `Delayed balance ${info.project.name}`;
  const created = await request.post('/api/workspace/accounts', { headers: { 'X-Ledger-Request': '1' }, data: {
    display_name: name, institution: 'Synthetic Bank', account_type: 'current', currency: 'GBP',
  } });
  expect(created.ok()).toBe(true);
  const account = await created.json();
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let fail = true;
  await page.route(`**/api/workspace/accounts/${encodeURIComponent(account.id)}/balances`, async route => {
    if (fail) { await pending; await route.fulfill({ status: 503, json: { detail: 'Synthetic balance save failed.' } }); }
    else await route.continue();
  });
  await page.goto('/#settings');
  await page.locator('#settings-accounts > summary').click();
  await page.getByRole('button', { name: `Record balance for ${name}`, exact: true }).click();
  await page.getByLabel('Balance (GBP)', { exact: true }).fill('123.45');
  await page.getByLabel('Balance date', { exact: true }).fill('2026-08-01');
  await page.getByRole('button', { name: 'Save balance', exact: true }).click();
  try {
    await expect(page.getByLabel('Balance (GBP)', { exact: true })).toBeDisabled();
    await expect(page.getByLabel('Balance date', { exact: true })).toBeDisabled();
    for (const prefix of ['Edit account', 'Record balance for', 'History coverage for']) {
      const buttons = page.getByRole('button', { name: new RegExp(`^${prefix} `) });
      for (const button of await buttons.all()) await expect(button).toBeDisabled();
    }
  } finally { release(); }
  await expect(page.getByRole('alert')).toContainText('Synthetic balance save failed.');
  await expect(page.getByLabel('Balance (GBP)', { exact: true })).toHaveValue('123.45');
  await expect(page.getByLabel('Balance date', { exact: true })).toHaveValue('2026-08-01');
  await expect(page.getByLabel('Balance (GBP)', { exact: true })).toBeEnabled();
  fail = false;
  await page.getByRole('button', { name: 'Save balance', exact: true }).click();
  const row = page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) });
  await expect(row).toContainText('Latest manual balance: £123.45');
  await page.reload();
  await page.locator('#settings-accounts > summary').click();
  await expect(row).toContainText('Latest manual balance: £123.45');
});

test('a delayed card save failure retains its currency and limit for retry', async ({ page }) => {
  const card = { id: 'synthetic-failure-card', account_id: 'local-failure-card', name: 'Synthetic HSBC card', institution_name: 'HSBC', currency: 'GBP', excluded: false, balance_rule: null as null | Record<string, unknown> };
  const banking = () => ({ accounts: [card], local_accounts: [{ id: card.account_id, account_type: 'credit', display_name: card.name }], sync: {}, configured: true });
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let fail = true;
  await page.route('**/api/banking', route => route.fulfill({ json: banking() }));
  await page.route('**/api/banking/accounts/synthetic-failure-card/balance-rule', async route => {
    if (fail) { await pending; await route.fulfill({ status: 503, json: { detail: 'Synthetic card save failed.' } }); }
    else {
      card.balance_rule = { ...route.request().postDataJSON(), confirmed_at: '2026-10-04T12:00:00Z' };
      await route.fulfill({ json: banking() });
    }
  });
  await page.goto('/#settings');
  await page.locator('#settings-cards > summary').click();
  await page.getByRole('combobox', { name: 'Connected credit card', exact: true }).selectOption(card.id);
  const mode = page.getByRole('combobox', { name: 'Bank feed balance type', exact: true });
  await mode.selectOption('available_credit');
  const limit = page.getByRole('spinbutton', { name: 'Confirmed credit limit · GBP', exact: true });
  await limit.fill('7500');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  try {
    await expect(mode).toBeDisabled();
    await expect(limit).toBeDisabled();
  } finally { release(); }
  await expect(page.getByRole('alert')).toContainText('Bank data could not be updated (503). Your saved history is still available.');
  await expect(mode).toHaveValue('available_credit');
  await expect(limit).toHaveValue('7500');
  await expect(limit).toBeEnabled();
  expect(card.balance_rule).toBeNull();
  fail = false;
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Balance setting saved.' })).toBeVisible();
  await page.reload();
  await page.locator('#settings-cards > summary').click();
  await page.getByRole('combobox', { name: 'Connected credit card', exact: true }).selectOption(card.id);
  await expect(limit).toHaveValue('7500');
});

test('pending manual account creation prevents switching and failure preserves the form', async ({ page }, info) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  let fail = true;
  await page.route('**/api/workspace/accounts', async route => {
    if (route.request().method() === 'POST' && fail) {
      await pending;
      await route.fulfill({ status: 503, json: { detail: 'Synthetic account creation failed.' } });
    } else await route.continue();
  });
  const name = `Retried account ${info.project.name}`;
  await page.goto('/#settings');
  await page.locator('#settings-accounts > summary').click();
  await page.getByRole('button', { name: 'Add manual account', exact: true }).click();
  await page.getByLabel('Account name', { exact: true }).fill(name);
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  try {
    await expect(page.getByLabel('Account name', { exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
    for (const button of await page.getByRole('button', { name: /^Edit account / }).all()) await expect(button).toBeDisabled();
  } finally { release(); }
  await expect(page.getByRole('alert')).toContainText('Synthetic account creation failed.');
  await expect(page.getByLabel('Account name', { exact: true })).toHaveValue(name);
  await expect(page.getByLabel('Account name', { exact: true })).toBeEnabled();
  fail = false;
  await page.getByRole('button', { name: 'Create manual account', exact: true }).click();
  await expect(page.getByRole('button', { name: `Edit account ${name}`, exact: true })).toBeVisible();
});

for (const currency of ['KWD', 'JPY']) {
  test(`${currency} card limits and native debt use the currency's supported precision`, async ({ page, request }, info) => {
    const headers = { 'X-Ledger-Request': '1' };
    const name = `Precision card ${currency} ${info.project.name}`;
    const created = await request.post('/api/workspace/accounts', { headers, data: { display_name: name, institution: 'Synthetic Precision Bank', account_type: 'credit', currency } });
    expect(created.ok(), await created.text()).toBe(true);
    const account = await created.json();
    const calendar = await (await request.get('/api/workspace/calendar')).json();
    const balance = await request.post(`/api/workspace/accounts/${account.id}/balances`, { headers, data: { amount: currency === 'KWD' ? -1.664 : -1664, currency, as_of: calendar.today } });
    expect(balance.ok(), await balance.text()).toBe(true);
    const card = { id: `synthetic-precision-${currency}`, account_id: account.id, name, institution_name: 'Synthetic Precision Bank', currency, excluded: false, balance_rule: null as null | Record<string, unknown> };
    const banking = () => ({ accounts: [card], local_accounts: [{ id: account.id, account_type: 'credit', display_name: name }], sync: {}, configured: true });
    let submitted: unknown;
    await page.route('**/api/banking', route => route.fulfill({ json: banking() }));
    await page.route(`**/api/banking/accounts/${card.id}/balance-rule`, route => {
      submitted = route.request().postDataJSON();
      card.balance_rule = { ...route.request().postDataJSON(), confirmed_at: '2026-10-04T12:00:00Z' };
      return route.fulfill({ json: banking() });
    });
    await page.goto('/#settings');
    await page.locator('#settings-accounts > summary').click();
    const item = page.locator('li').filter({ has: page.getByRole('button', { name: `Edit account ${name}`, exact: true }) });
    await expect(item).toContainText(currency === 'KWD' ? '1.664' : '1,664');
    if (currency === 'JPY') await expect(item).not.toContainText('1,664.00');
    await page.locator('#settings-cards > summary').click();
    await page.getByRole('combobox', { name: 'Connected credit card', exact: true }).selectOption(card.id);
    await page.getByRole('combobox', { name: 'Bank feed balance type', exact: true }).selectOption('available_credit');
    const limit = page.getByRole('spinbutton', { name: `Confirmed credit limit · ${currency}`, exact: true });
    await expect(limit).toHaveAttribute('min', currency === 'KWD' ? '0.001' : '1');
    await expect(limit).toHaveAttribute('step', currency === 'KWD' ? '0.001' : '1');
    const value = currency === 'KWD' ? '1000.123' : '1000';
    await limit.fill(value);
    expect(await limit.evaluate(element => (element as HTMLInputElement).checkValidity())).toBe(true);
    await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: 'Balance setting saved.' })).toBeVisible();
    expect(submitted).toEqual({ balance_type: 'available_credit', credit_limit: Number(value), currency });
    await page.reload();
    await page.locator('#settings-cards > summary').click();
    await page.getByRole('combobox', { name: 'Connected credit card', exact: true }).selectOption(card.id);
    await expect(limit).toHaveValue(value);
  });
}

test('a previously saved fractional JPY limit stays visible until reconfirmed in whole units', async ({ page }) => {
  const card = { id: 'synthetic-legacy-jpy', account_id: 'synthetic-legacy-local', name: 'Synthetic legacy card', institution_name: 'Synthetic Legacy Bank', currency: 'JPY', excluded: false,
    balance_rule: { balance_type: 'available_credit', credit_limit: 1000.12, currency: 'JPY', confirmed_at: '2026-09-01T12:00:00Z', legacy_fractional_limit: true } as Record<string, unknown> };
  const banking = () => ({ accounts: [card], local_accounts: [{ id: card.account_id, account_type: 'credit', display_name: card.name }], sync: {}, configured: true });
  let submitted: unknown;
  await page.route('**/api/banking', route => route.fulfill({ json: banking() }));
  await page.route(`**/api/banking/accounts/${card.id}/balance-rule`, route => {
    submitted = route.request().postDataJSON();
    card.balance_rule = { ...route.request().postDataJSON(), confirmed_at: '2026-10-04T12:00:00Z' };
    return route.fulfill({ json: banking() });
  });
  await page.goto('/#settings');
  await page.locator('#settings-cards > summary').click();
  await page.getByRole('combobox', { name: 'Connected credit card', exact: true }).selectOption(card.id);
  const limit = page.getByRole('spinbutton', { name: 'Confirmed credit limit · JPY', exact: true });
  await expect(limit).toHaveValue('1000.12');
  const hint = page.getByRole('status').filter({ hasText: 'Your saved JPY limit includes fractional units.' });
  await expect(hint).toBeVisible();
  expect(await limit.evaluate(element => (element as HTMLInputElement).checkValidity())).toBe(false);
  await limit.fill('1000');
  await page.getByRole('button', { name: 'Save balance setting', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Balance setting saved.' })).toBeVisible();
  expect(submitted).toEqual({ balance_type: 'available_credit', credit_limit: 1000, currency: 'JPY' });
  await expect(hint).toHaveCount(0);
  await expect(limit).toHaveValue('1000');
});
