import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';

const { marketValueTimestamp, marketValueDate, marketAxisLabel, marketPointValue, marketValueSegments } = await sourceModule('marketValue');
const { fetchMarketValue } = await sourceModule('marketValueApi');
const point = (hour, value, coverage_key = 'one') => ({ timestamp: `2026-09-18T${String(hour).padStart(2, '0')}:00:00Z`, value, coverage_key });

test('hourly points retain zero and never join gaps or changed account coverage', () => {
  const points = [point(10, 100), point(11, 0), point(12, null), point(13, 110), point(14, 120, 'two'), point(15, 125, 'two')];
  assert.deepEqual(marketValueSegments(points), [[100, 0, null, null, null, null], [null, null, null, 110, null, null], [null, null, null, null, 120, 125]]);
  assert.equal(marketPointValue(point(10, 0)), 0);
  assert.equal(marketPointValue(point(10, -20)), -20);
  for (const value of [null, undefined, NaN, Infinity, '15']) assert.equal(marketPointValue(point(10, value)), null);
  assert.equal(marketPointValue({ timestamp: 'unparseable', value: 100 }), null);
});

test('all chart times are UTC rather than browser locale time or a guessed naive timestamp', () => {
  const old = process.env.TZ;
  try {
    for (const zone of ['Europe/London', 'America/Los_Angeles', 'Pacific/Auckland']) {
      process.env.TZ = zone;
      assert.match(marketValueDate('2026-09-18T00:00:00Z'), /18 Sept 2026, 00:00 UTC/);
      assert.equal(marketAxisLabel(Date.parse('2026-09-18T00:00:00Z'), '1d'), '00:00');
      assert.equal(marketAxisLabel(Date.parse('2026-09-18T00:00:00Z'), '1w'), '18 Sept');
    }
  } finally { if (old === undefined) delete process.env.TZ; else process.env.TZ = old; }
  assert.equal(marketValueTimestamp('2026-09-18T09:00:00+01:00'), Date.parse('2026-09-18T08:00:00Z'));
  assert.equal(marketValueTimestamp('2026-09-18T09:00:00'), null);
  assert.equal(marketValueDate(null), 'Unknown time');
});

test('recent-price reads use exact filters, credentials and GET, including explicit empty selection', async () => {
  const original = globalThis.fetch, calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ basis: 'current_holdings', summary: {}, points: [], warnings: [], methodology: [] })); };
    await fetchMarketValue(['raw/account&1', 'paper?2'], 'USD', '1w', '1h');
    const params = new URL(calls[0].url, 'http://local').searchParams;
    assert.deepEqual(params.getAll('account'), ['raw/account&1', 'paper?2']);
    assert.equal(params.get('currency'), 'USD'); assert.equal(params.get('range'), '1w'); assert.equal(params.get('interval'), '1h');
    assert.equal(calls[0].options.method, undefined); assert.equal(calls[0].options.cache, 'no-store');
    assert.equal(calls[0].options.credentials, 'same-origin');
    await fetchMarketValue([], 'GBP', '1m', '1d');
    assert.deepEqual(new URL(calls[1].url, 'http://local').searchParams.getAll('account'), ['__none__']);
    globalThis.fetch = async () => new Response(JSON.stringify({ basis: 'account_profit', summary: {}, points: [], warnings: [], methodology: [] }));
    await assert.rejects(fetchMarketValue(['a'], 'GBP', '1d', '1h'), /incomplete/);
    globalThis.fetch = async () => new Response('PRIVATE BROKER PAYLOAD', { status: 503 });
    await assert.rejects(fetchMarketValue(['a'], 'GBP', '1d', '1h'), error => error.message.includes('503') && !error.message.includes('PRIVATE'));
  } finally { globalThis.fetch = original; }
});
