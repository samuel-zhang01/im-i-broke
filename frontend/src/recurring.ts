import type { RecurringOverview, RecurringSeries } from "./api";

export function isForecastEligible(series: RecurringSeries) {
  return series.forecast_eligible !== false && !series.data_stale && series.status !== "ended" && (!series.pattern_type || series.pattern_type === "regular_payment");
}

export function currentRecurringTotals(patterns: RecurringSeries[]) {
  const current = patterns.filter(isForecastEligible);
  return {
    monthly: current.reduce((sum, r) => sum + r.monthly_cost, 0),
    current: current.length,
    priceChanges: current.filter(r => r.status === "price_change").length,
  };
}

/** Historical evidence has a rate even when it no longer supports a forecast. */
export function recurringMonthlyAmount(series: RecurringSeries) {
  return isForecastEligible(series) ? series.monthly_cost : series.historical_monthly_cost ?? series.monthly_cost;
}

export function recurringForPayment(data: RecurringOverview, id: number) {
  return [...data.patterns, ...data.candidates].find(r => r.txn_ids.includes(id));
}

const eventName = "ledger-open-recurring";
export function openRecurring(id: number) {
  window.dispatchEvent(new CustomEvent(eventName, { detail: id }));
}
export function watchRecurringNavigation(open: (id: number) => void) {
  const listener = (event: Event) => {
    const id = (event as CustomEvent<unknown>).detail;
    if (typeof id === "number" && Number.isSafeInteger(id) && id > 0) open(id);
  };
  window.addEventListener(eventName, listener);
  return () => window.removeEventListener(eventName, listener);
}

function dateTimestamp(date: string | null | undefined) {
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return NaN;
  const timestamp = Date.parse(`${date}T12:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === date ? timestamp : NaN;
}

/** Empirical timing bounds, with the original central estimate as a fallback. */
export function recurringDateWindow(series: RecurringSeries) {
  const next = dateTimestamp(series.next_expected);
  if (!Number.isFinite(next)) return null;
  const earliest = dateTimestamp(series.next_expected_earliest);
  const latest = dateTimestamp(series.next_expected_latest);
  if (earliest <= next && latest >= next) {
    return { earliest: series.next_expected_earliest!, latest: series.next_expected_latest! };
  }
  return { earliest: series.next_expected!, latest: series.next_expected! };
}

export function recurringWaitingForPayment(series: RecurringSeries, today: string) {
  const window = recurringDateWindow(series);
  return isForecastEligible(series) && !!window && Number.isFinite(dateTimestamp(today))
    && series.next_expected! < today && window.latest >= today;
}

/** Retain a supported timing window until it has passed; never roll it forward. */
export function upcomingRecurring(patterns: RecurringSeries[], today: string) {
  const start = dateTimestamp(today);
  if (!Number.isFinite(start)) return [];
  const end = start + 30 * 24 * 60 * 60 * 1000;
  return patterns.filter(series => {
    if (!isForecastEligible(series)) return false;
    const window = recurringDateWindow(series);
    return !!window && dateTimestamp(window.latest) >= start && dateTimestamp(window.earliest) <= end;
  }).sort((a, b) => a.next_expected!.localeCompare(b.next_expected!) || a.merchant.localeCompare(b.merchant));
}
