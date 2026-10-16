import { marketValueSegments, marketValueTimestamp } from "./marketValue";
import type { MarketValuePoint } from "./marketValueApi";
import { wealthHistorySegments } from "./wealth";
import type { WealthPoint } from "./wealthApi";

export type InvestmentValueSeries = {
  id: string;
  kind: "history" | "recent";
  label: string;
  partial: boolean;
  data: [timestamp: number, value: number | null][];
};

export type InvestmentValueWindow = { start?: number; end?: number; dailyAtEnd?: boolean };

export function historyTimestamp(date: string, observedAt?: string | null, end?: number, dailyAtEnd = false): number | null {
  const observed = marketValueTimestamp(observedAt);
  if (observed !== null && observedAt?.slice(0, 10) === date) return observed;
  // Daily account observations have a date, not an intraday valuation time.
  if (/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    const timestamp = Date.parse(`${date}T${dailyAtEnd ? "23:59:59.999" : "00:00:00"}Z`);
    return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === date ? (dailyAtEnd && end !== undefined && new Date(end).toISOString().slice(0, 10) === date ? Math.min(timestamp, end) : timestamp) : null;
  }
  return marketValueTimestamp(date);
}

/** Share a UTC axis without resampling either source or joining coverage gaps. */
export function investmentValueChartData(
  historyPoints: WealthPoint[],
  recentPoints: MarketValuePoint[],
  window: InvestmentValueWindow = {},
): { series: InvestmentValueSeries[]; timestamps: number[]; start: number | null; end: number | null } {
  let start = Number.isFinite(window.start) ? window.start! : null;
  let end = Number.isFinite(window.end) ? window.end! : null;
  if (start !== null && end !== null && start > end) [start, end] = [end, start];
  const inWindow = (timestamp: number | null): timestamp is number => timestamp !== null
    && (start === null || timestamp >= start) && (end === null || timestamp <= end);
  const historyTimes = historyPoints.map(point => historyTimestamp(point.date, point.observed_at, end ?? undefined, window.dailyAtEnd));
  const recentTimes = recentPoints.map(point => marketValueTimestamp(point.timestamp));
  const timestamps = [...new Set([...historyTimes, ...recentTimes].filter(inWindow))].sort((a, b) => a - b);

  // Segment before clipping so a narrower window cannot remove a source break.
  // An unplaceable date is still a break, even though it cannot appear on the axis.
  const history = wealthHistorySegments(historyPoints.map((point, index) => historyTimes[index] === null
    ? { ...point, net_worth: null } : point));
  const recent = marketValueSegments(recentPoints);
  const series: InvestmentValueSeries[] = [];
  const addSeries = (id: string, kind: InvestmentValueSeries["kind"], partial: boolean, times: (number | null)[], values: (number | null)[]) => {
    const data: InvestmentValueSeries["data"] = [];
    times.forEach((timestamp, index) => {
      if (inWindow(timestamp)) data.push([timestamp, values[index]]);
    });
    if (!data.some(([, value]) => value !== null)) return;
    data.sort(([a], [b]) => a - b);
    series.push({ id, kind, label: kind === "history" ? "Asset history" : "Recent prices · estimate", partial, data });
  };
  history.forEach((segment, index) => addSeries(`history-${index}`, "history", segment.partial, historyTimes, segment.values));
  recent.forEach((values, index) => {
    const partial = recentPoints.some((point, pointIndex) => values[pointIndex] !== null
      && (point.missing_accounts > 0 || point.priced_holdings < point.holdings_count));
    addSeries(`recent-${index}`, "recent", partial, recentTimes, values);
  });

  return { series, timestamps, start: start ?? timestamps[0] ?? null, end: end ?? timestamps[timestamps.length - 1] ?? null };
}

/** Index both lines only when their starting values were observed at exactly the same time. */
export function indexInvestmentValueChart(model: ReturnType<typeof investmentValueChartData>): {
  model: ReturnType<typeof investmentValueChartData>; baseline: number | null;
} {
  const observations = new Map<number, { history?: number; recent?: number }>();
  for (const series of model.series) for (const [timestamp, value] of series.data) {
    if (value === null || !Number.isFinite(value)) continue;
    const observation = observations.get(timestamp) ?? {};
    observation[series.kind] = value;
    observations.set(timestamp, observation);
  }
  const baseline = [...observations.keys()].sort((a, b) => a - b).find(timestamp => {
    const point = observations.get(timestamp)!;
    return point.history !== undefined && point.history > 0 && point.recent !== undefined && point.recent > 0;
  });
  if (baseline === undefined) return { model, baseline: null };

  const bases = observations.get(baseline)!;
  const series = model.series.map(segment => ({
    ...segment,
    data: segment.data.filter(([timestamp]) => timestamp >= baseline).map(([timestamp, value]): [number, number | null] => [
      timestamp, value === null ? null : value / bases[segment.kind]! * 100,
    ]),
  })).filter(segment => segment.data.some(([, value]) => value !== null));
  return { baseline, model: { ...model, series, timestamps: model.timestamps.filter(timestamp => timestamp >= baseline), start: baseline } };
}
