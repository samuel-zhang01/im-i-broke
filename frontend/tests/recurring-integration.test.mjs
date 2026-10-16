import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';
const { currentRecurringTotals, recurringForPayment, recurringMonthlyAmount, recurringWaitingForPayment, upcomingRecurring, openRecurring, watchRecurringNavigation } = await sourceModule('recurring');
const { api } = await sourceModule('api');

test('monthly estimates exclude stale and ended patterns, and history uses exact payment membership', () => {
  const data = {patterns:[
    {key:'a',status:'active',monthly_cost:10,txn_ids:[1,2]},
    {key:'b',status:'price_change',monthly_cost:20,txn_ids:[3,4]},
    {key:'stale',status:'price_change',data_stale:true,monthly_cost:100,txn_ids:[5]},
    {key:'ended',status:'ended',monthly_cost:0,txn_ids:[6]},
  ],candidates:[{key:'pending',txn_ids:[7]}]};
  assert.deepEqual(currentRecurringTotals(data.patterns), {monthly:30,current:2,priceChanges:1});
  assert.equal(recurringForPayment(data,4).key,'b');
  assert.equal(recurringForPayment(data,7).key,'pending');
  assert.equal(recurringForPayment(data,99),undefined);
});

test('a payment remains upcoming while its recorded timing window overlaps today', () => {
  const series = (key, extra = {}) => ({key, merchant:key, status:'active', forecast_eligible:true, next_expected:'2026-09-17', next_expected_earliest:'2026-09-15', next_expected_latest:'2026-09-20', ...extra});
  const waiting = series('waiting');
  const lastDay = series('last-day', {next_expected_latest:'2026-09-18'});
  const firstDay = series('edge', {next_expected:'2026-10-20', next_expected_earliest:'2026-10-18', next_expected_latest:'2026-10-23'});
  const rows = [firstDay, waiting, lastDay,
    series('expired', {next_expected_latest:'2026-09-17'}),
    series('distant', {next_expected:'2026-10-20', next_expected_earliest:'2026-10-19', next_expected_latest:'2026-10-23'}),
    series('stale', {data_stale:true}), series('ended', {status:'ended'}),
    series('variable', {pattern_type:'variable_cost'}), series('unsupported', {forecast_eligible:false}),
    series('unknown', {next_expected:null}), series('invalid-bounds', {next_expected_earliest:'2026-09-21'}),
    series('invalid-date', {next_expected:'2026-02-30', next_expected_earliest:'2026-02-28', next_expected_latest:'2026-10-01'}),
  ];
  assert.deepEqual(upcomingRecurring(rows,'2026-09-18').map(row=>row.key), ['last-day','waiting','edge']);
  assert.equal(waiting.next_expected, '2026-09-17');
  assert.equal(recurringWaitingForPayment(waiting,'2026-09-18'), true);
  assert.equal(recurringWaitingForPayment(waiting,'2026-09-21'), false);
  assert.equal(recurringWaitingForPayment(waiting,'2026-09-17'), false);
  assert.deepEqual(upcomingRecurring(rows,'invalid'), []);
});

test('observed monthly rates remain visible for ended and nonforecast patterns without inflating commitments', () => {
  const active = {status:'active',monthly_cost:18,historical_monthly_cost:15};
  const ended = {status:'ended',monthly_cost:0,historical_monthly_cost:15};
  const variable = {status:'active',pattern_type:'variable_cost',monthly_cost:0,historical_monthly_cost:9};
  const legacy = {status:'ended',monthly_cost:11};
  assert.deepEqual([active, ended, variable, legacy].map(recurringMonthlyAmount), [18,15,9,11]);
  assert.deepEqual(currentRecurringTotals([active,ended,variable,legacy]), {monthly:18,current:1,priceChanges:0});
});

test('recurring links carry only a valid transaction ID and unsubscribe cleanly', t => {
  const previous = globalThis.window;
  globalThis.window = new EventTarget();
  t.after(() => { globalThis.window = previous; });
  const received=[];
  const stop=watchRecurringNavigation(id=>received.push(id));
  openRecurring(42);openRecurring(-1);openRecurring('42');openRecurring(NaN);
  assert.deepEqual(received,[42]);
  stop();openRecurring(43);
  assert.deepEqual(received,[42]);
});

test('recurring overview requests matching evidence with the active preview settings', async t => {
  const original=globalThis.fetch;
  t.after(()=>{globalThis.fetch=original;});
  let url;
  globalThis.fetch=async path=>{url=new URL(path,'http://localhost');return new Response(JSON.stringify({patterns:[],candidates:[]}));};
  await api.recurringOverview({min_occurrences:2,tol_annual:30});
  assert.equal(url.pathname,'/api/recurring');
  assert.equal(url.searchParams.get('details'),'true');
  assert.equal(url.searchParams.get('min_occurrences'),'2');
  assert.equal(url.searchParams.get('tol_annual'),'30');
});
