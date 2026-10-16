import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { rebaseCommonHistory, groupBrokerageAccounts, rankInvestmentAccounts, priceMovers, defaultSpotlight, signedPercent } = await sourceModule("investmentAnalysis");
const { fetchInvestmentAnalysis, fetchInvestmentInsights, fetchSecurityAnalysis, refreshInvestmentMarket } = await sourceModule("investmentAnalysisApi");
const holding = (fields = {}) => ({ id: "a", instrument_id: "listing-a", market_currency: "USD", currency: "USD", value: 100, is_cash: false, returns: { "1d": 2 }, ...fields });

test("comparison rebases native prices on exact shared dates, without interpolating missing sessions", () => {
  const result = rebaseCommonHistory([
    { id: "a", name: "A", currency: "USD", points: [{ date: "2026-01-01", close: 5 }, { date: "2026-01-02", close: 10 }, { date: "2026-01-03", close: 12 }, { date: "2026-01-04", close: 15 }] },
    { id: "b", name: "B", currency: "GBP", points: [{ date: "2026-01-02", close: 200 }, { date: "2026-01-04", close: 180 }] },
  ]);
  assert.deepEqual(result.dates, ["2026-01-02", "2026-01-04"]);
  assert.deepEqual(result.series[0].values, [0, 50]);
  assert.ok(Math.abs(result.series[1].values[1] + 10) < 1e-10);
  assert.deepEqual(result.series.map((row) => row.currency), ["USD", "GBP"]);
});

test("comparison refuses one shared observation, absent history and zero or invalid price baselines", () => {
  const row = (points) => ({ id: "a", name: "A", currency: "GBP", points });
  assert.deepEqual(rebaseCommonHistory([]).series, []);
  assert.deepEqual(rebaseCommonHistory([row([{ date: "2026-01-01", close: 0 }, { date: "2026-01-02", close: 10 }])]).series, []);
  assert.deepEqual(rebaseCommonHistory([row([{ date: "2026-01-01", close: 10 }, { date: "2026-01-02", close: 12 }]), row([])]).series, []);
  assert.equal(signedPercent(null), "—");
  assert.equal(signedPercent(0), "0.00%");
});

test("broker grouping preserves every account identity, including same-name subaccounts", () => {
  const rows = [{ id: "one", institution: "Trading212", name: "Invest" }, { id: "two", institution: "Trading 212", name: "Invest" }, { id: "three", institution: "Moomoo US", name: "Margin" }, { id: "four", institution: "Moomoo", name: "Cash" }];
  const groups = groupBrokerageAccounts(rows);
  assert.deepEqual(groups.map((row) => [row.name, row.accounts.map((a) => a.id)]), [["Trading 212", ["one", "two"]], ["Moomoo", ["three", "four"]]]);
});

test("account priority preserves valid zero and unknown balances without merging identities", () => {
  const rows = [{ id: "unknown", name: "A", value: null }, { id: "zero", name: "B", value: 0 }, { id: "negative", name: "C", value: -50 }, { id: "funded", name: "D", value: 100 }];
  assert.deepEqual(rankInvestmentAccounts(rows).map((row) => row.id), ["funded", "negative", "zero", "unknown"]);
  assert.equal(rows[0].id, "unknown", "ranking does not mutate API response order");
});

test("review uses exact selected scope and local reads; incomplete responses fail safely", async () => {
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ summary: {}, coverage: {}, items: [] }), { status: 200 }); };
    await fetchInvestmentInsights(["paper&exact"], "USD");
    const params = new URL(calls[0].url, "http://local").searchParams;
    assert.deepEqual(params.getAll("account"), ["paper&exact"]);
    assert.equal(params.get("currency"), "USD");
    assert.equal(calls[0].options.method, undefined);
    await fetchInvestmentInsights([], "GBP");
    assert.deepEqual(new URL(calls[1].url, "http://local").searchParams.getAll("account"), ["__none__"]);
    globalThis.fetch = async () => new Response(JSON.stringify({ summary: {}, items: [] }), { status: 200 });
    await assert.rejects(fetchInvestmentInsights(["a"], "GBP"), /incomplete/);
    globalThis.fetch = async () => new Response("PRIVATE RESPONSE", { status: 502 });
    await assert.rejects(fetchInvestmentInsights(["a"], "GBP"), (error) => error.message.includes("502") && !error.message.includes("PRIVATE"));
  } finally { globalThis.fetch = original; }
});

test("movers preserve distinct listing identities and exclude cash, unknown returns and repeat ownership", () => {
  const rows = [holding(), holding({ id: "duplicate-account" }), holding({ id: "different-listing", instrument_id: "listing-b", symbol: "SAME", returns: { "1d": 3 } }), holding({ id: "cash", instrument_id: "cash", is_cash: true, returns: { "1d": 9 } }), holding({ id: "unknown", returns: { "1d": null } })];
  assert.deepEqual(priceMovers(rows, "1d", "up").map((row) => row.id), ["different-listing", "a"]);
  assert.deepEqual(priceMovers(rows, "1d", "down"), []);
  assert.equal(defaultSpotlight([holding({ id: "unknown", value: 999, returns: { "1d": null } }), holding()]).id, "a");
});

test("investment reads encode exact account and position IDs; refresh is an explicit protected POST", async () => {
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ summary: {}, accounts: [], holdings: [], holding: {}, points: [] }), { status: 200 }); };
    await fetchInvestmentAnalysis(["account&a", "account?b"], "GBP", "3m");
    assert.deepEqual(new URL(calls.at(-1).url, "http://local").searchParams.getAll("account"), ["account&a", "account?b"]);
    await fetchInvestmentAnalysis([], "USD", "all");
    assert.deepEqual(new URL(calls.at(-1).url, "http://local").searchParams.getAll("account"), ["__none__"]);
    await fetchSecurityAnalysis("account&a", "position?b", "USD", "1m");
    assert.equal(new URL(calls.at(-1).url, "http://local").searchParams.get("position"), "position?b");
    assert.equal(calls.at(-1).options.cache, "no-store");
    await refreshInvestmentMarket();
    assert.equal(calls.at(-1).url, "/api/investments/market/refresh");
    assert.equal(calls.at(-1).options.method, "POST");
    assert.equal(calls.at(-1).options.headers.get("X-Ledger-Request"), "1");
  } finally { globalThis.fetch = original; }
});

test("analysis and refresh failures never expose raw response data", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("PRIVATE BROKER RESPONSE", { status: 502 });
    await assert.rejects(fetchInvestmentAnalysis(["a"], "GBP", "all"), (error) => error.message.includes("502") && !error.message.includes("PRIVATE"));
    await assert.rejects(refreshInvestmentMarket(), (error) => error.message.includes("Saved prices") && !error.message.includes("PRIVATE"));
  } finally { globalThis.fetch = original; }
});
