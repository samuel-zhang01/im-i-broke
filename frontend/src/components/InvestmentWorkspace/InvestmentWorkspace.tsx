import { Heading } from "../Heading";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { fetchInvestmentAnalysis, type InvestmentAnalysis, type InvestmentRange } from "../../investmentAnalysisApi";
import { gainTone, priceMovers, signedPercent } from "../../investmentAnalysis";
import { accountLabel, nativeMoney, portfolioDate } from "../../connectedPortfolio";
import { Card } from "../ui";
import { PageSkeleton } from "../Skeleton";
import AccountDirectory from "./AccountDirectory";
import SecurityDetail from "./SecurityDetail";
import PerformanceTable from "./PerformanceTable";
import SectorOverview from "./SectorOverview";
import AccountValueHistory from "./AccountValueHistory";
import type { ConnectedAccount } from "../../snaptradeApi";
import SecurityComparison from "./SecurityComparison";
import StockDetailDialog from "./StockDetailDialog";
import InvestmentInsights from "./InvestmentInsights";

export type InvestmentWorkspaceMode = "performance" | "accounts" | "sectors" | "compare" | "review";

export default function InvestmentWorkspace({ mode, accountIds, currency, range, revision, extraNotes, returnRates, onOpenAccount, onShowPerformance, onNavigate }: {
  mode: InvestmentWorkspaceMode; accountIds: string[]; currency: string; range: InvestmentRange; revision: string;
  extraNotes: ReactNode; onOpenAccount: (id: string) => void; onShowPerformance: () => void;
  returnRates?: ConnectedAccount["return_rates"];
  onNavigate: (view: "performance" | "review" | "calendar" | "sectors") => void;
}) {
  const [result, setResult] = useState<{ key: string; data: InvestmentAnalysis } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedHolding, setSelectedHolding] = useState<string | null>(null);
  const [sector, setSector] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [showRecent, setShowRecent] = useState(false);
  const notes = useRef<HTMLDetailsElement>(null);
  const accountKey = accountIds.join("|");
  useEffect(() => { setSector(null); setSelectedHolding(null); }, [accountKey]);
  const scopeKey = `${accountIds.join("|")}:${currency}:${range}`;
  useEffect(() => {
    const controller = new AbortController();
    let busy = false;
    async function read() {
      if (busy || controller.signal.aborted || document.visibilityState !== "visible") return;
      busy = true; setLoading(true); setError(null);
      try {
        const data = await fetchInvestmentAnalysis(accountIds, currency, range, controller.signal);
        if (!controller.signal.aborted) setResult({ key: scopeKey, data });
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Investment analysis is unavailable.");
      } finally { busy = false; if (!controller.signal.aborted) setLoading(false); }
    }
    void read();
    const timer = window.setInterval(() => { void read(); }, 60_000);
    const visible = () => { if (document.visibilityState === "visible") void read(); };
    document.addEventListener("visibilitychange", visible);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
    // Account IDs, currency and range are fully represented by the scope key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey, revision, retry]);
  const data = result?.key === scopeKey ? result.data : null;
  const spotlight = data?.holdings.find((holding) => holding.id === selectedHolding && !holding.is_cash) ?? null;
  function openNotes() { if (notes.current) { notes.current.open = true; notes.current.scrollIntoView({ behavior: "smooth", block: "start" }); } }
  function chooseHolding(id: string) { setSelectedHolding(id); }
  if (!data && loading) return <PageSkeleton variant="portfolio" label="Loading investment performance…" />;
  if (!data) return <Card><p role="alert" className="text-sm text-amber">{error || "No investment analysis is available."}</p><button className="btn mt-3" onClick={() => setRetry((value) => value + 1)}>Try again</button></Card>;
  const summary = data.summary;
  const coverageLabel = summary.missing_accounts > 0 ? `Partial · ${summary.known_accounts}/${summary.selected_accounts} account balances` : "Balance details";
  const missingSession = data.holdings.filter(holding => !holding.is_cash && holding.day_move_estimate === null);
  const missingCloseDates = [...new Set(missingSession.map(holding => holding.day_move_missing_date).filter(Boolean))].sort();
  const sessionReason = {
    unsupported_security: "Security type unsupported", missing_price_history: "Price history unavailable",
    stale_prices: "Prices need updating", stale_holdings: "Holdings need updating", missing_quantity: "Quantity unavailable",
    share_units_changed: "Share units need verification after a split", missing_previous_session_close: "Previous session close unavailable",
    missing_exchange_rate: "Exchange rate unavailable",
  };
  const sessionGroups = new Map<string, typeof missingSession>();
  for (const holding of missingSession) {
    const reason = holding.day_move_unavailable_reason ? sessionReason[holding.day_move_unavailable_reason] : "Previous close, quantity or exchange rate unavailable";
    sessionGroups.set(reason, [...(sessionGroups.get(reason) ?? []), holding]);
  }
  const missingBalances = data.accounts.filter(account => data.selected_account_ids.includes(account.id) && account.value === null);
  const brokerReturn = accountIds.length === 1 ? returnRates?.data.find((rate) => rate.timeframe === range.toUpperCase()) : undefined;
  return <div className="content-enter space-y-4">
    {missingSession.length > 0 && <p role="status" className="rounded-lg border border-amber/20 bg-amber/5 px-4 py-3 text-xs leading-relaxed text-amber">Session move is a subtotal for {summary.priced_holdings} of {summary.holdings_count} positions. Current account values are separate.{missingCloseDates.length > 0 && <> Previous closing prices are unavailable for {missingCloseDates.join(", ")}; refreshing may still return the same gap.</>} <button className="min-h-11 underline" onClick={openNotes}>View excluded positions</button></p>}
    {error && <p role="alert" className="text-xs text-amber">Couldn’t update this analysis. Saved figures remain visible. <button className="underline" onClick={() => setRetry((value) => value + 1)}>Retry</button></p>}
    <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4 rounded-xl border border-line bg-white px-4 py-3 sm:px-5 sm:py-4" aria-busy={loading}>
      <div><div className="flex flex-wrap items-center gap-3"><p className="eyebrow">{summary.missing_accounts ? "Known account value" : "Account value"} · {data.currency}</p><button className={`text-xs underline underline-offset-4 ${summary.missing_accounts ? "text-amber" : "text-mist"}`} onClick={openNotes}>{loading ? "Updating…" : coverageLabel}</button></div><p className="mt-2 break-words text-[32px] font-semibold leading-tight tracking-tight tabular-nums">{nativeMoney(summary.value, data.currency)}</p>{brokerReturn ? <p className={`mt-1 text-xs ${gainTone(brokerReturn.return_percent)}`}>{signedPercent(brokerReturn.return_percent)} broker return · {brokerReturn.timeframe}{returnRates?.stale ? " · older" : ""}</p> : <p className="mt-1 text-xs text-mist">{summary.known_accounts} / {summary.selected_accounts} accounts · includes cash</p>}</div>
      <dl className="grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 sm:gap-x-8"><div><dt className="text-xs text-mist">Open position P/L</dt><dd className={`mt-1 break-words text-base font-semibold tabular-nums sm:text-xl ${gainTone(summary.unrealized_pnl)}`}>{nativeMoney(summary.unrealized_pnl, data.currency)}</dd>{summary.pnl_holdings !== undefined && <dd className="mt-1 text-xs text-mist">{summary.pnl_holdings} / {summary.holdings_count} positions</dd>}</div><div><dt className="text-xs text-mist">Session move · estimate</dt><dd className={`mt-1 break-words text-base font-semibold tabular-nums sm:text-xl ${gainTone(summary.day_move_estimate)}`}>{nativeMoney(summary.day_move_estimate, data.currency)}</dd><dd><button type="button" className={`min-h-11 text-left text-xs underline underline-offset-4 ${summary.priced_holdings < summary.holdings_count ? "text-amber" : "text-mist"}`} onClick={openNotes} aria-label="Session move coverage">{summary.priced_holdings} / {summary.holdings_count} positions{summary.priced_holdings < summary.holdings_count ? " · partial" : ""}</button></dd></div></dl>
    </div>

    {mode === "accounts" ? <AccountDirectory accounts={data.accounts} currency={data.currency} onOpen={onOpenAccount} /> : mode === "sectors" ? <SectorOverview sectors={data.sectors} holdings={data.holdings} currency={data.currency} accountIds={accountIds} revision={revision} onChoose={(name) => { setSector(name); onShowPerformance(); }} /> : mode === "review" ? <InvestmentInsights holdings={data.holdings} accounts={data.accounts} accountIds={accountIds} currency={data.currency} revision={revision} onAction={(action) => { if (action.target === "account" && action.account_id) onOpenAccount(action.account_id); else if (action.target === "holding") { const holding = data.holdings.find((row) => row.account_id === action.account_id && row.position_id === action.position_id); if (holding) chooseHolding(holding.id); } else onNavigate(action.target === "calendar" ? "calendar" : "sectors"); }} /> : mode === "compare" ? <SecurityComparison holdings={data.holdings} currency={data.currency} range={range} revision={revision} /> : <>
      <AccountValueHistory accountIds={accountIds} currency={data.currency} range={range} revision={revision} showRecent={showRecent} onShowRecentChange={setShowRecent} />
      <PerformanceTable holdings={data.holdings} currency={data.currency} range={range} selectedId={spotlight?.id ?? null} sector={sector} onClearSector={() => setSector(null)} onChoose={chooseHolding} />
      <details className="rounded-xl border border-line bg-white px-4"><summary className="cursor-pointer py-3 text-xs font-medium">Latest session movers</summary><div className="grid gap-3 pb-4 md:grid-cols-2">{([['up', 'Latest session risers'], ['down', 'Latest session fallers']] as const).map(([direction, title]) => {
        const movers = priceMovers(data.holdings, "1d", direction);
        return <Card key={direction}><Heading level={2} variant="section">{title}</Heading>{!movers.length ? <p className="py-4 text-xs text-mist">No supported {direction === "up" ? "positive" : "negative"} price moves in this selection.</p> : <div className="mt-3 divide-y divide-line">{movers.map((holding) => <button className="flex w-full items-center justify-between gap-3 py-3 text-left" key={holding.id} onClick={() => chooseHolding(holding.id)}><span className="min-w-0"><span className="block text-sm font-semibold">{holding.symbol}</span><span className="block truncate text-xs text-mist">{holding.name}</span></span><span className={`shrink-0 text-sm font-semibold tabular-nums ${gainTone(holding.returns['1d'])}`}>{signedPercent(holding.returns['1d'])}</span></button>)}</div>}</Card>;
      })}</div></details>
    </>}
    {spotlight && <StockDetailDialog title={`${spotlight.symbol} · holding details`} onClose={() => setSelectedHolding(null)}><SecurityDetail holding={spotlight} choices={data.holdings} currency={data.currency} range={range} revision={revision} onChoose={chooseHolding} /></StockDetailDialog>}

    <details ref={notes} id="investment-analysis-notes" className="rounded-xl border border-line bg-white px-4 text-xs text-mist"><summary className="min-h-11 cursor-pointer py-3 font-medium">Data coverage</summary><div className="space-y-4 pb-4">
      <dl aria-label="Investment data coverage" className="grid grid-cols-3 gap-3 rounded-lg bg-surface2 p-3">
        {([["Account balances", summary.known_accounts, summary.selected_accounts], ["Positions valued", summary.valued_holdings, summary.holdings_count], ["Session estimate", summary.priced_holdings, summary.holdings_count]] as const).map(([label, known, total]) => <div className="min-w-0" key={label}><dt className="text-xs">{label}</dt><dd className={`mt-1 text-sm font-semibold tabular-nums ${known < total ? "text-amber" : "text-foam"}`}>{known} / {total}</dd></div>)}
      </dl>
      <p>Account value includes cash. Session move is an estimate for covered positions, not your investment return.</p>
      {missingBalances.length > 0 && <p className="text-amber">Balance unavailable: {missingBalances.map(accountLabel).join("; ")}.</p>}
      {missingSession.length > 0 && <div aria-label="Positions excluded from session move" className="space-y-2"><p className="font-medium text-foam">Excluded from the session estimate</p>
        {[...sessionGroups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).map(([reason, holdings]) => {
          const byAccount = new Map<string, typeof holdings>();
          for (const holding of holdings) byAccount.set(holding.account_id, [...(byAccount.get(holding.account_id) ?? []), holding]);
          return <details key={`${scopeKey}:${reason}`} className="rounded-lg border border-line px-3"><summary className="min-h-11 cursor-pointer py-3 text-foam">{reason} <span className="text-mist">· {holdings.length} {holdings.length === 1 ? "position" : "positions"}</span></summary>
            <div className="max-h-72 space-y-3 overflow-y-auto overscroll-contain pb-3">{[...byAccount].map(([id, positions]) => {
              const account = data.accounts.find(row => row.id === id);
              const label = account ? accountLabel(account) : positions[0].account_name || positions[0].institution;
              return <div key={id}><p className="break-words font-medium">{label} · {positions.length} {positions.length === 1 ? "position" : "positions"}</p><ul className="mt-1 grid gap-x-4 sm:grid-cols-2">{positions.map(holding => <li className="min-w-0" key={holding.id}><button type="button" className="min-h-11 w-full break-words py-2 text-left hover:text-foam" aria-label={`View ${holding.symbol} details in ${label}`} onClick={() => chooseHolding(holding.id)}><span className="font-semibold text-foam">{holding.symbol}</span> · {holding.name}</button></li>)}</ul></div>;
            })}</div>
          </details>;
        })}
        <p>Their known market values remain included in your account value.</p>
      </div>}
      <div className="divide-y divide-line border-t border-line">
        <details><summary className="min-h-11 cursor-pointer py-3 font-medium"><span>Source details</span>{data.warnings.length > 0 && <span className="font-normal"> · {new Set(data.warnings).size} {new Set(data.warnings).size === 1 ? "note" : "notes"}</span>}</summary><div className="space-y-3 pb-3">
          <p>{data.balance_as_of ? <>Broker balances as of {portfolioDate(data.balance_as_of, true)}.</> : <>Broker balances retrieved {portfolioDate(data.retrieved_at, true)}; the broker did not supply a valuation timestamp.</>} Holdings as of {portfolioDate(data.holdings_as_of ?? null, true)}.</p>
          <p>Portfolio retrieved {portfolioDate(data.retrieved_at, true)}; latest market observation {portfolioDate(data.market_as_of, true)}.</p>
          <p>{summary.classified_holdings} / {summary.direct_holdings ?? summary.holdings_count} direct positions classified by sector{summary.fund_holdings !== undefined ? ` · ${summary.fund_holdings} funds grouped separately` : ""}{summary.unclassified_holdings ? ` · ${summary.unclassified_holdings} unclassified` : ""}.</p>
          {[...new Set(data.warnings)].map(warning => <p key={warning} className="text-amber">{warning}</p>)}{extraNotes}
        </div></details>
        <details><summary className="min-h-11 cursor-pointer py-3 font-medium">How figures are calculated</summary><div className="space-y-3 pb-3">
          {brokerReturn && <p>Broker return is a per-account figure supplied through SnapTrade for {brokerReturn.timeframe}; its calculation method is not reported. It is not combined across accounts. Observation {portfolioDate(brokerReturn.as_of, true)}.</p>}
          {[...new Set(data.methodology)].map(method => <p key={method}>{method}</p>)}
        </div></details>
      </div>
    </div></details>
  </div>;
}
