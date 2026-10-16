import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';

const { csvColumns, productRequest } = await sourceModule('productApi');
const { captureAuthorizationCallback } = await sourceModule('connectionAuthorization');

test('CSV header suggestions preserve quoted separators, escaped quotes and BOM', () => {
  assert.deepEqual(csvColumns('\uFEFFDate,"Description, reference","Quoted ""column""",Amount\r\n2026-01-01,"Shop, branch",x,-3', ','), ['Date', 'Description, reference', 'Quoted "column"', 'Amount']);
  assert.deepEqual(csvColumns('Date;Description;Amount\n01/01/2026;Shop;12,30', ';'), ['Date', 'Description', 'Amount']);
  assert.deepEqual(csvColumns('Date\tDescription\tAmount\r\n', '\t'), ['Date', 'Description', 'Amount']);
});

test('product requests preserve same-origin auth protections and show validation errors', async t => {
  const previous = globalThis.fetch; t.after(() => { globalThis.fetch = previous; });
  let sent;
  globalThis.fetch = async (path, options) => { sent = { path, options }; return new Response(JSON.stringify({ detail: [{ msg: 'Currency must use three uppercase letters' }] }), { status: 422 }); };
  await assert.rejects(productRequest('workspace/accounts', 'POST', { currency: 'gbp' }), /Currency must use three uppercase letters/);
  assert.equal(sent.path, '/api/workspace/accounts');
  assert.equal(sent.options.headers.get('X-Ledger-Request'), '1');
  assert.equal(sent.options.credentials, 'same-origin');
  assert.equal(sent.options.cache, 'no-store');
});

test('provider callbacks remove URL secrets immediately and never reflect provider errors', () => {
  const paths = [];
  assert.deepEqual(captureAuthorizationCallback('https://finance.example/connect/callback/lunchflow?state=private-state&code=private-code', path => paths.push(path)), { provider: 'lunchflow', state: 'private-state', code: 'private-code' });
  assert.deepEqual(paths, ['/']);
  const rejected = captureAuthorizationCallback('https://finance.example/connect/callback/snaptrade?state=state&error=private-error-body', path => paths.push(path));
  assert.equal(rejected.error.includes('private-error-body'), false);
  assert.match(captureAuthorizationCallback('https://finance.example/connect/callback/snaptrade?state=state&status=ABANDONED', () => {}).error, /did not authorize/);
  assert.match(captureAuthorizationCallback('https://finance.example/connect/callback/snaptrade?state=state&status=ERROR', () => {}).error, /did not authorize/);
  assert.equal(captureAuthorizationCallback('https://finance.example/#settings', () => assert.fail('ordinary pages must not be rewritten')), null);
});
