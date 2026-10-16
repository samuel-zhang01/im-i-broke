import { onCLS, onINP, onLCP, type Metric } from "web-vitals";

export const PERFORMANCE_METRICS = ["LCP", "INP", "CLS"] as const;
export type PerformanceMetricName = typeof PERFORMANCE_METRICS[number];
type MetricRating = "good" | "needs-improvement" | "poor";
export type PerformanceReading = Readonly<{
  name: PerformanceMetricName;
  supported: boolean;
  value: number | null;
  rating: MetricRating | null;
}>;

export function performanceSupport(entryTypes: readonly string[], interactionIds: boolean) {
  return {
    LCP: entryTypes.includes("largest-contentful-paint"),
    INP: entryTypes.includes("event") && interactionIds,
    CLS: entryTypes.includes("layout-shift"),
  };
}

/** Retain only three scalar readings, never performance entries, elements or URLs. */
export function createPerformanceStore(support: Record<PerformanceMetricName, boolean>) {
  let snapshot: readonly PerformanceReading[] = Object.freeze(PERFORMANCE_METRICS.map(name => Object.freeze({
    name, supported: support[name], value: null, rating: null,
  })));
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    record: ({ name, value, rating }: Pick<Metric, "name" | "value" | "rating">) => {
      const previous = snapshot.find(reading => reading.name === name);
      if (!previous?.supported || !Number.isFinite(value) || value < 0
        || !["good", "needs-improvement", "poor"].includes(rating)
        || (previous.value === value && previous.rating === rating)) return;
      snapshot = Object.freeze(snapshot.map(reading => reading.name === name
        ? Object.freeze({ name: reading.name, supported: true, value, rating }) : reading));
      listeners.forEach(listener => listener());
    },
  };
}

const entryTypes = typeof PerformanceObserver === "undefined" ? [] : (PerformanceObserver.supportedEntryTypes ?? []);
const timing = globalThis as typeof globalThis & { PerformanceEventTiming?: { prototype: object } };
const support = performanceSupport(entryTypes, Boolean(timing.PerformanceEventTiming && "interactionId" in timing.PerformanceEventTiming.prototype));
export const performanceDiagnostics = createPerformanceStore(support);
let started = false;

/** Once per document, outside React lifecycle; no network or persistent storage. */
export function startPerformanceDiagnostics() {
  if (started) return;
  started = true;
  const options = { reportAllChanges: true };
  if (support.LCP) onLCP(performanceDiagnostics.record, options);
  if (support.INP) onINP(performanceDiagnostics.record, options);
  if (support.CLS) onCLS(performanceDiagnostics.record, options);
}
