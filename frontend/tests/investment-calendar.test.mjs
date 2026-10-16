import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { calendarOffset, calendarMonthLabel, compactCalendarChange, fetchInvestmentCalendar } = await sourceModule("calendarApi");

test("calendar uses Monday-first UTC dates, including leap years", () => {
  assert.equal(calendarOffset(2026, 9), 1);
  assert.equal(calendarOffset(2026, 2), 6);
  assert.equal(calendarOffset(2024, 2), 3);
  assert.equal(calendarMonthLabel(2026, 9), "September 2026");
});

test("calendar distinguishes absent data from a flat day and formats signed movement", () => {
  for (const value of [null, undefined, NaN, Infinity]) assert.equal(compactCalendarChange(value), "—");
  assert.equal(compactCalendarChange(0), "0");
  assert.equal(compactCalendarChange(.42), "+0.42");
  // ICU versions differ on en-GB compact suffix casing; value and sign must agree.
  assert.match(compactCalendarChange(1520), /^\+1\.5[kK]$/);
  assert.match(compactCalendarChange(-1520), /^-1\.5[kK]$/);
});

test("calendar scope is encoded exactly and errors cannot expose provider response bodies", async () => {
  const oldFetch = globalThis.fetch;
  try {
    let request;
    globalThis.fetch = async (url, options) => {
      request = { url, options };
      return new Response(JSON.stringify({ metric: "account_value_change", days: [], months: [], points: [], summary: {} }), { status: 200 });
    };
    await fetchInvestmentCalendar(["broker account &one", "two"], "USD", 2026, 9, "recorded");
    const query = new URL(request.url, "http://localhost").searchParams;
    assert.deepEqual(query.getAll("account"), ["broker account &one", "two"]);
    assert.equal(query.get("source"), "recorded");
    assert.equal(query.get("currency"), "USD");
    assert.equal(request.options.cache, "no-store");
    assert.equal(request.options.credentials, "same-origin");
    await fetchInvestmentCalendar([], "GBP", 2026, 9, "estimate");
    assert.deepEqual(new URL(request.url, "http://localhost").searchParams.getAll("account"), ["__none__"]);
    globalThis.fetch = async () => new Response("PRIVATE PROVIDER BODY", { status: 502 });
    await assert.rejects(fetchInvestmentCalendar([], "GBP", 2026, 9, "estimate"), (error) => error.message.includes("502") && !error.message.includes("PRIVATE"));
    globalThis.fetch = async () => new Response("{}", { status: 200 });
    await assert.rejects(fetchInvestmentCalendar([], "GBP", 2026, 9, "estimate"), /incomplete/);
  } finally {
    globalThis.fetch = oldFetch;
  }
});
