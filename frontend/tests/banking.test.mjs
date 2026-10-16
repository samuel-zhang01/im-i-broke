import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { bankRows, bankTotals, bankBalanceTotals, nativeMoney, accountBalance, isOldBankData, bankName } = await sourceModule("banking");
const { fetchBanking, fetchBankAccount, syncBanking, mapBankAccount, resolveBankReview, setBankBalanceRule } = await sourceModule("bankingApi");
const provider = (id, fields = {}) => ({ id, account_id: null, institution_name: "Revolut", name: `Account ${id}`, currency: "GBP", status: "active", present: true, excluded: false, mapping_status: "new", mapping_candidates: [], balance: null, counts: { cached: 0, imported: 0, matched: 0, pending: 0, review: 0 }, ...fields });
const local = (id, fields = {}) => ({ id, bank: "revolut", display_name: `Statement ${id}`, account_type: "current", connected: false, balance: 10, currency: "GBP", ...fields });
const fixture = (fields = {}) => ({ configured: true, accounts: [], local_accounts: [], sync: {}, stats: {}, ...fields });

test("account index keeps linked history once and preserves Amex statements", () => {
  const data = fixture({ accounts: [provider("r1", { account_id: "old-r", balance: { amount: 0, currency: "GBP" } }), provider("amex", { account_id: "old-a", excluded: true })], local_accounts: [local("old-r"), local("old-a", { bank: "Amex" }), local("broker", { account_type: "investment" })] });
  const rows = bankRows(data);
  assert.equal(rows.length, 2);
  assert.equal(rows.filter(row => row.local?.id === "old-r").length, 1);
  assert.equal(rows.find(row => row.local?.id === "old-r").provider.id, "r1");
  assert.equal(rows.find(row => row.institution === "American Express").provider, undefined);
  assert.equal(accountBalance(rows.find(row => row.provider)), 0);
});

test("native totals never combine currencies, stale statements, or absent accounts", () => {
  const rows = bankRows(fixture({ accounts: [provider("gbp", { balance: { amount: 100, currency: "GBP" } }), provider("sgd", { balance: { amount: 200, currency: "SGD" } }), provider("missing", { present: false, balance: { amount: 900, currency: "GBP" } }), provider("unknown")], local_accounts: [local("manual", { balance: 800 })] }));
  assert.deepEqual(bankTotals(rows), { GBP: 100, SGD: 200 });
  assert.equal(accountBalance(rows.find(row => row.provider?.id === "unknown")), null);
  assert.equal(nativeMoney(null), "—");
  assert.equal(nativeMoney(NaN), "—");
  assert.match(nativeMoney(0, "GBP"), /0\.00/);
  assert.match(nativeMoney(100, "SGD"), /100\.00/);
});

test("freshness treats unknown dates as stale and timezone-free server timestamps as UTC", () => {
  const now = Date.parse("2026-09-18T12:00:00Z");
  assert.equal(isOldBankData(null, now), true);
  assert.equal(isOldBankData("not a timestamp", now), true);
  assert.equal(isOldBankData("2026-09-18 11:00:00", now), false);
  assert.equal(isOldBankData("2026-09-16T23:00:00+00:00", now), true);
  assert.equal(bankName("HSBC UK"), bankName("hsbc"));
});

test("connected balance breakdown separates gross debt from net cash without adding statement balances", () => {
  const rows = bankRows(fixture({ accounts: [
    provider("cash", { balance: { amount: 2447.60, currency: "GBP" } }),
    provider("card", { balance: { amount: -7145.65, currency: "GBP" } }),
    provider("zero", { balance: { amount: 0, currency: "USD" } }),
    provider("absent", { present: false, balance: { amount: -500, currency: "GBP" } }),
    provider("unknown"),
  ], local_accounts: [local("statement", { account_type: "credit", balance: -1167.60 })] }));
  const totals = bankBalanceTotals(rows);
  assert.equal(totals.GBP.assets, 2447.60);
  assert.equal(totals.GBP.liabilities, 7145.65);
  assert.equal(Number(totals.GBP.net.toFixed(2)), -4698.05);
  assert.deepEqual(totals.USD, { assets: 0, liabilities: 0, net: 0 });
});

test("bank API encodes account paths and filters and authenticates mutations without provider credentials", async () => {
  const original = globalThis.fetch, calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify(fixture()), { status: 200 }); };
    await fetchBanking();
    await fetchBankAccount("id/a?b", { q: "coffee & lunch", status: "pending", date_from: "2026-09-01", limit: 30 });
    const call = calls.at(-1);
    assert.match(call.url, /accounts\/id%2Fa%3Fb\?/);
    assert.equal(new URL(call.url, "http://localhost").searchParams.get("q"), "coffee & lunch");
    assert.equal(call.options.cache, "no-store");
    await syncBanking();
    await mapBankAccount("new/one", "saved-account");
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { account_id: "saved-account", create_new: false });
    await resolveBankReview(7, "match", 12);
    assert.deepEqual(JSON.parse(calls.at(-1).options.body), { action: "match", transaction_id: 12 });
    assert.equal(calls.at(-1).options.method, "POST");
    assert.equal(calls.at(-1).options.headers.get("X-Ledger-Request"), "1");
    assert.equal(calls.at(-1).options.headers.has("Authorization"), false);
  } finally { globalThis.fetch = original; }
});

test("connected totals use effective estimates and do not coerce unknown debt or original provider values to cash", () => {
  const rows = bankRows(fixture({ accounts: [
    provider("cash", { balance: { amount: 2447.60, currency: "GBP" } }),
    provider("card", { balance: { amount: -1459.56, reported_amount: -7145.65, currency: "GBP" } }),
    provider("unknown", { balance: { amount: null, reported_amount: -200, currency: "GBP" } }),
  ], local_accounts: [local("amex", { account_type: "credit", balance: null, reported_balance: -1167.60 })] }));
  assert.equal(Number(bankTotals(rows).GBP.toFixed(2)), 988.04);
  const totals = bankBalanceTotals(rows).GBP;
  assert.equal(totals.assets, 2447.60);
  assert.equal(totals.liabilities, 1459.56);
  assert.equal(Number(totals.net.toFixed(2)), 988.04);
  assert.equal(accountBalance(rows.find(row => row.provider?.id === "unknown")), null);
  assert.equal(accountBalance(rows.find(row => row.local?.id === "amex")), null);
});

test("bank failures do not expose upstream response bodies or clear cache through a delete", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("UPSTREAM_PRIVATE_ACCOUNT_AND_KEY", { status: 502 });
    await assert.rejects(syncBanking(), error => /saved history/.test(error.message) && !error.message.includes("PRIVATE"));
    globalThis.fetch = async () => new Response("private", { status: 409 });
    await assert.rejects(mapBankAccount("1", "old"), /changed/);
    globalThis.fetch = async () => new Response(JSON.stringify({ accounts: null }), { status: 200 });
    await assert.rejects(fetchBanking(), /incomplete/);
  } finally { globalThis.fetch = original; }
});

test('a failed balance does not turn an unknown account currency into GBP', () => {
 const base = { accounts: [{id:'unknown',account_id:null,name:'Unknown currency',institution_name:'QA Bank',currency:null,balance:null}],local_accounts:[] };
 assert.equal(bankRows(base)[0].currency,'Unknown');
 const mapped = {...base,accounts:[{...base.accounts[0],account_id:'saved'}],local_accounts:[{id:'saved',bank:'QA Bank',display_name:'Known saved currency',account_type:'current',currency:'SGD'}]};
 assert.equal(bankRows(mapped)[0].currency,'SGD');
});


test("available-credit settings use a reversible account-scoped mutation", async () => {
  const original = globalThis.fetch, calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify(fixture()), { status: 200 }); };
    await setBankBalanceRule("id/a", { balance_type: "available_credit", credit_limit: 8750, currency: "GBP" });
    await setBankBalanceRule("id/a", { balance_type: "reported" });
    assert.deepEqual(calls.map(call => call.url), ["/api/banking/accounts/id%2Fa/balance-rule", "/api/banking/accounts/id%2Fa/balance-rule"]);
    assert.deepEqual(calls.map(call => JSON.parse(call.options.body)), [{ balance_type: "available_credit", credit_limit: 8750, currency: "GBP" }, { balance_type: "reported" }]);
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers.get("X-Ledger-Request"), "1");
  } finally { globalThis.fetch = original; }
});

test('native currency amounts preserve supported minor units across ISO precision groups', () => {
  assert.match(nativeMoney(-1.664, 'KWD'), /1\.664/);
  assert.match(nativeMoney(-1.664, 'IQD'), /1\.664/);
  assert.match(nativeMoney(-1664, 'JPY'), /1,664$/);
  assert.match(nativeMoney(-1.66, 'GBP'), /1\.66$/);
  assert.match(nativeMoney(-1.66, 'EUR'), /1\.66$/);
  assert.match(nativeMoney(-1.66, 'AFN'), /1\.66$/);
});
