import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { api } = await sourceModule("api");
const json = (value) => new Response(JSON.stringify(value), { status: 200 });

test("portfolio source selections reach totals, quote refresh and paginated fills consistently", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const requested = [];
  globalThis.fetch = async (url) => { requested.push(new URL(url, "http://localhost")); return json({}); };
  const sources = ["trading212:isa", "moomoo:cash account"];
  await api.investments(sources);
  await api.valuation(true, sources);
  await api.investmentTrades("US1234567890", 100, sources);
  assert.deepEqual(requested.map((url) => url.searchParams.getAll("account")), [sources, sources, sources]);
  assert.equal(requested[1].searchParams.get("refresh"), "true");
  assert.equal(requested[2].searchParams.get("offset"), "100");
  assert.equal(requested[2].searchParams.get("isin"), "US1234567890");
});

test("an explicit empty portfolio selection never becomes all accounts", async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const requested = [];
  globalThis.fetch = async (url) => { requested.push(new URL(url, "http://localhost")); return json({}); };
  await api.investments();
  await api.investments([]);
  await api.valuation(false, []);
  await api.investmentTrades("US1234567890", 0, []);
  assert.deepEqual(requested[0].searchParams.getAll("account"), []);
  for (const url of requested.slice(1)) assert.deepEqual(url.searchParams.getAll("account"), ["__none__"]);
});
