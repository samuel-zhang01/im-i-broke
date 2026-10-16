import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { balanceSeriesData, balanceTooltip, chartPeriodLabel, signedBarPoint, bankBalanceGeometry, bankBalanceTickLabels } = await sourceModule("chartData");

test("balance gaps carry their original point date, never backfill before evidence", () => {
  const points = balanceSeriesData(["2026-01", "2026-02", "2026-03", "2026-04"], [
    { period: "2026-02", balance: 0 }, { period: "2026-04", balance: -25 },
  ]);
  assert.deepEqual(points, [
    { value: null, savedPeriod: null, carried: false },
    { value: 0, savedPeriod: "2026-02", carried: false },
    { value: 0, savedPeriod: "2026-02", carried: true },
    { value: -25, savedPeriod: "2026-04", carried: false },
  ]);
});

test("an explicit unknown balance interrupts the carried-forward line", () => {
  const points = balanceSeriesData(["2026-01", "2026-02", "2026-03"], [
    { period: "2026-01", balance: 100 }, { period: "2026-02", balance: null },
  ]);
  assert.equal(points[0].value, 100);
  assert.deepEqual(points.slice(1), [
    { value: null, savedPeriod: null, carried: false },
    { value: null, savedPeriod: null, carried: false },
  ]);
});

test("balance tooltip discloses carry-forward and safely renders untrusted account labels", () => {
  const html = balanceTooltip([
    { axisValue: "2026-03", seriesName: '<img src=x onerror="alert(1)">', data: { value: 0, savedPeriod: "2026-02", carried: true } },
    { seriesName: "Unknown & account", data: { value: null, savedPeriod: null, carried: false } },
  ]);
  assert.match(html, /Carried from 2026-02/);
  assert.match(html, /£0\.00/);
  assert.match(html, /No supported balance/);
  assert.match(html, /&lt;img/);
  assert.match(html, /Unknown &amp; account/);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /NaN|undefined/);
});

test("signed bars keep losses and refunds on the correct side; invalid values stay missing", () => {
  for (const value of [null, undefined, NaN, Infinity, "25"]) assert.equal(signedBarPoint(value, "#123456").value, null);
  assert.equal(signedBarPoint(0, "#123456").value, 0);
  const loss = signedBarPoint(-15, "#123456", true);
  assert.equal(loss.value, -15);
  assert.deepEqual(loss.itemStyle.borderRadius, [4, 0, 0, 4]);
  assert.deepEqual(signedBarPoint(-15, "#123456").itemStyle.borderRadius, [0, 0, 4, 4]);
  assert.equal(signedBarPoint(15, "#123456").value, 15);
});

test("compact calendar labels do not shift date across viewer timezones", () => {
  const previous = process.env.TZ;
  try {
    for (const timezone of ["America/Los_Angeles", "Europe/London", "Pacific/Auckland"]) {
      process.env.TZ = timezone;
      assert.equal(chartPeriodLabel("2026-01"), "Jan 26");
      assert.equal(chartPeriodLabel("2026-01-01"), "1 Jan");
    }
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
  assert.equal(chartPeriodLabel("Unknown period"), "Unknown period");
});

test("bank chart spaces irregular fetches by elapsed time rather than observation index", () => {
  const chart = bankBalanceGeometry([
    { amount: -20, currency: "GBP", fetched_at: "2026-09-11T00:00:00Z" },
    { amount: 100, currency: "GBP", fetched_at: "2026-09-01T00:00:00Z" },
    { amount: 0, currency: "GBP", fetched_at: "2026-09-01T01:00:00Z" },
  ], "GBP");
  assert.deepEqual(chart.points.map(point => point.amount), [100, 0, -20]);
  assert.deepEqual(chart.points.map(point => point.x), [0, 1 / 240, 1]);
  assert.equal(chart.minimum, -20);
  assert.equal(chart.maximum, 100);
  assert.equal(chart.points[1].y, chart.zeroY);
  assert.ok(chart.low < -20 && chart.high > 100);
});

test("one zero bank observation is visible and malformed dates or other currencies cannot fabricate a trend", () => {
  const chart = bankBalanceGeometry([
    { amount: 0, currency: "GBP", fetched_at: "2026-09-01 01:00:00" },
    { amount: 100, currency: "USD", fetched_at: "2026-09-02T00:00:00Z" },
    { amount: 100, currency: "GBP", fetched_at: "2026-02-30T00:00:00Z" },
    { amount: 100, currency: "GBP", fetched_at: "unknown" },
    { amount: NaN, currency: "GBP", fetched_at: "2026-09-03T00:00:00Z" },
  ], "GBP");
  assert.equal(chart.points.length, 1);
  assert.equal(chart.points[0].x, .5);
  assert.equal(chart.points[0].y, .5);
  assert.equal(chart.points[0].amount, 0);
  assert.equal(chart.points[0].timestamp, Date.parse("2026-09-01T01:00:00Z"));
  assert.equal(bankBalanceGeometry([], "GBP").minimum, null);
});

test("equivalent fetch instants deduplicate while conflicting amounts at one instant are withheld", () => {
  const rows = [
    { amount: 10, currency: "GBP", fetched_at: "2026-09-01T00:00:00Z" },
    { amount: 10, currency: "GBP", fetched_at: "2026-09-01T01:00:00+01:00" },
  ];
  assert.equal(bankBalanceGeometry(rows, "GBP").points.length, 1);
  rows[1].amount = 20;
  assert.equal(bankBalanceGeometry(rows, "GBP").points.length, 0);
});


test("bank balance axis labels distinguish nearby balances without hiding small movements", () => {
  const compact = (low, high) => Object.fromEntries(Object.entries(bankBalanceTickLabels(low, high)).map(([key, value]) => [key, value.toLowerCase()]));
  assert.deepEqual(compact(1200, 1234.56), { low: "1.2k", high: "1.23k" });
  assert.deepEqual(compact(-1234.56, -1200), { low: "-1.23k", high: "-1.2k" });
  assert.deepEqual(bankBalanceTickLabels(1200.01, 1200.02), { low: "1,200.01", high: "1,200.02" });
  assert.deepEqual(compact(-1250000, 2500000), { low: "-1.3m", high: "2.5m" });
  const history = bankBalanceGeometry([
    { amount: 1200, currency: "GBP", fetched_at: "2026-09-20T10:00:00Z" },
    { amount: 1234.56, currency: "GBP", fetched_at: "2026-09-21T10:00:00Z" },
  ], "GBP");
  const ticks = bankBalanceTickLabels(history.low, history.high);
  assert.notEqual(ticks.low, ticks.high);
  assert.deepEqual([history.minimum, history.maximum], [1200, 1234.56]);
});
