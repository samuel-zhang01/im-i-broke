import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { accountCurrencyTotals, balanceCurrencyTotals, nativeMoney, accountLabel, compareOptionalNumbers, hasOldBrokerageData, portfolioDate } = await sourceModule("connectedPortfolio");
const { fetchConnectedPortfolio } = await sourceModule("snaptradeApi");
const section = (data) => ({ status: data.length ? "ok" : "empty", data, error: null, truncated: false });
const account = (values) => ({ id: "test", currency: "GBP", total_value: null, balances: section([]), ...values });

test("connected account values keep currencies separate and do not add cash twice", () => {
  const accounts = [
    account({ id: "gbp", total_value: 100, balances: section([{ currency: "GBP", cash: 25 }]) }),
    account({ id: "gbp-zero", total_value: 0 }),
    account({ id: "gbp-unknown", total_value: null }),
    account({ id: "usd", currency: "USD", total_value: 200 }),
    account({ id: "unlabelled", currency: null, total_value: 999 }),
  ];
  const totals = accountCurrencyTotals(accounts);
  assert.deepEqual(totals.map(({ currency, total, known, missing }) => ({ currency, total, known, missing })), [
    { currency: "GBP", total: 100, known: 2, missing: 1 },
    { currency: "USD", total: 200, known: 1, missing: 0 },
  ]);
  assert.equal(accountCurrencyTotals([account({ currency: "EUR" })])[0].total, null);
});

test("cash and buying-power aggregation preserve zero, missing data, and native currency", () => {
  const accounts = [account({ balances: section([
    { currency: "GBP", cash: 0, buying_power: 10 },
    { currency: "USD", cash: null, buying_power: null },
    { currency: null, cash: 999, buying_power: 999 },
  ]) }), account({ balances: section([{ currency: "GBP", cash: -5, buying_power: null }]) })];
  assert.deepEqual(balanceCurrencyTotals(accounts, "cash"), [
    { currency: "GBP", total: -5, missing: 0 }, { currency: "USD", total: null, missing: 1 },
  ]);
  assert.deepEqual(balanceCurrencyTotals(accounts, "buying_power"), [
    { currency: "GBP", total: 10, missing: 1 }, { currency: "USD", total: null, missing: 1 },
  ]);
  for (const unknown of [null, undefined, NaN, Infinity]) assert.equal(nativeMoney(unknown, "GBP"), "—");
  assert.equal(nativeMoney(0, "USD"), "US$0.00");
  assert.equal(nativeMoney(10, null), "Unknown currency 10.00");
});

test("unknown numeric values sort last in either direction", () => {
  for (const direction of ["asc", "desc"]) {
    assert.equal(compareOptionalNumbers(null, 1, direction), 1);
    assert.equal(compareOptionalNumbers(1, null, direction), -1);
    assert.equal(compareOptionalNumbers(null, null, direction), 0);
  }
  assert.ok(compareOptionalNumbers(5, 10, "desc") > 0);
});

test("old brokerage snapshots and unknown timestamps are distinguished", () => {
  const now = Date.parse("2026-09-18T12:00:00Z");
  assert.equal(hasOldBrokerageData([account({ holdings_synced_at: "2026-09-16T11:00:00Z" })], now), true);
  assert.equal(hasOldBrokerageData([account({ holdings_synced_at: "2026-09-18T11:00:00Z" })], now), false);
  assert.equal(hasOldBrokerageData([account({ holdings_synced_at: null })], now), false);
  assert.equal(portfolioDate("invalid"), "Not reported");
  assert.equal(portfolioDate("2026-09-18"), "18 Sept 2026");
});

test("portfolio requests use only the same-origin endpoint with no caching and expose no raw error body", async () => {
  const original = globalThis.fetch;
  try {
    let received;
    globalThis.fetch = async (url, options) => {
      received = { url, options };
      return new Response(JSON.stringify({ configured: true, accounts: [], connections: section([]), retrieved_at: "2026-09-18T12:00:00Z", error: null }), { status: 200 });
    };
    const result = await fetchConnectedPortfolio();
    assert.equal(result.configured, true);
    assert.equal(received.url, "/api/investments/connected");
    assert.equal(received.options.cache, "no-store");
    assert.equal(received.options.credentials, "same-origin");
    assert.equal(received.options.headers.get("X-Ledger-Request"), "1");
    assert.equal(received.options.method, undefined);
    await fetchConnectedPortfolio(undefined, true);
    assert.equal(received.url, "/api/investments/connected/refresh");
    assert.equal(received.options.method, "POST");
    assert.equal(received.options.headers.get("X-Ledger-Request"), "1");
    globalThis.fetch = async () => new Response("PRIVATE ACCOUNT DATA", { status: 502 });
    await assert.rejects(fetchConnectedPortfolio(), (error) => /502/.test(error.message) && !error.message.includes("PRIVATE"));
    globalThis.fetch = async () => new Response("quota", { status: 429 });
    await assert.rejects(fetchConnectedPortfolio(), /Too many refreshes/);
    globalThis.fetch = async () => new Response("{}", { status: 200 });
    await assert.rejects(fetchConnectedPortfolio(), /response was incomplete/);
  } finally { globalThis.fetch = original; }
});

test("currency labels stay explicit and account suffixes are not repeated", () => {
  assert.equal(nativeMoney(123456.78, "GBP"), "£123,456.78");
  assert.equal(nativeMoney(10, "GBp"), "Unknown currency 10.00");
  assert.equal(accountLabel({ name: "Margin (8270)", institution: "Broker", number_suffix: "8270" }), "Margin (8270)");
  assert.equal(accountLabel({ name: "Invest", institution: "Broker", number_suffix: "1234" }), "Invest · ••1234");
});
