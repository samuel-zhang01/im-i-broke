import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { archiveWealthSource, fetchWealth, refreshWealthRates } = await sourceModule("wealthApi");
const { selectedWealthAccounts, wealthComposition, chartPointValue, wealthHistorySegments, wealthHistoryConnectors, wealthHistoryConnectorSeries, wealthHistoryNavigator, wealthPointEvidence, wealthChartHistory, wealthNetAssetsHistory, filterWealthAccounts, clampWealthDates, compactWealth, wealthHistoryDateLabel, wealthSourceGroups, wealthSourceDate, wealthBalanceNeedsReview, wealthValuationLabel } = await sourceModule("wealth");
const point = (value, fields = {}) => ({ date: "2026-09-18", net_worth: value, assets: null, liabilities: null, known_accounts: 1, missing_accounts: 0, estimated: false, carried_accounts: 0, ...fields });

test("wealth filters preserve default, explicit-empty and repeated account selections", async () => {
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify({ summary: {}, accounts: [], points: [], updated: 0, attempted: 0, missing: 0 }), { status: 200 }); };
    await fetchWealth({ currency: "GBP", range: "all", accountIds: null });
    assert.deepEqual(new URL(calls.at(-1).url, "http://localhost").searchParams.getAll("account"), []);
    await fetchWealth({ currency: "USD", range: "3m", accountIds: [] });
    let params = new URL(calls.at(-1).url, "http://localhost").searchParams;
    assert.deepEqual(params.getAll("account"), ["__none__"]);
    assert.equal(params.get("currency"), "USD");
    assert.equal(params.get("range"), "3m");
    await fetchWealth({ currency: "GBP", range: "1y", accountIds: ["statement:a&b", "snaptrade:x"] });
    params = new URL(calls.at(-1).url, "http://localhost").searchParams;
    assert.deepEqual(params.getAll("account"), ["statement:a&b", "snaptrade:x"]);
    assert.equal(calls.at(-1).options.cache, "no-store");
    await refreshWealthRates();
    assert.equal(calls.at(-1).url, "/api/wealth/refresh");
    assert.equal(calls.at(-1).options.method, "POST");
    assert.equal(calls.at(-1).options.headers.get("X-Ledger-Request"), "1");
  } finally { globalThis.fetch = original; }
});

test("wealth failures never surface raw provider or financial response bodies", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("PRIVATE RESPONSE", { status: 502 });
    await assert.rejects(fetchWealth({ currency: "GBP", range: "all", accountIds: null }), (error) => error.message.includes("502") && !error.message.includes("PRIVATE"));
    await assert.rejects(refreshWealthRates(), /Saved rates/);
    await assert.rejects(archiveWealthSource("snaptrade:example", true), (error) => error.message.includes("502") && !error.message.includes("PRIVATE"));
  } finally { globalThis.fetch = original; }
});

test("source archiving and reinstatement use the authenticated reversible endpoint", async () => {
  const original = globalThis.fetch;
  const calls = [];
  try {
    globalThis.fetch = async (url, options) => { calls.push({ url, options }); return new Response("{}", { status: 200 }); };
    await archiveWealthSource("snaptrade:a&b", true);
    await archiveWealthSource("snaptrade:a&b", false);
    assert.deepEqual(calls.map(({ options }) => JSON.parse(options.body)), [{ source_id: "snaptrade:a&b", archived: true }, { source_id: "snaptrade:a&b", archived: false }]);
    for (const call of calls) {
      assert.equal(call.url, "/api/wealth/sources/archive");
      assert.equal(call.options.method, "POST");
      assert.equal(call.options.headers.get("X-Ledger-Request"), "1");
      assert.equal(call.options.headers.get("Content-Type"), "application/json");
    }
  } finally { globalThis.fetch = original; }
});

test("source groups retain zero and unknown connected accounts while separating archived records and aliases", () => {
  const accounts = [
    { id: "zero", kind: "investment", source: "snaptrade", value: 0, default_included: true },
    { id: "unknown", kind: "investment", source: "snaptrade", value: null, history_points: 0, default_included: false },
    { id: "missing-fx", kind: "investment", source: "statement", value: null, history_points: 5, default_included: false },
    { id: "old", kind: "investment", source: "snaptrade", value: 500, archived_by_user: true },
    { id: "history", kind: "investment", source: "statement", value: null, history_only: true },
    { id: "disconnected", kind: "investment", source: "snaptrade", value: null, archived: true },
    { id: "alias", kind: "investment", source: "statement", value: 500, source_status: "linked", archived_by_user: true },
  ];
  const groups = wealthSourceGroups(accounts);
  assert.deepEqual(groups.active.map((account) => account.id), ["zero", "unknown", "missing-fx"]);
  assert.deepEqual(groups.archived.map((account) => account.id), ["old", "history", "disconnected"]);
  assert.deepEqual(groups.linked.map((account) => account.id), ["alias"]);
});

test("balance provenance distinguishes bank retrieval from dated statements and flags uncertainty", () => {
  assert.match(wealthSourceDate({ source: "lunchflow", as_of: "2026-09-18T12:00:00Z" }), /^Fetched /);
  assert.match(wealthSourceDate({ source: "statement", as_of: "2026-08-01" }), /^Statement dated /);
  assert.match(wealthSourceDate({ source: "lunchflow", as_of: "2026-09-15", valuation: { reported_source: "statement" } }), /^Statement dated /);
  assert.equal(wealthSourceDate({ source: "lunchflow", as_of: null }), "Fetch date unknown");
  assert.equal(wealthSourceDate({ source: "statement", as_of: null }), "Statement date unknown");
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "discrepancy" } }), true);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "statement_only" } }), true);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "unverified" } }), true);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "no_discrepancy" } }), false);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "statement_only", requires_review: false } }), false);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "no_discrepancy", requires_review: true } }), true);
  assert.equal(wealthBalanceNeedsReview({ balance_check: { status: "available_credit" } }), false);
  assert.equal(wealthBalanceNeedsReview({}), false);
});

test("composition uses selected converted positive balances and separates liabilities", () => {
  const accounts = [{ id: "cash", kind: "cash", value: 50, native_value: 500 }, { id: "broker", kind: "investment", value: 100 }, { id: "debt", kind: "credit", value: -20 }, { id: "missing", kind: "cash", value: null }, { id: "duplicate", kind: "investment", value: 999 }];
  const selected = selectedWealthAccounts(accounts, ["cash", "broker", "debt", "missing"]);
  assert.equal(selected.length, 4);
  assert.deepEqual(wealthComposition(selected).map(({ kind, value }) => ({ kind, value })), [{ kind: "investment", value: 100 }, { kind: "cash", value: 50 }]);
  assert.deepEqual(wealthComposition(selectedWealthAccounts(accounts, [])), []);
});

test("effective card valuations distinguish estimated dates and unknown debt from original source balances", () => {
  const estimate = { id: "card", name: "Card", institution: "Bank", source: "lunchflow", kind: "credit", value: -1459.56, reported_value: -7145.65, as_of: "2026-09-18", valuation: { basis: "statement_activity_estimate", estimated: true, current_unknown: false } };
  const unknown = { id: "amex", name: "Amex", institution: "Amex", kind: "credit", value: null, reported_value: -1167.60, valuation: { basis: "unknown", estimated: false, current_unknown: true } };
  assert.match(wealthSourceDate(estimate), /^Estimated through /);
  assert.equal(wealthValuationLabel(estimate), "Estimated from statement + booked activity");
  assert.equal(wealthValuationLabel(unknown), "Current balance unknown · excluded from total");
  assert.deepEqual(filterWealthAccounts([estimate, unknown], "", "debt").map(account => account.id), ["card"]);
  assert.deepEqual(filterWealthAccounts([estimate, unknown], "", "unknown").map(account => account.id), ["amex"]);
});

test("history breaks at account coverage changes instead of drawing an apparent gain", () => {
  const points = [point(10, { known_accounts: 1, missing_accounts: 1, coverage_key: "a" }), point(20, { known_accounts: 1, missing_accounts: 1, coverage_key: "a" }), point(120, { known_accounts: 2, coverage_key: "ab" }), point(130, { known_accounts: 2, coverage_key: "ab" }), point(150, { known_accounts: 2, coverage_key: "bc" })];
  const segments = wealthHistorySegments(points);
  assert.equal(segments.length, 3);
  assert.deepEqual(segments[0].values, [10, 20, null, null, null]);
  assert.equal(segments[0].partial, true);
  assert.deepEqual(segments[1].values, [null, null, 120, 130, null]);
  assert.deepEqual(segments[2].values, [null, null, null, null, 150]);
});

test("missing history remains a gap, known zero remains zero, and estimate markers preserve comparable continuity", () => {
  assert.equal(chartPointValue(point(0)), 0);
  assert.equal(chartPointValue(point(0, { known_accounts: 0 })), null);
  assert.equal(chartPointValue(point(null)), null);
  const segments = wealthHistorySegments([point(0), point(null), point(10), point(12, { estimated: true })]);
  assert.deepEqual(segments.map((segment) => segment.values), [[0, null, null, null], [null, null, 10, 12]]);
  assert.equal(segments[1].partial, true);
});

test("history breaks on valuation source changes without inventing a gain between sources", () => {
  const observations = [
    point(100, { estimated: true, valuation_basis: "daily_holdings", valuation_key: "reconstructed-a" }),
    point(110, { estimated: false, valuation_basis: "recorded_balance", valuation_key: "reconstructed-a" }),
    point(150, { valuation_key: "broker-a" }),
    point(155, { valuation_key: "broker-a" }),
    point(200, { valuation_key: "broker-a", source_break: true }),
  ];
  assert.deepEqual(wealthHistorySegments(observations).map(segment => segment.values), [
    [100, 110, null, null, null], [null, null, 150, 155, null], [null, null, null, null, 200],
  ]);
});

test("history evidence distinguishes daily market and FX reconstruction from provider and recorded balances", () => {
  assert.equal(wealthPointEvidence(point(100, { valuation_basis: "daily_holdings" })), "Daily holdings, market prices & dated FX · estimate");
  assert.equal(wealthPointEvidence(point(100, { valuation_basis: "provider_estimate" })), "Broker-provided history · estimate");
  assert.equal(wealthPointEvidence(point(100, { valuation_basis: "mixed", reconstructed_accounts: 2 })), "Mixed sources · 2 accounts reconstructed daily");
  assert.equal(wealthPointEvidence(point(100, { valuation_basis: "mixed", reconstructed_accounts: 2, estimated: true })), "Mixed sources · 2 accounts reconstructed daily · estimate");
  assert.equal(wealthPointEvidence(point(100)), "Recorded balance");
  assert.equal(wealthPointEvidence(point(null, { known_accounts: 0 })), "No supported valuation");
  assert.equal(wealthPointEvidence(point(100, { complete: false, missing_accounts: 1 })), "Incomplete account coverage");
});

test("the comparable chart excludes a current-only account without changing the current total", () => {
  const data = { summary: { net_worth: 550000, selected_accounts: 5 }, points: [point(400000), point(550000)], history: { mode: "comparable_cohort", account_ids: ["a", "b"], excluded_account_ids: ["new"], account_count: 2, selected_accounts: 5, current_value: 410000, points: [point(400000), point(410000)] } };
  const chart = wealthChartHistory(data);
  assert.equal(data.summary.net_worth, 550000);
  assert.equal(chart.current_value, 410000);
  assert.deepEqual(chart.points.map((p) => p.net_worth), [400000, 410000]);
  assert.equal(chart.comparable, true);
  data.history.points = [];
  assert.deepEqual(wealthChartHistory(data).points, [], "Missing comparable history never falls back to misleading changing-coverage points");
});

test("net-assets chart excludes earlier partial history and keeps only complete totals", () => {
  const oldPoints = [point(100, { date: "2026-09-16", coverage_key: "broker" }), point(110, { date: "2026-09-17", coverage_key: "broker" }), point(120, { date: "2026-09-18", coverage_key: "broker" })];
  const netPoints = [point(420, { date: "2026-09-18", known_accounts: 2, coverage_key: "broker-and-bank" }), point(430, { date: "2026-09-19", known_accounts: 2, coverage_key: "broker-and-bank" })];
  const data = { summary: { net_worth: 430, selected_accounts: 2 }, selected_account_ids: ["broker", "bank"], points: [], history: {
    account_ids: ["broker"], excluded_account_ids: ["bank"], account_count: 1, selected_accounts: 2,
    points: oldPoints, net_points: netPoints, net_start_date: "2026-09-18", end_value: 120,
    change: 20, change_pct: 20, change_comparable: true,
  } };
  const chart = wealthNetAssetsHistory(data);
  assert.deepEqual(chart.points.map(p => p.net_worth), [420, 430]);
  assert.deepEqual(chart.points.map(p => p.missing_accounts), [0, 0]);
  assert.equal(chart.end_value, data.summary.net_worth);
  assert.equal(chart.change_comparable, false);
  assert.equal(chart.change, null);
  assert.deepEqual(wealthHistorySegments(chart.points).map(segment => segment.values), [[420, 430]]);
  assert.deepEqual(wealthHistoryConnectors(chart.points), []);
  data.history.net_points = netPoints.map(p => ({ ...p, valuation_key: 'same-full-net-basis' }));
  assert.equal(wealthNetAssetsHistory(data).change, 10, "The full-net segment can report its own change");
});

test("incomplete cohort observations stay gaps, even when a partial balance is supplied", () => {
  assert.equal(chartPointValue(point(100, { complete: false })), null);
  assert.deepEqual(wealthHistorySegments([point(100), point(110, { complete: false }), point(120)]).map((row) => row.values), [[100, null, null], [null, null, 120]]);
});

test("account breakdown keeps unknown amounts distinct from zero and filters by signed balance", () => {
  const accounts = [{ id: "zero", name: "Everyday", institution: "Demo", currency: "GBP", value: 0 }, { id: "debt", name: "Card", institution: "Demo", currency: "USD", value: -200 }, { id: "assets", name: "Investing", institution: "Broker", currency: "GBP", value: 100 }, { id: "unknown", name: "Unknown", institution: "Demo", currency: "GBP", value: null }];
  assert.deepEqual(filterWealthAccounts(accounts, "", "all").map((a) => a.id), ["debt", "assets", "zero", "unknown"]);
  assert.deepEqual(filterWealthAccounts(accounts, "", "assets").map((a) => a.id), ["assets", "zero"]);
  assert.deepEqual(filterWealthAccounts(accounts, "demo", "debt").map((a) => a.id), ["debt"]);
  assert.deepEqual(filterWealthAccounts(accounts, "", "unknown").map((a) => a.id), ["unknown"]);
});

test("keyboard chart dates clamp to evidence and preserve valid narrow windows", () => {
  const dates = [point(1, { date: "2026-01-01" }), point(2, { date: "2026-04-01" })];
  assert.deepEqual(clampWealthDates(dates, "2025-01-01", "2027-01-01"), { start: "2026-01-01", end: "2026-04-01" });
  assert.deepEqual(clampWealthDates(dates, "2026-02-01", "2026-03-01"), { start: "2026-02-01", end: "2026-03-01" });
  assert.deepEqual(clampWealthDates(dates, "2026-03-01", "2026-02-01"), { start: "2026-02-01", end: "2026-03-01" });
  assert.deepEqual(clampWealthDates([], "", ""), { start: "", end: "" });
});


test("confirmed available credit is a fetched calculation without an estimate or review flag", () => {
  const card = { source: "lunchflow", as_of: "2026-09-18", estimated: false, valuation: { basis: "available_credit", estimated: false }, balance_check: { status: "available_credit", requires_review: false } };
  assert.equal(wealthValuationLabel(card), "Credit limit − available credit");
  assert.match(wealthSourceDate(card), /^Fetched /);
  assert.equal(wealthBalanceNeedsReview(card), false);
});

test("wealth axis labels keep small changes distinguishable when zoomed", () => {
  assert.deepEqual([1000, 1100, 1200, 1300, 1400].map(value => compactWealth(value, 50)), ['1k', '1.1k', '1.2k', '1.3k', '1.4k']);
  assert.deepEqual([1_000_000, 1_001_000, 1_002_000].map(value => compactWealth(value, 250)), ['1m', '1.001m', '1.002m']);
  assert.deepEqual([-.5, 0, .5].map(value => compactWealth(value, .125)), ['-0.5', '0', '0.5']);
});

test("zooming a multi-year history to one week displays individual dates", () => {
  assert.equal(wealthHistoryDateLabel(Date.parse('2026-09-12'), '2025-09-18', '2026-09-18'), 'Sept 26');
  assert.equal(wealthHistoryDateLabel(Date.parse('2026-09-12'), '2026-09-12', '2026-09-18'), '12 Sept');
  assert.equal(wealthHistoryDateLabel(Date.parse('2026-09-13'), '2026-09-12', '2026-09-18'), '13 Sept');
  assert.equal(wealthHistoryDateLabel(Date.parse('2025-12-31'), '2025-12-30', '2026-01-03'), '31 Dec 25');
});


test("visual history links join only existing endpoints within the same account coverage", () => {
  const points = [
    point(100, { date: "2026-09-01", coverage_key: "a", valuation_key: "daily" }),
    point(null, { date: "2026-09-02", coverage_key: "", complete: false }),
    point(105, { date: "2026-09-03", coverage_key: "a", valuation_key: "statement" }),
    point(110, { date: "2026-09-04", coverage_key: "a", valuation_key: "daily" }),
    point(200, { date: "2026-09-05", coverage_key: "b", valuation_key: "daily" }),
    point(null, { date: "2026-09-06", complete: false }),
    point(0, { date: "2026-09-07", coverage_key: "a", valuation_key: "daily" }),
    point(10, { date: "2026-09-08", coverage_key: "a", valuation_key: "daily", source_break: true }),
    point(12, { date: "2026-09-09", coverage_key: "a", valuation_key: "daily" }),
  ];
  const copy = structuredClone(points), originalSegments = wealthHistorySegments(points);
  assert.deepEqual(wealthHistoryConnectors(points), [{ from: 0, to: 2 }, { from: 2, to: 3 }, { from: 6, to: 7 }]);
  const links = wealthHistoryConnectorSeries(points, "blue");
  assert.deepEqual(links.map(link => link.data.map(([, value]) => value)), [[100, 105], [105, 110], [0, 10]]);
  for (const link of links) {
    assert.equal(link.data.length, 2, "Only real endpoints are drawn; missing dates receive no values");
    assert.equal(link.silent, true);
    assert.equal(link.tooltip.show, false);
    assert.equal(link.showSymbol, false);
    assert.equal(link.preserveLinePattern, true);
    assert.equal(link.lineStyle.type, "dashed");
  }
  assert.deepEqual(points, copy);
  assert.deepEqual(wealthHistorySegments(points), originalSegments, "Financial source and coverage breaks are preserved");
});

test("visual links cannot cross invalid dates or infer account identity from equal account counts", () => {
  assert.deepEqual(wealthHistoryConnectors([
    point(1, { date: "2026-09-01" }), point(null, { date: "2026-09-02" }), point(2, { date: "2026-09-03" }),
  ]), []);
  assert.deepEqual(wealthHistoryConnectors([
    point(1, { date: "2026-09-01", coverage_key: "a" }),
    point(null, { date: "2026-02-30", coverage_key: "a" }),
    point(2, { date: "2026-09-03", coverage_key: "a" }),
  ]), []);
});

test("the navigator receives every dated segment and preserves null gaps without adding plot values", () => {
  const points = [
    point(100, { date: "2026-09-01", coverage_key: "a", valuation_key: "daily" }),
    point(null, { date: "2026-09-02", complete: false }),
    point(105, { date: "2026-09-03", coverage_key: "a", valuation_key: "statement" }),
    point(110, { date: "2026-09-04", coverage_key: "a", valuation_key: "daily" }),
  ];
  const navigator = wealthHistoryNavigator(points);
  assert.deepEqual(navigator.data, points.map(point => [Date.parse(point.date), point.net_worth]));
  assert.equal(navigator.lineStyle.opacity, 0, "The navigator source does not draw an unsegmented plot line");
  assert.equal(navigator.silent, true);
  assert.equal(navigator.tooltip.show, false);
  assert.equal(navigator.showSymbol, false);
  assert.equal(navigator.connectNulls, false);
});
