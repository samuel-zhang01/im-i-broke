import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceModule } from './source-module.mjs';
const { financePageFromHash, validFinancePage, FINANCE_PAGES } = await sourceModule('navigation');
test('net assets is first and every page has a validated bookmarkable destination', () => {
 assert.equal(FINANCE_PAGES[0], 'net-worth');
 for(const page of FINANCE_PAGES) assert.equal(financePageFromHash(`#${page}`),page);
 assert.equal(financePageFromHash('#/banking'),'banking');
 for(const unknown of ['#undefined','#javascript:alert(1)','#not-a-page','']) assert.equal(financePageFromHash(unknown),null);
 assert.equal(validFinancePage(null),false);
});

test('banking workspaces preserve every leaf destination and leave investment destinations independent', async () => {
 const { bankingWorkspace, primaryFinancePage } = await sourceModule('navigation');
 for(const id of ['transactions','anomalies','transfers','merchants']) {
  assert.equal(bankingWorkspace(id).label,'Activity');assert.equal(primaryFinancePage(id),'transactions');
  assert.equal(financePageFromHash(`#${id}`),id);
 }
 for(const id of ['recurring','planning','advice']) assert.equal(primaryFinancePage(id),'recurring');
 for(const id of ['net-worth','connected-portfolio','investments']) {assert.equal(bankingWorkspace(id),undefined);assert.equal(primaryFinancePage(id),id);}
});


test('retired import bookmarks resolve to connected banks without a navigation entry', () => {
 assert.equal(financePageFromHash('#import'), 'banking');
 assert.equal(financePageFromHash('#/import'), 'banking');
 assert.equal(validFinancePage('import'), false);
 assert.equal(FINANCE_PAGES.includes('import'), false);
});
