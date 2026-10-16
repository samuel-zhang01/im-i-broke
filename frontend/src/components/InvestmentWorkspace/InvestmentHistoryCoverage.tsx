import { isKnown, nativeMoney, portfolioDate } from "../../connectedPortfolio";
import type { WealthAccount, WealthData, WealthHistoryAccountCoverage } from "../../wealthApi";

type Props = {
  data: WealthData;
  onInspectSource?: (sourceId: string, name: string) => void;
  className?: string;
};
type CoverageRow = {
  id: string; name: string; account?: WealthAccount; coverage?: WealthHistoryAccountCoverage;
  included: boolean; currentValue: number | null; conflicts: number; discontinuities: number;
};
const reasons: Record<WealthHistoryAccountCoverage["reason"], string> = {
  included: "Included in history",
  zero_balance: "Zero current balance · excluded",
  current_value_unknown: "Current value unknown · excluded",
  insufficient_history: "Not enough dated history · excluded",
  shorter_shared_history: "Shorter shared history · excluded",
};
const sources: Record<NonNullable<WealthHistoryAccountCoverage["history_valuation_basis"]>, string> = {
  daily_holdings: "Reconstructed holdings, market prices & dated FX · estimate",
  provider_estimate: "Broker-provided history · estimate",
  recorded_balance: "Recorded account balance",
  mixed: "Mixed historical sources",
};
const accountCount = (count: number) => `${count} account${count === 1 ? "" : "s"}`;

export default function InvestmentHistoryCoverage({ data, onInspectSource, className = "" }: Props) {
  const history = data.history;
  const coverageKnown = Array.isArray(history?.account_ids) || Array.isArray(history?.account_coverage);
  const accounts = new Map(data.accounts.map(account => [account.id, account]));
  const metadata = new Map((history?.account_coverage ?? []).map(row => [row.account_id, row]));
  const included = new Set(history?.account_ids ?? []);
  const ids = history?.account_coverage?.map(row => row.account_id) ?? data.selected_account_ids ?? [];
  const rows: CoverageRow[] = ids.map(id => {
    const account = accounts.get(id), coverage = metadata.get(id);
    return {
      id, account, coverage, name: account?.name || id.replace(/^connected:|^statement:/, ""),
      included: coverage?.included ?? included.has(id),
      currentValue: coverage ? coverage.current_value : account?.value ?? null,
      conflicts: coverage?.provider_conflict_dates ?? account?.history_provider_conflict?.dates ?? 0,
      discontinuities: coverage?.source_discontinuities ?? account?.history_discontinuities ?? 0,
    };
  });
  if (!rows.length) return null;
  const summary = history?.coverage_summary;
  const includedCount = summary?.included_accounts ?? rows.filter(row => row.included).length;
  const nonzeroIncluded = summary?.included_nonzero_accounts ?? rows.filter(row => row.included && isKnown(row.currentValue) && row.currentValue !== 0).length;
  const nonzeroExcluded = summary?.excluded_nonzero_accounts ?? rows.filter(row => !row.included && isKnown(row.currentValue) && row.currentValue !== 0).length;
  const zeroExcluded = summary?.excluded_zero_accounts ?? rows.filter(row => !row.included && row.currentValue === 0).length;
  const unknownExcluded = summary?.excluded_unknown_accounts ?? rows.filter(row => !row.included && !isKnown(row.currentValue)).length;
  const unresolved = rows.filter(row => row.discontinuities > 0);
  const alternatives = rows.flatMap(row => (row.account?.history_alternatives ?? []).map(source => ({ row, source })));
  return <section aria-label="Historical account coverage" className={`min-w-0 rounded-xl border border-line bg-surface2/40 p-3 text-xs ${className}`}>
    <p className="font-medium">{coverageKnown ? `${includedCount} / ${rows.length} selected accounts in this history` : "Historical account coverage unavailable"}</p>
    {coverageKnown && nonzeroIncluded > 0 && nonzeroExcluded === 0 && <p className="mt-1 leading-relaxed text-mist">All {accountCount(nonzeroIncluded)} with a known non-zero balance are included.</p>}
    {coverageKnown && (zeroExcluded > 0 || unknownExcluded > 0 || nonzeroExcluded > 0) && <p className="mt-1 leading-relaxed text-mist">
      {[zeroExcluded > 0 ? `${accountCount(zeroExcluded)} with a zero balance excluded` : null,
        unknownExcluded > 0 ? `${accountCount(unknownExcluded)} with an unknown balance excluded` : null,
        nonzeroExcluded > 0 ? `${accountCount(nonzeroExcluded)} with a non-zero balance excluded` : null].filter(Boolean).join(" · ")}
    </p>}
    {coverageKnown && unknownExcluded > 0 && <p className="mt-1 leading-relaxed text-mist">An unknown balance is not treated as zero.</p>}
    {unresolved.length > 0 && <div role="status" aria-label="Historical data quality" className="mt-3 space-y-2 rounded-lg border border-amber/30 bg-amber/5 p-3 text-amber">
      <p className="font-semibold">Historical values need review</p>
      {unresolved.map(row => <div key={row.id} className="space-y-1 leading-relaxed">
        {row.conflicts > 0 && <p>{row.name}: provider estimates conflict with reconstructed history on {row.conflicts} date{row.conflicts === 1 ? "" : "s"}. Conflicting provider estimates are excluded.</p>}
        {row.discontinuities > 0 && <p>{row.name}: {row.discontinuities} large history change{row.discontinuities === 1 ? " is" : "s are"} unresolved. Values are preserved with breaks in the chart; these changes may reflect cash flows or incomplete history, not investment returns.</p>}
      </div>)}
    </div>}
    {rows.filter(row => row.conflicts > 0).map(row => <p key={`resolved-${row.id}`} className="mt-2 leading-relaxed text-mist">{row.name}: statement-backed history is used. {row.conflicts} conflicting provider estimates were superseded.</p>)}
    {alternatives.map(({ row, source }) => <div key={`${row.id}:${source.source_id}`} className="mt-3 rounded-lg border border-seablue/25 bg-white p-3">
      <p className="font-medium">{row.name}</p>
      <p className="mt-1 leading-relaxed text-mist">Imported history available{source.end_date ? ` through ${portfolioDate(source.end_date)}` : ""}; account identity unconfirmed.</p>
      <p className="mt-1 leading-relaxed text-mist">{source.name} · {source.valued_days} dated values. This source is not combined with the connected account.</p>
      {onInspectSource && <button type="button" className="mt-2 min-h-11 rounded-lg border border-line bg-white px-3 text-xs font-medium" onClick={() => onInspectSource(source.source_id, source.name)}>View imported history<span className="sr-only"> for {row.name}</span></button>}
    </div>)}
    <details className="mt-2 border-t border-line">
      <summary className="min-h-11 cursor-pointer py-3 font-medium">Account contributions &amp; history sources</summary>
      <p className="mb-3 text-xs leading-relaxed text-mist">Historical contributions use each account’s value at the comparison’s final plotted date. Current values use the latest account snapshot; the dates and totals can differ.</p>
      <ul className="space-y-3" aria-label="Historical account contributions">
        {rows.map(row => <li key={row.id} className="min-w-0 rounded-lg border border-line bg-white p-3">
          <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><p className="break-words font-medium">{row.name}</p>{row.account?.institution && <p className="mt-0.5 text-xs text-mist">{row.account.institution}</p>}</div><p className={`text-xs ${row.included ? "text-seablue" : "text-mist"}`}>{row.coverage ? reasons[row.coverage.reason] : row.included ? "Included in history" : "Historical coverage unavailable"}</p></div>
          <dl className="mt-3 grid grid-cols-1 gap-3 min-[360px]:grid-cols-2">
            <div className="min-w-0"><dt className="text-xs text-mist">Historical contribution · {data.currency}</dt><dd className="mt-1 break-words font-semibold tabular-nums">{row.included || !coverageKnown ? nativeMoney(row.coverage?.history_end_value, data.currency) : "Not included"}</dd><dd className="mt-1 text-xs text-mist">{row.coverage?.history_end_date ? portfolioDate(row.coverage.history_end_date) : row.included || !coverageKnown ? "Dated contribution unavailable" : "Excluded from this comparison"}</dd></div>
            <div className="min-w-0"><dt className="text-xs text-mist">Current account value · {data.currency}</dt><dd className="mt-1 break-words font-semibold tabular-nums">{nativeMoney(row.currentValue, data.currency)}</dd><dd className="mt-1 text-xs text-mist">{row.coverage?.latest_observation_date || row.account?.as_of ? portfolioDate(row.coverage?.latest_observation_date ?? row.account?.as_of ?? null) : "Snapshot date unavailable"}</dd></div>
          </dl>
          <p className="mt-3 text-xs leading-relaxed text-mist">{row.coverage?.history_valuation_basis ? sources[row.coverage.history_valuation_basis] : row.included || !coverageKnown ? "Historical source details unavailable" : "No contribution to the historical line"}</p>
          {!row.included && row.coverage?.latest_observation_date && <p className="mt-1 text-xs text-mist">Latest available observation: {portfolioDate(row.coverage.latest_observation_date)}</p>}
        </li>)}
      </ul>
    </details>
  </section>;
}
