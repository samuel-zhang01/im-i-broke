import { useCallback, useEffect, useState } from "react";
import { fetchMarketValue, type MarketValueData, type MarketValueInterval, type MarketValueRange } from "../../marketValueApi";

interface MarketValueOptions {
  accountIds: string[];
  currency: string;
  revision: string;
  range: MarketValueRange;
  interval: MarketValueInterval;
  enabled?: boolean;
}

export default function useMarketValue({ accountIds, currency, revision, range, interval, enabled = true }: MarketValueOptions) {
  const [result, setResult] = useState<{ key: string; data: MarketValueData } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [request, setRequest] = useState<{ key: string; loading: boolean } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([accountIds, currency, range, interval, revision]);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false, busy = false;
    let controller: AbortController | null = null;
    setFailure(null);
    setRequest({ key, loading: true });

    async function read() {
      if (disposed || busy || document.visibilityState !== "visible") return;
      busy = true;
      controller = new AbortController();
      try {
        const data = await fetchMarketValue(accountIds, currency, range, interval, controller.signal);
        if (!disposed) {
          setResult({ key, data });
          setFailure(null);
        }
      } catch (reason) {
        if (!disposed && !controller.signal.aborted) {
          setFailure({ key, message: reason instanceof Error ? reason.message : "Recent prices are unavailable." });
        }
      } finally {
        busy = false;
        if (!disposed) setRequest({ key, loading: false });
      }
    }

    void read();
    const timer = window.setInterval(() => { void read(); }, 60_000);
    const visible = () => { if (document.visibilityState === "visible") void read(); };
    document.addEventListener("visibilitychange", visible);
    return () => {
      disposed = true;
      controller?.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
    };
    // The serialized key covers all request filters without depending on array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, attempt, enabled]);

  // Keep observations during transient refresh failures, but never across scopes.
  return {
    data: enabled && result?.key === key ? result.data : null,
    error: enabled && failure?.key === key ? failure.message : null,
    loading: enabled && (request?.key === key ? request.loading : true),
    retry,
  };
}
