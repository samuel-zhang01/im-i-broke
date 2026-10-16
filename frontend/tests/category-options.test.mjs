import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';

const { CATEGORIES, categoryOptions } = await sourceModule('categories');
const { api } = await sourceModule('api');
const { validPageOffset } = await sourceModule('pagination');

test('server order and archived choices are respected while current categories remain selectable', () => {
  const options = categoryOptions(
    ['Legal services', 'Scam / fraud', 'Shopping', 'Software & hardware', 'Legal services'],
    [null, undefined, '', '  ', 'Family transfers'],
  );
  assert.deepEqual(options, ['Legal services', 'Scam / fraud', 'Shopping', 'Software & hardware', 'Family transfers']);
  assert.equal(options.filter(value => value === 'Shopping').length, 1);
  assert.equal(options.includes('Housing'), false);
  assert.ok(categoryOptions([], ['Excluded-only category']).includes('Excluded-only category'));
  assert.deepEqual(categoryOptions([]), []);
  assert.deepEqual(categoryOptions(null), CATEGORIES);
});

test('category choices use the full category catalog, including excluded and transfer-only labels', async t => {
  const previous = globalThis.fetch;
  t.after(() => { globalThis.fetch = previous; });
  let requested;
  globalThis.fetch = async path => {
    requested = path;
    return new Response(JSON.stringify(['Excluded-only category', 'Family transfers']));
  };
  assert.deepEqual(await api.categoryOptions(), ['Excluded-only category', 'Family transfers']);
  assert.equal(requested, '/api/workspace/category-options');
});

test('a shrinking filtered result returns to the last valid page after review or reclassification', () => {
  assert.equal(validPageOffset(50, 50, 50), 0);
  assert.equal(validPageOffset(100, 50, 100), 50);
  assert.equal(validPageOffset(0, 50, 150), 0);
  assert.equal(validPageOffset(51, 50, 50), 50);
  assert.equal(validPageOffset(125, 50, 0), 0);
});
