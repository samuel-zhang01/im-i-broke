import { money } from "./format";

export function finiteChartValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function signedBarPoint(value: unknown, color: string, horizontal = false) {
  const amount = finiteChartValue(value);
  return { value: amount, itemStyle: { color, borderRadius: horizontal
    ? amount !== null && amount < 0 ? [4, 0, 0, 4] : [0, 4, 4, 0]
    : amount !== null && amount < 0 ? [0, 0, 4, 4] : [4, 4, 0, 0] } };
}

export type BalancePoint = { value: number | null; savedPeriod: string | null; carried: boolean };
export function balanceSeriesData(periods: string[], rows: { period: string; balance: number | null }[]): BalancePoint[] {
  const observations = new Map(rows.map(row => [row.period, finiteChartValue(row.balance)]));
  let value: number | null = null, savedPeriod: string | null = null;
  return periods.map(period => {
    if (observations.has(period)) {
      value = observations.get(period)!;
      savedPeriod = value === null ? null : period;
    }
    return { value, savedPeriod, carried: savedPeriod !== null && savedPeriod !== period };
  });
}

export function escapeChartText(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]!));
}

type BalanceTooltipRow = { axisValue?: string; seriesName?: string; data?: BalancePoint };
export function balanceTooltip(input: BalanceTooltipRow | BalanceTooltipRow[]): string {
  const rows = Array.isArray(input) ? input : [input];
  return `<strong>${escapeChartText(rows[0]?.axisValue)}</strong>` + rows.map(row => {
    const point = row.data;
    const provenance = point?.savedPeriod
      ? point.carried ? `Carried from ${point.savedPeriod}` : "Saved balance point"
      : "No supported balance";
    return `<div style="margin-top:8px"><span>${escapeChartText(row.seriesName)}</span>` +
      `<strong style="margin-left:12px">${money(point?.value)}</strong>` +
      `<div style="font-size:11px">${escapeChartText(provenance)}</div></div>`;
  }).join("");
}

export function chartPeriodLabel(value: string): string {
  if (!/^\d{4}-\d{2}(-\d{2})?$/.test(value)) return value;
  const date = new Date(`${value.length === 7 ? `${value}-01` : value}T00:00:00Z`);
  if (!Number.isFinite(date.getTime())) return value;
  return date.toLocaleDateString("en-GB", value.length === 7
    ? { month: "short", year: "2-digit", timeZone: "UTC" }
    : { day: "numeric", month: "short", timeZone: "UTC" });
}

/** Keep close balance bounds distinguishable without making wide ranges verbose. */
export function bankBalanceTickLabels(low: number, high: number): { low: string; high: string } {
  for (let precision = 1; precision <= 3; precision++) {
    const format = new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: precision });
    const labels = { low: format.format(low), high: format.format(high) };
    if (low === high || labels.low !== labels.high) return labels;
  }
  // Very small movements near a large balance need ordinary numbers; more
  // compact decimal places would hide the financial meaning behind a suffix.
  const precision = Math.min(8, Math.max(2, 1 - Math.floor(Math.log10(Math.abs(high - low)))));
  const format = new Intl.NumberFormat("en-GB", { maximumFractionDigits: precision });
  const labels = { low: format.format(low), high: format.format(high) };
  return labels.low === labels.high ? { low: String(low), high: String(high) } : labels;
}

export function bankBalanceGeometry(rows: { amount: number; currency: string; fetched_at: string }[], currency: string) {
  const byTime = new Map<number, { amount: number; currency: string; fetched_at: string; timestamp: number }>();
  const conflicts = new Set<number>();
  for (const row of rows) {
    if (row.currency !== currency || finiteChartValue(row.amount) === null) continue;
    const stamp = row.fetched_at.replace(" ", "T");
    if (!/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(stamp)) continue;
    const calendar = Date.parse(`${stamp.slice(0, 10)}T00:00:00Z`);
    if (!Number.isFinite(calendar) || new Date(calendar).toISOString().slice(0, 10) !== stamp.slice(0, 10)) continue;
    const timestamp = Date.parse(stamp.length === 10 ? `${stamp}T00:00:00Z` : /(?:Z|[+-]\d{2}:?\d{2})$/.test(stamp) ? stamp : `${stamp}Z`);
    if (!Number.isFinite(timestamp)) continue;
    if (byTime.has(timestamp) && byTime.get(timestamp)!.amount !== row.amount) conflicts.add(timestamp);
    byTime.set(timestamp, { ...row, timestamp });
  }
  const points = [...byTime.values()].filter(point => !conflicts.has(point.timestamp)).sort((a, b) => a.timestamp - b.timestamp);
  if (!points.length) return { points: [], minimum: null, maximum: null, low: null, high: null, zeroY: null };
  const minimum = Math.min(...points.map(point => point.amount)), maximum = Math.max(...points.map(point => point.amount));
  const padding = (maximum - minimum) * .08 || Math.max(Math.abs(minimum) * .02, 1);
  const low = minimum - padding, high = maximum + padding;
  const first = points[0].timestamp, span = points[points.length - 1].timestamp - first;
  return { points: points.map(point => ({ ...point,
    x: span ? (point.timestamp - first) / span : .5,
    y: (high - point.amount) / (high - low),
  })), minimum, maximum, low, high, zeroY: low <= 0 && high >= 0 ? high / (high - low) : null };
}
