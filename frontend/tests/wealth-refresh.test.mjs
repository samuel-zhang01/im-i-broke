import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';
const { refreshWealthSources } = await sourceModule('wealthRefresh');
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

test('providers start together, each result is reported immediately, then FX discovers their new currencies', async () => {
  const banks = deferred(), investments = deferred();
  const events = [];
  const operation = refreshWealthSources(['banks', 'investments', 'rates'], new AbortController().signal,
    source => events.push(`start:${source}`), (source, result) => events.push(`${source}:${result.status}`), {
      banks: () => banks.promise, investments: () => investments.promise,
      rates: async () => { events.push('rates-request'); return { status: 'updated' }; },
    });
  assert.deepEqual(events, ['start:banks', 'start:investments']);
  banks.resolve({ status: 'failed', message: 'Saved bank values retained' });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ['start:banks', 'start:investments', 'banks:failed']);
  investments.resolve({ status: 'updated' });
  await operation;
  assert.deepEqual(events.slice(3), ['investments:updated', 'start:rates', 'rates-request', 'rates:updated']);
});

test('retry requests only the failed source, deduplicates requests and reports thrown errors', async () => {
  const calls = [], results = [];
  const tasks = Object.fromEntries(['banks', 'investments', 'rates'].map(source => [source, async () => { calls.push(source); throw new Error('Provider unavailable'); }]));
  await refreshWealthSources(['banks', 'banks'], new AbortController().signal, () => {}, (source, result) => results.push([source, result]), tasks);
  assert.deepEqual(calls, ['banks']);
  assert.deepEqual(results, [['banks', { status: 'failed', message: 'Provider unavailable' }]]);
});

test('leaving the page aborts result callbacks and does not start dependent rates', async () => {
  const bank = deferred(), controller = new AbortController(), calls = [];
  const operation = refreshWealthSources(['banks', 'rates'], controller.signal, source => calls.push(source), () => calls.push('result'), {
    banks: () => bank.promise, investments: async () => ({ status: 'updated' }), rates: async () => { calls.push('rates-request'); return { status: 'updated' }; },
  });
  controller.abort(); bank.resolve({ status: 'updated' }); await operation;
  assert.deepEqual(calls, ['banks']);
});

test('partial FX and position failures are not reported as successful updates', async () => {
  const originalFetch = globalThis.fetch;
  let section = { status: 'error' };
  globalThis.fetch = async path => new Response(JSON.stringify(path.includes('wealth') ? { updated: 2, attempted: 3, missing: 1 }
    : { configured: true, connections: { status: 'ok', items: [] }, accounts: [{ balances: { status: 'ok' }, positions: section }], cache: {} }), { status: 200 });
  try {
    for (const partial of [{ status: 'error' }, { status: 'ok', stale: true }, { status: 'ok', truncated: true }]) {
      section = partial;
      const results = [];
      await refreshWealthSources(['investments', 'rates'], new AbortController().signal, () => {}, (source, result) => results.push([source, result.status]));
      assert.deepEqual(results, [['investments', 'failed'], ['rates', 'failed']]);
    }
  } finally { globalThis.fetch = originalFetch; }
});

test('an incomplete FX response cannot turn an update green', async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const data of [null, {}, { updated: 1, attempted: 2, missing: 0 }, { updated: -1, attempted: 0, missing: 1 }]) {
      globalThis.fetch = async () => new Response(JSON.stringify(data), { status: 200 });
      const results = [];
      await refreshWealthSources(['rates'], new AbortController().signal, () => {}, (_, result) => results.push(result.status));
      assert.deepEqual(results, ['failed']);
    }
  } finally { globalThis.fetch = originalFetch; }
});
