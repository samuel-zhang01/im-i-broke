import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceModule } from './source-module.mjs';
const store = await sourceModule('workspaceStore');

test('workspace controls stay isolated per account and retain their reference between reads', () => {
  store.clearWorkspaceState();
  const first = { search: 'rent', offset: 40, from: '2026-01-01' };
  const second = { search: '', offset: 0, from: '' };
  store.writeWorkspaceValue('bank:a', first);
  store.writeWorkspaceValue('bank:b', second);
  assert.equal(store.readWorkspaceValue('bank:a'), first);
  assert.equal(store.readWorkspaceValue('bank:b'), second);
  assert.equal(store.hasWorkspaceValue('bank:c'), false);
});

test('session invalidation clears controls and refuses a late previous-session write', () => {
  store.clearWorkspaceState();
  const generation = store.workspaceGeneration();
  store.writeWorkspaceValue('recurring:search', 'utility');
  let notifications = 0;
  const unwatch = store.watchWorkspaceState(() => notifications++);
  store.clearWorkspaceState();
  assert.equal(store.readWorkspaceValue('recurring:search'), undefined);
  store.writeWorkspaceValue('recurring:search', 'late callback', generation);
  assert.equal(store.hasWorkspaceValue('recurring:search'), false);
  assert.equal(notifications, 1);
  unwatch();
});

test('workspace memory has a fixed bound and retains recently changed controls', () => {
  store.clearWorkspaceState();
  for (let index = 0; index < store.WORKSPACE_LIMIT; index++) store.writeWorkspaceValue(`view:${index}`, index);
  store.writeWorkspaceValue('view:0', 'recent');
  store.writeWorkspaceValue('new-account', 'all');
  assert.equal(store.readWorkspaceValue('view:0'), 'recent');
  assert.equal(store.hasWorkspaceValue('view:1'), false);
  assert.equal(store.readWorkspaceValue('new-account'), 'all');
});
