import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';
const { categoryColor } = await sourceModule('bankingCharts');
test('banking category colours retain identity when rankings and filters change', () => {
  const categories = ['Groceries', 'Eating out', 'Subscriptions', 'Uncategorized'];
  const before = Object.fromEntries(categories.map(name => [name, categoryColor(name)]));
  for (const name of categories.reverse().slice(1)) assert.equal(categoryColor(name), before[name]);
  assert.equal(categoryColor('  GROCERIES '), categoryColor('Groceries'));
  assert.notEqual(categoryColor('Uncategorized'), categoryColor('Eating out'));
  assert.match(categoryColor('A custom category'), /^#[A-Fa-f0-9]{6}$/);
});
