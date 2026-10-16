import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { sourceModule } from './source-module.mjs';
const { institutionCatalog, institutionLogo, institutionName, institutionInitials } = await sourceModule('institutions');
const { bankRows } = await sourceModule('banking');
const { connectionRefreshIssue, portfolioRefreshIssue } = await sourceModule('providerRefresh');

test('every published brokerage and alias resolves to a bundled PNG', async () => {
  assert.ok(institutionCatalog.institutions.filter(row => row.providers.includes('snaptrade')).length >= 40);
  for (const institution of institutionCatalog.institutions) {
    for (const alias of institution.aliases) assert.equal(institutionLogo(alias), `/logos/${institution.slug}.png`, alias);
    const png = await readFile(new URL(`../public/logos/${institution.slug}.png`, import.meta.url));
    assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', institution.name);
  }
});

test('regional provider names resolve without branding unrelated names', () => {
  for (const [name, expected] of [['DBS (SG) - Personal', 'dbs'], ['HSBC Personal', 'hsbc'],
    ['Charles Schwab', 'schwab'], ['E*TRADE', 'etrade'], ['Webull Canada', 'webull-ca'],
    ['Trading212', 'trading212'], ['Futu', 'moomoo']]) assert.equal(institutionLogo(name), `/logos/${expected}.png`);
  for (const name of ['Republic Bank', 'Public Bank', 'Schwabish Savings', '新银行', 'Future Connector Bank']) {
    assert.equal(institutionLogo(name), undefined);
    assert.equal(institutionName(name), name);
  }
  assert.equal(institutionInitials('新银行'), '新');
});

test('new and same-named banks retain distinct accounts and currencies', () => {
  const accounts = ['USD', 'EUR', 'JPY'].map((currency, i) => ({ id: `new-${i}`, name: 'Everyday', institution_name: '新银行', currency }));
  const rows = bankRows({ accounts, local_accounts: [] });
  assert.equal(rows.length, 3);
  assert.equal(new Set(rows.map(row => row.key)).size, 3);
  assert.deepEqual(new Set(rows.map(row => row.currency)), new Set(['USD', 'EUR', 'JPY']));
  assert.ok(rows.every(row => row.institution === '新银行'));
});

test('a failed first connection is visible even when another connection has a good cache', () => {
  const status = { id: 'new', label: 'New broker', enabled: true, status: 'error', last_error: 'Access unavailable', last_synced_at: null };
  assert.match(connectionRefreshIssue([status]), /New broker/);
  assert.equal(connectionRefreshIssue([{ ...status, enabled: false }]), null);
  assert.match(portfolioRefreshIssue({ configured: true, accounts: [], connection_statuses: [status] }), /New broker/);
  assert.equal(portfolioRefreshIssue({ configured: true, accounts: [{ positions: { status: 'unsupported' }, balances: { status: 'ok' } }] }), null);
  assert.match(portfolioRefreshIssue({ configured: true, accounts: [{ positions: { status: 'error' }, balances: { status: 'ok' } }] }), /could not update/);
});
