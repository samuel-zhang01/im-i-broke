import test from "node:test";
import assert from "node:assert/strict";
import { sourceModule } from "./source-module.mjs";
const { spendBreakdown } = await sourceModule("spendingExplorer");
const cells = [
  {
    month: "2026-01",
    category: "Shopping",
    merchant: "Shop",
    gross: 100,
    refunds: 0,
    net: 100,
    n: 1,
  },
  {
    month: "2026-02",
    category: "Shopping",
    merchant: "Shop",
    gross: 0,
    refunds: 25,
    net: -25,
    n: 1,
  },
  {
    month: "2026-02",
    category: "Food",
    merchant: "Cafe",
    gross: 12,
    refunds: 0,
    net: 12,
    n: 1,
  },
];
test("category multiselect reconciles refunds and explicit empty selections", () => {
  const all = spendBreakdown(cells, null);
  assert.equal(all.net, 87);
  assert.equal(all.refunds, 25);
  const shopping = spendBreakdown(cells, ["Shopping"]);
  assert.equal(shopping.net, 75);
  assert.equal(shopping.vendors.length, 1);
  assert.equal(shopping.vendors[0].n, 2);
  assert.equal(spendBreakdown(cells, []).net, 0);
  assert.equal(spendBreakdown(cells, ["Shopping", "Food"]).net, 87);
});

test('explorer preferences reject invalid saved values and preserve empty category selection', async t => {
  const {readExplorerPreferences,saveExplorerPreferences}=await sourceModule('spendingExplorer');
  const previous=globalThis.sessionStorage; const store=new Map();
  globalThis.sessionStorage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)};
  t.after(()=>{globalThis.sessionStorage=previous;});
  const prefs={period:'custom',account:'a',from:'2026-01-01',to:'2026-02-28',selected:[],vendorSearch:'Cafe'};
  saveExplorerPreferences('overview',prefs);
  assert.deepEqual(readExplorerPreferences('overview'),prefs);
  store.set('ledger.explorer.overview',JSON.stringify({period:'wrong',selected:1,from:'wrong'}));
  assert.equal(readExplorerPreferences('overview').period,'all');
  assert.equal(readExplorerPreferences('overview').selected,null);
  store.set('ledger.explorer.overview','broken');
  assert.equal(readExplorerPreferences('overview').period,'all');
});
