import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';
const { readCached, clearReadCache } = await sourceModule('readCache');
const { upcomingRecurring } = await sourceModule('recurring');

test('identical bank reads share work and successful data until invalidation', async () => {
  clearReadCache(); let calls = 0;
  const load = async () => { calls++; return { version: calls }; };
  const [one, two] = await Promise.all([readCached('/api/summary?b=2&a=1', load), readCached('/api/summary?a=1&b=2', load)]);
  assert.equal(calls, 1); assert.deepEqual(one, two);
  await readCached('/api/summary?a=1&b=2', load); assert.equal(calls, 1);
  clearReadCache(); await readCached('/api/summary?a=1&b=2', load); assert.equal(calls, 2);
});
test('a late old response cannot replace data fetched after a confirmed edit', async () => {
  clearReadCache(); let release;
  const old = readCached('/api/transactions', () => new Promise(resolve => { release = resolve; }));
  await Promise.resolve(); clearReadCache();
  const current = await readCached('/api/transactions', async () => 'after edit');
  release('before edit'); await old;
  assert.equal(current, 'after edit');
  assert.equal(await readCached('/api/transactions', async () => 'unexpected reload'), 'after edit');
});
test('failed reads are retryable and cancellation does not cancel another reader', async () => {
  clearReadCache();
  await assert.rejects(readCached('/api/summary', async () => { throw Error('offline'); }), /offline/);
  assert.equal(await readCached('/api/summary', async () => 'recovered'), 'recovered');
  let release, upstreamSignal;
  const first = new AbortController(), second = new AbortController();
  const load = signal => { upstreamSignal = signal; return new Promise(resolve => { release = resolve; }); };
  const cancelled = readCached('/api/recurring', load, first.signal);
  const retained = readCached('/api/recurring', load, second.signal);
  await Promise.resolve(); first.abort();
  await assert.rejects(cancelled, error => error.name === 'AbortError');
  assert.equal(upstreamSignal.aborted, false);
  release('shared result'); assert.equal(await retained, 'shared result');
});
test('abandoned searches cancel their upstream request', async () => {
  clearReadCache(); const controller = new AbortController(); let upstream;
  const promise = readCached('/api/transactions?q=old', signal => { upstream = signal; return new Promise(() => {}); }, controller.signal);
  await Promise.resolve(); controller.abort();
  await assert.rejects(promise, error => error.name === 'AbortError');
  await new Promise(resolve => setTimeout(resolve, 60)); assert.equal(upstream.aborted, true);
});
test('upcoming recurring dates exclude stale, ended, past, unknown and distant evidence', () => {
  const series = (key, date, extra = {}) => ({ key, merchant: key, next_expected: date, status: 'active', data_stale: false, ...extra });
  const result = upcomingRecurring([series('today','2026-09-18'),series('next','2026-10-18'),series('old','2026-09-17'),series('later','2026-10-19'),series('stale','2026-09-22',{data_stale:true}),series('ended','2026-09-22',{status:'ended'}),series('unknown',null)], '2026-09-18');
  assert.deepEqual(result.map(row => row.key), ['today','next']);
});

test('habits, variable charges and unsupported regular schedules cannot inflate expected payments', async () => {
 const { currentRecurringTotals } = await sourceModule('recurring');
 const row = (merchant,extra={}) => ({merchant,next_expected:'2026-09-22',status:'active',data_stale:false,monthly_cost:12,pattern_type:'regular_payment',forecast_eligible:true,...extra});
 const rows=[row('Supported'),row('Shopping',{pattern_type:'spending_habit',monthly_cost:900}),row('FX fees',{pattern_type:'variable_cost',monthly_cost:400}),row('Irregular',{forecast_eligible:false,monthly_cost:100}),row('Stale',{data_stale:true}),row('Ended',{status:'ended'})];
 assert.deepEqual(upcomingRecurring(rows,'2026-09-18').map(item=>item.merchant),['Supported']);
 assert.deepEqual(currentRecurringTotals(rows),{monthly:12,current:1,priceChanges:0});
});
