import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { money, shortDate } = await sourceModule("format");
const { api } = await sourceModule("api");

test("unknown values remain visibly unknown; a known zero remains £0", () => {
  for (const unknown of [null, undefined, NaN, Infinity]) assert.equal(money(unknown), "—");
  assert.equal(money(0), "£0.00");
  assert.equal(money(-123.45), "-£123.45");
});

test("statement calendar dates do not shift with the viewer's timezone", () => {
  const previous = process.env.TZ;
  try {
    for (const timezone of ["America/Los_Angeles", "Europe/London", "Pacific/Auckland"]) {
      process.env.TZ = timezone;
      assert.equal(shortDate("2026-01-01"), "01 Jan 26", timezone);
    }
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
  assert.equal(shortDate("not a date"), "—");
});

test("zero contribution and zero starting wealth reach the planning API", async () => {
  const original = globalThis.fetch;
  try {
    let called;
    globalThis.fetch = async (url) => { called = url; return new Response("{}", { status: 200 }); };
    await api.planning({ years: 1, annual_return: 0, inflation: 0, target: 100, initial_wealth: 0, monthly_contribution: 0 });
    const query = new URL(called, "http://localhost").searchParams;
    assert.equal(query.get("initial_wealth"), "0");
    assert.equal(query.get("monthly_contribution"), "0");
    assert.equal(query.get("annual_return"), "0");
  } finally { globalThis.fetch = original; }
});

test("backend validation errors are shown with useful detail", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ msg: "Target must be positive" }] }), { status: 422 });
    await assert.rejects(api.summary, /Target must be positive/);
    globalThis.fetch = async () => new Response("upstream unavailable", { status: 502 });
    await assert.rejects(api.summary, /Request failed \(502\)/);
  } finally { globalThis.fetch = original; }
});

test("missing investment accounts have an empty state; service failures remain errors", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "No investment account found" }), { status: 404 });
    assert.equal(await api.investments(), null);
    assert.equal(await api.valuation(), null);
    globalThis.fetch = async () => new Response("temporarily unavailable", { status: 503 });
    await assert.rejects(api.investments, /503/);
  } finally { globalThis.fetch = original; }
});
