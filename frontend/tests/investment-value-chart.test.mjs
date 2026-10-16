import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { investmentValueChartData, indexInvestmentValueChart } = await sourceModule("investmentValueChart");
const history = (date, value, fields = {}) => ({ date, net_worth: value, assets: null, liabilities: null, known_accounts: 1, missing_accounts: 0, estimated: false, carried_accounts: 0, coverage_key: "a", ...fields });
const recent = (timestamp, value, fields = {}) => ({ timestamp, value, priced_holdings: 1, holdings_count: 1, known_accounts: 1, missing_accounts: 0, oldest_price_at: null, estimated: true, coverage_key: "a", ...fields });
const visibleValues = data => data.series.map(series => series.data.filter(([, value]) => value !== null));
const utc = value => Date.parse(value);

test("both sources share a numeric UTC axis without fabricated matching observations", () => {
  const data = investmentValueChartData(
    [history("2026-09-17", 100), history("2026-09-18", 105)],
    [recent("2026-09-17T09:00:00+01:00", 102), recent("2026-09-18T09:00:00Z", 106)],
  );
  assert.deepEqual(data.timestamps, [utc("2026-09-17"), utc("2026-09-17T08:00:00Z"), utc("2026-09-18"), utc("2026-09-18T09:00:00Z")]);
  assert.deepEqual(data.series.map(({ id, kind, label }) => ({ id, kind, label })), [
    { id: "history-0", kind: "history", label: "Asset history" },
    { id: "recent-0", kind: "recent", label: "Recent prices · estimate" },
  ]);
  assert.deepEqual(visibleValues(data), [
    [[utc("2026-09-17"), 100], [utc("2026-09-18"), 105]],
    [[utc("2026-09-17T08:00:00Z"), 102], [utc("2026-09-18T09:00:00Z"), 106]],
  ]);
  assert.equal(data.start, data.timestamps[0]);
  assert.equal(data.end, data.timestamps.at(-1));
});

test("history keeps missing, incomplete, coverage and valuation source breaks", () => {
  const points = [
    history("2026-09-10", 0), history("2026-09-11", null),
    history("2026-09-12", 10), history("2026-09-13", 20, { complete: false }),
    history("2026-09-14", 30), history("2026-09-15", 40, { coverage_key: "ab" }),
    history("2026-09-16", 50, { coverage_key: "ab", valuation_key: "broker" }),
    history("2026-09-17", 60, { coverage_key: "ab", valuation_key: "broker", source_break: true }),
    history("2026-09-18", 65, { coverage_key: "ab", valuation_key: "broker", estimated: true }),
  ];
  const data = investmentValueChartData(points, []);
  assert.deepEqual(visibleValues(data).map(values => values.map(([, value]) => value)), [[0], [10], [30], [40], [50], [60, 65]]);
  assert.equal(data.series.at(-1).partial, true);
  assert.equal(data.series[0].data[1][1], null);
  assert.equal(data.timestamps.length, points.length, "Missing observations retain their timestamps for inspection");
});

test("recent-price gaps and changing coverage never become apparent market moves", () => {
  const points = [
    recent("2026-09-18T09:00:00Z", 0), recent("2026-09-18T10:00:00Z", null),
    recent("2026-09-18T11:00:00Z", 10), recent("2026-09-18T12:00:00Z", 110, { coverage_key: "ab" }),
    recent("2026-09-18T13:00:00Z", 115, { coverage_key: "ab", priced_holdings: 1, holdings_count: 2 }),
  ];
  const data = investmentValueChartData([], points);
  assert.deepEqual(visibleValues(data).map(values => values.map(([, value]) => value)), [[0], [10], [110, 115]]);
  assert.equal(data.series.at(-1).partial, true);
  assert.equal(data.series[0].partial, false);
});

test("window bounds are inclusive and never synthesize endpoints or remove segment identity", () => {
  const points = [history("2026-09-15", 10), history("2026-09-16", null), history("2026-09-17", 20), history("2026-09-18", 30)];
  const start = utc("2026-09-17"), end = utc("2026-09-18");
  const data = investmentValueChartData(points, [], { start, end });
  assert.deepEqual(data.timestamps, [start, end]);
  assert.equal(data.series[0].id, "history-1");
  assert.deepEqual(visibleValues(data), [[[start, 20], [end, 30]]]);
  const between = investmentValueChartData(points, [], { start: start + 1, end: end - 1 });
  assert.deepEqual(between, { series: [], timestamps: [], start: start + 1, end: end - 1 });
  assert.deepEqual(investmentValueChartData(points, [], { start: end, end: start }), data);
});

test("chart output is chronological without mutating source observations", () => {
  const points = [history("2026-09-18", 2), history("2026-09-17", 1)];
  const copy = structuredClone(points);
  const data = investmentValueChartData(points, []);
  assert.deepEqual(data.timestamps, [utc("2026-09-17"), utc("2026-09-18")]);
  assert.deepEqual(data.series[0].data, [[utc("2026-09-17"), 1], [utc("2026-09-18"), 2]]);
  assert.deepEqual(points, copy);
});

test("invalid or naive times cannot create a bridge between valid observations", () => {
  const data = investmentValueChartData(
    [history("2026-09-16", 1), history("2026-02-30", 2), history("2026-09-17", 3), history("2026-09-17T12:00:00", 4), history("2026-09-18", 5)],
    [recent("2026-09-18T08:00:00Z", 6), recent("2026-09-18T09:00:00", 7), recent("2026-09-18T10:00:00Z", 8)],
  );
  assert.deepEqual(visibleValues(data).map(values => values.map(([, value]) => value)), [[1], [3], [5], [6], [8]]);
  assert.equal(data.timestamps.length, 5);
  assert.ok(data.timestamps.every(Number.isFinite));
});

test("daily account dates and offset market times stay UTC in every browser timezone", () => {
  const old = process.env.TZ;
  try {
    for (const zone of ["Europe/London", "America/Los_Angeles", "Pacific/Auckland"]) {
      process.env.TZ = zone;
      const data = investmentValueChartData([history("2026-09-18", 1)], [recent("2026-09-18T01:00:00+01:00", 2)]);
      assert.deepEqual(data.timestamps, [utc("2026-09-18T00:00:00Z")]);
      assert.deepEqual(visibleValues(data), [[[data.start, 1]], [[data.start, 2]]]);
    }
  } finally { if (old === undefined) delete process.env.TZ; else process.env.TZ = old; }
});

test("empty and unsupported values remain absent rather than becoming zero", () => {
  assert.deepEqual(investmentValueChartData([], []), { series: [], timestamps: [], start: null, end: null });
  const data = investmentValueChartData([history("2026-09-18", Infinity)], [recent("2026-09-18T10:00:00Z", NaN)], { start: NaN, end: Infinity });
  assert.deepEqual(data.series, []);
  assert.deepEqual(data.timestamps, [utc("2026-09-18"), utc("2026-09-18T10:00:00Z")]);
});

test("indexed trends start at the first exact shared observation and retain real timestamps", () => {
  const source = investmentValueChartData(
    [history("2026-09-15", 80), history("2026-09-16", 100), history("2026-09-17", 110)],
    [recent("2026-09-15T12:00:00Z", 980), recent("2026-09-16T01:00:00+01:00", 1000), recent("2026-09-16T12:00:00Z", 1010), recent("2026-09-17T00:00:00Z", 1050)],
    { start: utc("2026-09-14"), end: utc("2026-09-18") },
  );
  const copy = structuredClone(source);
  const indexed = indexInvestmentValueChart(source);
  assert.equal(indexed.baseline, utc("2026-09-16"));
  assert.equal(indexed.model.start, indexed.baseline);
  assert.equal(indexed.model.end, source.end);
  assert.deepEqual(indexed.model.timestamps, [utc("2026-09-16"), utc("2026-09-16T12:00:00Z"), utc("2026-09-17")]);
  assert.deepEqual(visibleValues(indexed.model).map(values => values.map(([timestamp, value]) => [timestamp, Math.round(value * 100) / 100])), [
    [[utc("2026-09-16"), 100], [utc("2026-09-17"), 110]],
    [[utc("2026-09-16"), 100], [utc("2026-09-16T12:00:00Z"), 101], [utc("2026-09-17"), 105]],
  ]);
  assert.deepEqual(source, copy, "Indexing never overwrites actual currency values");
});

test("indexed trends require a positive known value from both lines at the baseline", () => {
  const source = investmentValueChartData(
    [history("2026-09-14", 0), history("2026-09-15", 10), history("2026-09-16", 10), history("2026-09-17", 10), history("2026-09-18", 0)],
    [recent("2026-09-14T00:00:00Z", 100), recent("2026-09-15T00:00:00Z", -100), recent("2026-09-16T00:00:00Z", null), recent("2026-09-17T00:00:00Z", 100), recent("2026-09-18T00:00:00Z", 0)],
  );
  const indexed = indexInvestmentValueChart(source);
  assert.equal(indexed.baseline, utc("2026-09-17"));
  assert.deepEqual(visibleValues(indexed.model).map(values => values.map(([, value]) => value)), [[100, 0], [100, 0]]);
  assert.deepEqual(indexed.model.series.map(segment => segment.id), ["history-0", "recent-1"]);
});

test("indexing preserves missing observations and coverage/source segment boundaries", () => {
  const source = investmentValueChartData(
    [history("2026-09-14", 5), history("2026-09-15", 10, { source_break: true }), history("2026-09-16", null), history("2026-09-17", 11), history("2026-09-18", 12, { coverage_key: "ab" })],
    [recent("2026-09-15T00:00:00Z", 100), recent("2026-09-16T00:00:00Z", null), recent("2026-09-17T00:00:00Z", 120, { coverage_key: "ab" })],
  );
  const indexed = indexInvestmentValueChart(source);
  assert.deepEqual(indexed.model.series.map(segment => segment.id), ["history-1", "history-2", "history-3", "recent-0", "recent-1"]);
  assert.deepEqual(visibleValues(indexed.model).map(values => values.map(([, value]) => Math.round(value))), [[100], [110], [120], [100], [120]]);
  for (const segment of indexed.model.series) {
    assert.equal(segment.data.find(([timestamp]) => timestamp === utc("2026-09-16"))[1], null);
  }
  assert.deepEqual(indexed.model.timestamps, [utc("2026-09-15"), utc("2026-09-16"), utc("2026-09-17"), utc("2026-09-18")]);
});

test("disjoint or unusable baselines leave values unchanged rather than aligning different times", () => {
  for (const source of [
    investmentValueChartData([history("2026-09-17", 10)], [recent("2026-09-17T00:00:01Z", 100)]),
    investmentValueChartData([history("2026-09-17", 10)], [recent("2026-09-18T00:00:00Z", 100)]),
    investmentValueChartData([history("2026-09-17", 0)], [recent("2026-09-17T00:00:00Z", 100)]),
    investmentValueChartData([history("2026-09-17", 10)], [recent("2026-09-17T00:00:00Z", Infinity)]),
    investmentValueChartData([history("2026-09-17", null)], [recent("2026-09-17T00:00:00Z", 100)]),
    investmentValueChartData([], []),
  ]) {
    const indexed = indexInvestmentValueChart(source);
    assert.equal(indexed.baseline, null);
    assert.equal(indexed.model, source);
  }
});

test("combined chart uses receipt times and explicitly places date-only marks at day end", () => {
  const data = investmentValueChartData([
    history("2026-09-18", 100),
    history("2026-09-21", 105, { observed_at: "2026-09-21T10:07:00Z" }),
  ], [], { dailyAtEnd: true, end: utc("2026-09-21T10:30:00Z") });
  assert.deepEqual(data.timestamps, [utc("2026-09-18T23:59:59.999Z"), utc("2026-09-21T10:07:00Z")]);
  const today = investmentValueChartData([history("2026-09-21", 105)], [], { dailyAtEnd: true, end: utc("2026-09-21T10:30:00Z") });
  assert.equal(today.timestamps[0], utc("2026-09-21T10:30:00Z"));
});
