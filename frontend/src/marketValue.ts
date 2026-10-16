import type { MarketValuePoint, MarketValueRange } from "./marketValueApi";

export function marketValueTimestamp(value: string | null | undefined): number | null {
  if (!value || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export function marketValueDate(value: string | null | undefined): string {
  const timestamp = marketValueTimestamp(value);
  return timestamp === null ? "Unknown time" : `${new Intl.DateTimeFormat("en-GB", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC",
  }).format(timestamp)} UTC`;
}

export function marketAxisLabel(timestamp: number, range: MarketValueRange): string {
  if (!Number.isFinite(timestamp)) return "";
  return new Intl.DateTimeFormat("en-GB", range === "1d"
    ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC" }
    : { day: "numeric", month: "short", timeZone: "UTC" }).format(timestamp);
}

export function marketPointValue(point: MarketValuePoint): number | null {
  return marketValueTimestamp(point.timestamp) !== null && typeof point.value === "number" && Number.isFinite(point.value) ? point.value : null;
}

export function marketValueSegments(points: MarketValuePoint[]): (number | null)[][] {
  const result: (number | null)[][] = [];
  let previous: MarketValuePoint | null = null;
  let active: (number | null)[] | null = null;
  points.forEach((point, index) => {
    const value = marketPointValue(point);
    if (value === null) { previous = null; active = null; return; }
    if (!active || previous?.coverage_key !== point.coverage_key) {
      active = Array(points.length).fill(null) as (number | null)[];
      result.push(active);
    }
    active[index] = value;
    previous = point;
  });
  return result;
}
