import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceModule } from './source-module.mjs';
const { createPerformanceStore, performanceSupport } = await sourceModule('performanceDiagnostics');

test('performance diagnostics distinguish unsupported and unobserved metrics', () => {
  assert.deepEqual(performanceSupport(['event', 'largest-contentful-paint'], true), { LCP: true, INP: true, CLS: false });
  assert.equal(performanceSupport(['event'], false).INP, false);
  assert.deepEqual(createPerformanceStore({ LCP: true, INP: false, CLS: false }).getSnapshot(), [
    { name: 'LCP', supported: true, value: null, rating: null },
    { name: 'INP', supported: false, value: null, rating: null },
    { name: 'CLS', supported: false, value: null, rating: null },
  ]);
});

test('performance memory is bounded and strips attribution, URLs, elements and IDs', () => {
  const store = createPerformanceStore({ LCP: true, INP: true, CLS: true });
  let updates = 0;
  const unsubscribe = store.subscribe(() => updates++);
  for (let i = 0; i < 1000; i++) store.record({ name: 'LCP', value: i, rating: 'good',
    id: 'secret', entries: [{ element: { textContent: 'private account' }, url: '/private' }], navigationURL: '/secret' });
  assert.equal(updates, 1000);
  assert.equal(store.getSnapshot().length, 3);
  assert.deepEqual(store.getSnapshot()[0], { name: 'LCP', value: 999, rating: 'good', supported: true });
  assert.equal(JSON.stringify(store.getSnapshot()).includes('secret'), false);
  assert.equal(Object.isFrozen(store.getSnapshot()[0]), true);
  unsubscribe();
  store.record({ name: 'LCP', value: 1000, rating: 'good' });
  assert.equal(updates, 1000);
});

test('invalid, duplicate and unsupported performance reports do not fabricate readings', () => {
  const store = createPerformanceStore({ LCP: true, INP: false, CLS: true });
  const original = store.getSnapshot();
  for (const report of [
    { name: 'LCP', value: NaN, rating: 'good' }, { name: 'LCP', value: Infinity, rating: 'good' },
    { name: 'LCP', value: -1, rating: 'good' }, { name: 'LCP', value: 1, rating: 'invented' },
    { name: 'INP', value: 1, rating: 'good' }, { name: 'unknown', value: 1, rating: 'good' },
  ]) store.record(report);
  assert.equal(store.getSnapshot(), original);
  store.record({ name: 'CLS', value: 0, rating: 'good' });
  const measured = store.getSnapshot();
  store.record({ name: 'CLS', value: 0, rating: 'good' });
  assert.equal(store.getSnapshot(), measured);
  assert.equal(measured[2].value, 0);
});
