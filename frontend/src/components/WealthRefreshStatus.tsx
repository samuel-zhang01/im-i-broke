import { useEffect, useState } from "react";
import type { WealthRefreshOutcome, WealthRefreshSource } from "../wealthRefresh";
import { WEALTH_REFRESH_LABELS, WEALTH_REFRESH_SOURCES } from "../wealthRefresh";

export type WealthRefreshRow = {
  status: WealthRefreshOutcome["status"] | "waiting" | "running" | "idle";
  message?: string;
  startedAt?: number;
};
export type WealthRefreshRows = Record<WealthRefreshSource, WealthRefreshRow>;
export const initialWealthRefreshRows = (): WealthRefreshRows => ({ banks: { status: "idle" }, investments: { status: "idle" }, rates: { status: "idle" } });

export default function WealthRefreshStatus({ rows, busy, onRetry }: {
  rows: WealthRefreshRows; busy: boolean; onRetry: (source: WealthRefreshSource) => void;
}) {
  const [now, setNow] = useState(Date.now);
  const until = Math.max(0, ...WEALTH_REFRESH_SOURCES.filter(source => rows[source].status === "failed").map(source => (rows[source].startedAt ?? 0) + 60_000));
  useEffect(() => {
    setNow(Date.now());
    if (until <= Date.now()) return;
    const timer = setInterval(() => { setNow(Date.now()); if (Date.now() >= until) clearInterval(timer); }, 1000);
    return () => clearInterval(timer);
  }, [until]);
  if (WEALTH_REFRESH_SOURCES.every(source => rows[source].status === "idle")) return null;
  const text = { idle: "Not requested", waiting: "Waiting for accounts", running: "Updating…", updated: "Updated", failed: "Update incomplete", unavailable: "Not connected" };
  return <section aria-label="Refresh status" className="rounded-lg border border-line bg-white px-3 py-2 text-xs">
    <ul className="grid gap-1 sm:grid-cols-3">
      {WEALTH_REFRESH_SOURCES.map(source => {
        const row = rows[source], remaining = Math.max(0, Math.ceil(((row.startedAt ?? 0) + 60_000 - now) / 1000));
        return <li key={source} className="flex min-h-11 flex-wrap items-center gap-x-2 gap-y-1">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${row.status === "failed" ? "bg-amber" : row.status === "updated" ? "bg-kelp" : "bg-mist/50"}`} aria-hidden="true" />
          <span role="status">{WEALTH_REFRESH_LABELS[source]} <span className="text-mist">· {text[row.status]}</span></span>
          {row.status === "failed" && <button className="btn btn-quiet text-xs" disabled={busy || remaining > 0} aria-label={`Retry ${WEALTH_REFRESH_LABELS[source].toLowerCase()}`} onClick={() => onRetry(source)}>{remaining > 0 ? `Retry in ${remaining}s` : "Retry"}</button>}
        </li>;
      })}
    </ul>
    {WEALTH_REFRESH_SOURCES.some(source => rows[source].message) && <details className="mt-1 border-t border-line pt-1 text-mist"><summary className="min-h-11 cursor-pointer py-2">Update details</summary><ul className="space-y-2 pb-2">{WEALTH_REFRESH_SOURCES.filter(source => rows[source].message).map(source => <li key={source}><span className="font-medium">{WEALTH_REFRESH_LABELS[source]}:</span> {rows[source].message}</li>)}</ul></details>}
  </section>;
}
