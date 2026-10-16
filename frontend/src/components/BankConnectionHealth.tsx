import { DataTable } from "./DataTable";
import { fetchBankingHealth } from "../bankingApi";
import { bankTimestamp } from "../banking";
import { Async, useAsync } from "./ui";

export default function BankConnectionHealth() {
  const health = useAsync(signal => fetchBankingHealth(signal));
  return <Async state={health}>{data => {
    const issues = Object.entries(data.integrity_issues || {}).filter(([, count]) => count > 0);
    const connections = data.connections || {};
    return <div className="space-y-4 pt-3 text-xs text-mist">
      <p className="leading-relaxed">{data.freshness_basis}</p>
      {!data.schema_ready ? <p>The connection store is not ready. Existing statement records remain available.</p> : <>
        <dl className="grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3">{[
          ["Saved bank records", data.cache?.total], ["Possible overlaps", data.review_queue?.awaiting_review], ["Pending payments", data.review_queue?.pending_authorizations],
          ["Native-currency records", data.review_queue?.native_currency_records], ["Connections without a balance", connections.no_balance], ["Failed connections", connections.failed], ["Failed saved feeds", connections.failed_provider_connections ?? 0], ["Missing connections", connections.missing],
        ].map(([label, count]) => <div key={String(label)}><dt>{label}</dt><dd className="mt-1 text-lg font-semibold tabular-nums text-foam">{typeof count === "number" ? count.toLocaleString() : "—"}</dd></div>)}</dl>
        <div className="space-y-2 border-t border-line pt-3"><p>Latest successful fetch: <span className="font-medium text-foam">{bankTimestamp(data.freshness?.last_success_at)}</span></p><p>Automatic refresh: {data.scheduler?.running ? "running now" : data.scheduler?.enabled ? `every ${Math.round(data.scheduler.interval_seconds / 60)} minutes while the server runs` : "not scheduled"}</p>{data.freshness?.last_attempt_failed && <p className="text-amber">The last fetch attempt failed. Saved records are still available.</p>}{data.scheduler?.expired_lease && <p className="text-amber">An interrupted refresh has expired. The next scheduled attempt can recover it.</p>}<p className={issues.length ? "text-amber" : "text-kelp"}>{issues.length ? `${issues.length} local history checks need inspection.` : "Local history link checks passed."}</p></div>
        {issues.length > 0 && <details><summary className="min-h-11 cursor-pointer">History check details</summary><ul className="mt-2 space-y-1">{issues.map(([key, count]) => <li key={key}>{key.replace(/_/g, " ")}: {count}</li>)}</ul></details>}
      </>}
      <details className="border-t border-line pt-2"><summary className="min-h-11 cursor-pointer">Local response times</summary><p className="mt-1 leading-relaxed">{data.timings.scope} Includes reads and edits. Percentiles use up to 128 recent server responses for each operation.</p><div className="mt-3 overflow-x-auto"><DataTable className="w-full text-left"><thead><tr><th className="pb-2 font-medium">Operation</th><th className="pb-2 text-right font-medium">Typical</th><th className="pb-2 text-right font-medium">95th percentile</th></tr></thead><tbody>{Object.entries(data.timings.operations).map(([name, timing]) => <tr key={name} className="border-t border-line"><td className="py-2">{name.replace(/_/g, " ")}</td><td className="py-2 text-right tabular-nums">{Math.round(timing.recent_p50_ms)} ms</td><td className="py-2 text-right tabular-nums">{Math.round(timing.recent_p95_ms)} ms</td></tr>)}</tbody></DataTable></div></details>
    </div>;
  }}</Async>;
}
