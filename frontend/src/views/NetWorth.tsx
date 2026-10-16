import { Heading } from "../components/Heading";
import MobileDisclosure from "../components/MobileDisclosure";
import { AvailableCreditEvidence } from "../components/AvailableCreditEvidence";
import { DataTable, TableMeta } from "../components/DataTable";
import Icon from "../components/Icon";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { archiveWealthSource, fetchWealth, type WealthAccount, type WealthData, type WealthFilters, type WealthRange } from "../wealthApi";
import { refreshWealthSources, WEALTH_REFRESH_SOURCES, type WealthRefreshSource } from "../wealthRefresh";
import WealthRefreshStatus, { initialWealthRefreshRows } from "../components/WealthRefreshStatus";
import { useWorkspaceState } from "../workspaceState";
import { useReportingCurrency } from "../useReportingCurrency";
import { openBankAccount } from "../banking";
import { openFinancePage } from "../navigation";
import { isKnown, nativeMoney, portfolioDate } from "../connectedPortfolio";
import { filterWealthAccounts, selectedWealthAccounts, wealthBalanceNeedsReview, wealthNetAssetsHistory, wealthComposition, wealthSourceDate, wealthSourceGroups, wealthValuationLabel } from "../wealth";
import { Card, Empty, useAsync } from "../components/ui";
import { ChartSkeleton, PageSkeleton } from "../components/Skeleton";
import RecentTransactions from "../components/RecentTransactions";
import { C } from "../theme";
import { watchData } from "../dataEvents";

const WealthHistoryChart = lazy(() => import("../components/WealthHistoryChart"));

const RANGES: [WealthRange, string][] = [["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["1y", "1Y"], ["all", "All"]];
const COLORS = { investment: C.teal, cash: C.cyan, credit: C.kelp };
const ownershipLabel = (account: WealthAccount) => account.ownership_share === null ? "Ownership needs review" : typeof account.ownership_share === "number" && account.ownership_share < 1 ? `${Number((account.ownership_share * 100).toFixed(2))}% ownership` : "";
const sourceLabel = (account: WealthAccount) => [account.balance_source === "manual" ? "Manual balance" : account.source === "snaptrade" ? "Connected investment" : account.valuation?.reported_source === "statement" ? "Statement" : account.source === "lunchflow" ? "Connected bank" : "Statement", ownershipLabel(account)].filter(Boolean).join(" · ");

export default function NetWorth() {
  const [filters, setFilters] = useWorkspaceState<WealthFilters>("net-worth:filters", { currency: "GBP", range: "1m", accountIds: null });
  useReportingCurrency("net-worth:filters", currency => setFilters(current => ({ ...current, currency })));
  const [request, setRequest] = useState(0);
  const [data, setData] = useState<WealthData | null>(null);
  const [loading, setLoading] = useState(true);
  const recent = useAsync(signal => api.transactions({ sort: "date", order: "desc", limit: 10, offset: 0 }, signal));
  const [error, setError] = useState<string | null>(null);
  const [refreshRows, setRefreshRows] = useState(initialWealthRefreshRows);
  const [refreshing, setRefreshing] = useState(false);
  const refreshingController = useRef<AbortController | null>(null);
  const [archivingId, setArchivingId] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [archiveNotice, setArchiveNotice] = useState<string | null>(null);
  const [undoArchive, setUndoArchive] = useState<{ account: WealthAccount; wasSelected: boolean } | null>(null);
  const archivePending = useRef(false);
  const [draftIds, setDraftIds] = useState<string[] | null>(null);
  const [accountSearch, setAccountSearch] = useState("");
  const [search, setSearch] = useWorkspaceState("net-worth:account-search", "");
  const [balanceScope, setBalanceScope] = useWorkspaceState<"all" | "assets" | "debt" | "unknown">("net-worth:balance-scope", "all");
  const [coolingDown, setCoolingDown] = useState(false);
  const [refreshStartedAt, setRefreshStartedAt] = useState(0);
  const selector = useRef<HTMLDetailsElement>(null);
  const notesPanel = useRef<HTMLDetailsElement>(null);
  const selectionKey = filters.accountIds === null ? "default" : JSON.stringify(filters.accountIds);

  useEffect(() => () => refreshingController.current?.abort(), []);
  useEffect(() => watchData(() => {
    // A local refresh/archive rereads once after its dependent writes finish.
    // Other tabs and background syncs must also update the visible headline.
    if (!refreshingController.current && !archivePending.current) setRequest(previous => previous + 1);
  }), []);
  async function refresh(sources: readonly WealthRefreshSource[] = WEALTH_REFRESH_SOURCES) {
    if (refreshingController.current) return;
    const allowed = sources.filter(source => Date.now() >= (refreshRows[source].startedAt ?? 0) + 60_000);
    if (!allowed.length) return;
    const controller = new AbortController();
    refreshingController.current = controller;
    setRefreshing(true);
    setRefreshRows(previous => ({ ...previous, ...Object.fromEntries(allowed.map(source => [source, { ...previous[source], status: "waiting", message: undefined }])) }));
    try {
      await refreshWealthSources(allowed, controller.signal, source => {
        const startedAt = Date.now();
        setRefreshStartedAt(startedAt);
        setRefreshRows(previous => ({ ...previous, [source]: { status: "running", startedAt } }));
      }, (source, result) => setRefreshRows(previous => ({ ...previous, [source]: { ...previous[source], ...result } })));
      if (!controller.signal.aborted) setRequest(previous => previous + 1);
    } finally {
      if (!controller.signal.aborted) setRefreshing(false);
      if (refreshingController.current === controller) refreshingController.current = null;
    }
  }

  useEffect(() => {
    if (!refreshStartedAt) return;
    setCoolingDown(true);
    const timer = setTimeout(() => setCoolingDown(false), Math.max(0, refreshStartedAt + 60_000 - Date.now()));
    return () => clearTimeout(timer);
  }, [refreshStartedAt]);
  useEffect(() => {
    function close(event: PointerEvent) { if (selector.current && !selector.current.contains(event.target as Node)) selector.current.open = false; }
    function escape(event: KeyboardEvent) { if (event.key === "Escape" && selector.current?.open) { selector.current.open = false; selector.current.querySelector("summary")?.focus(); } }
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    queueMicrotask(() => {
      if (controller.signal.aborted) return;
      setLoading(true); setError(null);
      void (async () => {
        if (controller.signal.aborted) return;
        const result = await fetchWealth(filters, controller.signal);
        if (!controller.signal.aborted) setData(result);
      })().catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Couldn’t load net assets."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    });
    return () => controller.abort();
    // The selection key represents the selected account IDs, not array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.currency, filters.range, selectionKey, request]);

  function changeFilters(next: Partial<WealthFilters>) { setFilters(previous => ({ ...previous, ...next })); }
  async function archiveSource(account: WealthAccount, archived: boolean, undo = false) {
    if (archivePending.current || refreshingController.current) return;
    archivePending.current = true;
    const wasSelected = filters.accountIds?.includes(account.id) ?? false;
    setArchivingId(account.id); setArchiveError(null);
    try {
      await archiveWealthSource(account.id, archived);
      setDraftIds(null);
      if (selector.current) selector.current.open = false;
      setFilters(previous => ({ ...previous, accountIds: previous.accountIds === null ? null
        : archived ? previous.accountIds.filter(id => id !== account.id)
        : undo && undoArchive?.wasSelected ? [...new Set([...previous.accountIds, account.id])] : previous.accountIds }));
      setUndoArchive(archived ? { account, wasSelected } : null);
      setRequest(previous => previous + 1);
      setArchiveNotice(`${account.name} ${archived ? "archived. Saved records retained." : "reinstated."}`);
    } catch (reason) { setArchiveError(reason instanceof Error ? reason.message : "The source could not be updated. Please try again."); }
    finally { archivePending.current = false; setArchivingId(null); }
  }
  function showNotes() { if (notesPanel.current) { notesPanel.current.open = true; notesPanel.current.scrollIntoView({ behavior: "smooth", block: "start" }); } }
  function openAccount(account: WealthAccount) {
    if (account.drilldown) {
      if (account.drilldown.page === "banking" && account.drilldown.account_id) openBankAccount(account.drilldown.account_id);
      else openFinancePage(account.drilldown.page, account.drilldown.account_id ?? undefined);
    } else if (account.kind === "investment") openFinancePage(account.source === "snaptrade" ? "connected-portfolio" : "investments");
    else openBankAccount(account.id.replace(/^statement:/, ""));
  }
  const accounts = data ? selectedWealthAccounts(data.accounts, data.selected_account_ids) : [];
  const visibleAccounts = filterWealthAccounts(accounts, search, balanceScope);
  const { active: selectable, archived, linked } = wealthSourceGroups(data?.accounts ?? []);
  const selectorAccounts = selectable.filter((account) => `${account.name} ${account.institution}`.toLowerCase().includes(accountSearch.toLowerCase().trim()));
  const balanceChecks = accounts.filter(wealthBalanceNeedsReview);
  const estimatedAccounts = data?.summary.estimated_accounts ?? accounts.filter((account) => account.valuation?.estimated).length;
  const unknownLiabilities = accounts.filter((account) => account.kind === "credit" && account.valuation?.current_unknown);
  const debtSources = accounts.filter((account) => wealthBalanceNeedsReview(account) || isKnown(account.value) && account.value < 0);
  const checkedIds = draftIds ?? data?.selected_account_ids ?? [];
  const selectionChanged = draftIds !== null && [...draftIds].sort().join("|") !== [...(data?.selected_account_ids ?? [])].sort().join("|");
  const composition = wealthComposition(accounts);
  const positiveTotal = composition.reduce((sum, part) => sum + part.value, 0);
  const history = data ? wealthNetAssetsHistory(data) : null;
  const notes = [...new Set([...(data?.warnings ?? []), ...(data?.cache.refresh_error ? [data.cache.refresh_error] : [])])];
  const coverage = data ? data.summary.missing_accounts > 0 ? `${data.summary.known_accounts}/${data.summary.selected_accounts} valued` : data.summary.stale_accounts > 0 ? `${data.summary.stale_accounts} older sources` : `${data.summary.selected_accounts} account${data.summary.selected_accounts === 1 ? "" : "s"}` : "Loading…";

  return <div className={`space-y-4 ${data ? "content-enter" : ""}`}>
    {loading && !data && <PageSkeleton label="Loading your saved balances…" variant="networth" />}
    {error && <p role="alert" className="rounded-lg border border-amber/30 bg-amber/5 px-4 py-3 text-sm text-amber">{data ? `Couldn’t update this view. Previous ${data.currency} figures remain visible.` : "Net assets could not be loaded."} <button className="ml-2 underline" onClick={() => setRequest(previous => previous + 1)}>Try again</button></p>}
    {data && history && <>
      <Card className="!p-0 overflow-hidden" aria-busy={loading || refreshing}>
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4 p-4 sm:p-5">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-3"><Heading level={2} variant="label">{estimatedAccounts ? "Estimated known net assets" : data.summary.partial ? "Known net assets" : balanceChecks.length || data.summary.balance_check_accounts ? "Reported net assets" : "Net assets"} · {data.currency}</Heading><button className={`min-h-11 text-xs underline decoration-line underline-offset-4 ${data.summary.partial || data.summary.stale_accounts ? "text-amber" : "text-mist"}`} onClick={showNotes}>{loading || refreshing ? "Updating…" : coverage}</button></div><output aria-label="Net assets value" className="mt-2 block break-words text-[34px] font-semibold leading-tight tracking-tight tabular-nums sm:text-[40px]">{nativeMoney(data.summary.net_worth, data.currency)}</output><p className="mt-1 text-xs text-mist">{data.summary.partial ? "Partial total · " : ""}{estimatedAccounts ? "includes estimated balances" : "latest supported balances"} · debts subtracted</p></div>
          <dl className="grid min-w-0 grid-cols-2 items-end gap-x-5 gap-y-3 sm:gap-x-8"><div><dt className="text-xs text-mist">Positive balances</dt><dd className="mt-1 break-words text-lg font-semibold tabular-nums sm:text-xl">{nativeMoney(data.summary.assets, data.currency)}</dd></div><div><dt className="text-xs text-mist">{estimatedAccounts ? "Estimated debt & negative balances" : "Debt & negative balances"}</dt><dd className="mt-1 break-words text-lg font-semibold tabular-nums text-coral sm:text-xl">{nativeMoney(data.summary.liabilities, data.currency)}</dd></div></dl>
        </div>
        {unknownLiabilities.length > 0 && <p className="border-t border-line bg-amber/5 px-4 py-3 text-xs text-amber sm:px-5">Current debt unknown for {unknownLiabilities.map((account) => account.name).join(", ")}; excluded from this partial total, not treated as paid off.</p>}
      </Card>

      {debtSources.length > 0 && <MobileDisclosure label="Debt balance details" summary={`${debtSources.length} account${debtSources.length === 1 ? "" : "s"}${balanceChecks.length ? ` · ${balanceChecks.length} to check` : ""}`}><Card>
        <Heading level={2} variant="section">Debt balances</Heading>
        {balanceChecks.length > 0 && <p className="mt-2 text-xs text-amber">{balanceChecks.length} account{balanceChecks.length === 1 ? " needs" : "s need"} a balance check. Unknown balances are excluded from the total.</p>}
        <ul className="mt-3 divide-y divide-line">{debtSources.map((account) => <li key={account.id} className="py-3 first:pt-0 last:pb-0">
          <div className="flex flex-wrap justify-between gap-2"><button className="text-left text-sm font-medium underline decoration-line underline-offset-4 hover:text-teal" onClick={() => openAccount(account)}>{account.name}</button><span className="text-sm font-semibold tabular-nums">{account.valuation?.current_unknown ? "Current balance unknown" : nativeMoney(account.value, data.currency)}</span></div>
          <details className="debt-evidence mt-0.5 text-xs text-mist" aria-label={`Balance details for ${account.name}`}><summary className="flex min-h-11 cursor-pointer list-none flex-wrap items-center justify-between gap-x-4 gap-y-1"><span>{wealthValuationLabel(account)} · {wealthSourceDate(account)}{account.stale ? " · older source" : ""}</span><span className="inline-flex items-center gap-1 whitespace-nowrap underline decoration-line underline-offset-4"><span className="debt-chevron" aria-hidden="true">›</span><span>Balance details</span></span></summary><div className="space-y-2 py-2 leading-relaxed">
          <p>{sourceLabel(account)}{account.valuation?.estimated ? " · estimate from the statement and booked activity" : ""}.</p>
          {account.valuation?.basis === "available_credit" && <div className="text-mist"><AvailableCreditEvidence valuation={account.valuation} currency={account.currency} /></div>}
          {account.valuation && !["reported", "available_credit"].includes(account.valuation.basis) && <><p className="text-xs text-amber">{account.valuation.explanation}</p><p className="text-xs text-mist">{(account.valuation.reported_source ?? account.source) === "statement" ? "Previous statement balance" : "Provider-reported balance"}: {nativeMoney(account.reported_native_value ?? account.valuation.reported_balance, account.currency)}{account.valuation.reported_as_of ? ` · ${portfolioDate(account.valuation.reported_as_of)}` : ""}. Excluded from the current calculation.</p></>}
          {account.balance_check && account.valuation?.basis !== "available_credit" && <>
            {(!account.valuation || account.valuation.basis === "reported") && <p className={`text-xs ${wealthBalanceNeedsReview(account) ? "text-amber" : "text-mist"}`}>{account.balance_check.message}</p>}
            {account.balance_check.status === "discrepancy" && <p className="text-xs text-mist">Statement {nativeMoney(account.balance_check.statement_balance, account.balance_check.currency)} on {portfolioDate(account.balance_check.statement_date)} · booked activity estimate {nativeMoney(account.balance_check.transaction_estimate, account.balance_check.currency)}{account.valuation?.estimated ? ". This estimate is used in the total." : ". This estimate is a check only; it is not used without sufficient activity coverage."}</p>}
            {account.balance_check.pending_count > 0 && <p className="text-xs text-mist">Pending activity: {nativeMoney(account.balance_check.pending_amount, account.balance_check.currency)} across {account.balance_check.pending_count} transaction{account.balance_check.pending_count === 1 ? "" : "s"}. {account.valuation?.estimated ? "Excluded from the booked balance estimate." : "Treatment in the reported balance may vary by bank."}</p>}
            {account.source === "lunchflow" && account.balance_check.status === "discrepancy" && <p className="text-xs text-mist"><a className="underline decoration-line underline-offset-4" href="https://www.lunchflow.app/docs/guides/configuration/balance" target="_blank" rel="noopener noreferrer">Check Lunch Flow balance settings ↗</a> · Compare the actual amount owed or current balance with available credit.</p>}
            {account.balance_check.potential_repayments?.length > 0 && <p className="text-xs text-mist">Possible repayments after the statement: {account.balance_check.potential_repayments.map((payment) => `${nativeMoney(payment.amount, account.balance_check!.currency)} on ${portfolioDate(payment.date)}`).join(", ")}. {account.valuation?.current_unknown ? "A newer card balance is needed to confirm the remaining debt." : "Shown for checking the statement; these amounts are not subtracted again."}</p>}
          </>}
          </div></details>
        </li>)}</ul>
      </Card></MobileDisclosure>}

      <MobileDisclosure label="Accounts & currency" summary={`${data.selected_account_ids.length} account${data.selected_account_ids.length === 1 ? "" : "s"} · ${data.currency}`}><div className="flex flex-wrap items-center gap-2" aria-label="Net assets controls">
        <details ref={selector} className="relative min-w-0 w-full sm:w-auto" onToggle={(event) => { if (event.currentTarget.open) { setDraftIds(data.selected_account_ids); setAccountSearch(""); requestAnimationFrame(() => document.getElementById("wealth-source-search")?.focus()); } else if (event.currentTarget.contains(document.activeElement)) { event.currentTarget.querySelector("summary")?.focus(); } }}>
          <summary className="btn cursor-pointer list-none">Accounts · {data.selected_account_ids.length}<span className="ml-2 text-mist" aria-hidden="true">⌄</span></summary>
          <div className="relative mt-2 flex max-h-[calc(100dvh-12rem)] w-full flex-col overflow-hidden rounded-xl border border-line bg-white shadow-lg sm:absolute sm:left-0 sm:top-full sm:z-50 sm:mt-2 sm:max-h-[70vh] sm:w-[min(30rem,calc(100vw-2rem))]">
            <div className="shrink-0 border-b border-line p-3"><div className="mb-2 flex items-center justify-between gap-3"><p className="text-sm font-semibold">Accounts to include</p><button className="min-h-11 min-w-11 rounded-lg text-mist hover:bg-surface2" aria-label="Close account selection" onClick={() => { if (selector.current) selector.current.open = false; }}>×</button></div><label className="sr-only" htmlFor="wealth-source-search">Find an account to include</label><input id="wealth-source-search" className="input w-full" type="search" value={accountSearch} onChange={(event) => setAccountSearch(event.target.value)} placeholder="Find an account or bank" /></div>
            <fieldset disabled={refreshing || archivingId !== null} className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2 sm:max-h-80"><legend className="sr-only">Accounts included in net assets</legend>{selectorAccounts.map((account) => <div key={account.id} className="flex items-start gap-2 rounded-lg px-2 py-2.5 hover:bg-surface2"><label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3"><input className="mt-1 h-4 w-4 shrink-0 accent-teal" type="checkbox" checked={checkedIds.includes(account.id)} onChange={(event) => setDraftIds(event.target.checked ? [...checkedIds, account.id] : checkedIds.filter((id) => account.id !== id))} /><span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium">{account.name}</span><span className="mt-0.5 block text-xs text-mist">{account.institution} · {account.currency || "Currency unknown"}</span>{account.exclusion_reason && <span className="mt-1 block text-xs text-amber">{account.exclusion_reason}</span>}</span></label>{account.kind === "investment" && <button className="min-h-11 shrink-0 px-2 text-xs text-mist underline" disabled={loading || refreshing || archivingId !== null} onClick={() => void archiveSource(account, true)} aria-label={`Archive ${account.name}`}>Archive</button>}</div>)}{!selectorAccounts.length && <p className="p-3 text-xs text-mist">No accounts match this search.</p>}</fieldset>
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-line p-3"><button className="min-h-11 px-1 text-xs text-mist underline" onClick={() => setDraftIds(selectable.filter((account) => account.default_included).map((account) => account.id))}>Default sources</button><button className="min-h-11 px-2 text-xs text-mist underline" onClick={() => setDraftIds([])}>Clear</button><button className="btn-primary ml-auto" disabled={!selectionChanged || loading || refreshing || archivingId !== null} onClick={() => { changeFilters({ accountIds: checkedIds }); setDraftIds(null); if (selector.current) selector.current.open = false; }}>Apply selection</button></div>
            {linked.length > 0 && <p className="shrink-0 border-t border-line px-3 py-2 text-xs text-mist">{linked.length} linked source{linked.length === 1 ? " is" : "s are"} counted with the connected account.</p>}
          </div>
        </details>
        <select aria-label="Reporting currency" className="input w-24" value={filters.currency} disabled={refreshing || archivingId !== null} onChange={(event) => changeFilters({ currency: event.target.value })}>{[...new Set([...data.currencies, filters.currency])].map((currency) => <option key={currency}>{currency}</option>)}</select>
        <button className="btn ml-auto" disabled={loading || refreshing || coolingDown || archivingId !== null} onClick={() => void refresh()}>{refreshing ? "Refreshing…" : "↻ Refresh"}</button>
      </div>
      </MobileDisclosure>
      <WealthRefreshStatus rows={refreshRows} busy={refreshing || loading || archivingId !== null} onRetry={source => void refresh([source])} />
      {!!data.summary.unmapped_bank_accounts && <p role="status" className="text-sm text-amber">{data.summary.unmapped_bank_accounts} discovered bank accounts need setup before their balances can be included. <button className="min-h-11 underline" onClick={() => openFinancePage("banking")}>Review bank accounts</button></p>}
      {(archiveNotice || archiveError) && <aside aria-label="Account update" className="fixed inset-x-3 bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-40 flex items-center gap-3 rounded-xl border border-line bg-white p-3 shadow-lg sm:inset-x-auto lg:bottom-5 sm:right-5 sm:max-w-lg">
        <div className="min-w-0 flex-1 break-words text-xs">{archiveNotice && <p role="status">{archiveNotice}</p>}{archiveError && <p role="alert" className="text-amber">{archiveError}</p>}</div>
        {undoArchive && <button className="btn shrink-0" disabled={loading || refreshing || archivingId !== null} onClick={() => void archiveSource(undoArchive.account, false, true)} aria-label="Undo archive">Undo</button>}
        <button className="table-action shrink-0" aria-label="Dismiss account update" onClick={() => { setArchiveNotice(null); setArchiveError(null); setUndoArchive(null); }}>×</button>
      </aside>}

      <Card className="!p-4 sm:!p-5" aria-busy={loading || refreshing}>
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3"><div className="min-w-0"><Heading level={2} variant="section">Net assets over time</Heading><p className="mt-1 text-xs text-mist">{history.net_start_date ? `Full selected-account coverage from ${portfolioDate(history.net_start_date)}` : "No date has supported values for every selected account"}{isKnown(history.end_value) ? ` · ${nativeMoney(history.end_value, data.currency)} at ${portfolioDate(history.end_date)}` : ""}</p>{history.change_comparable && isKnown(history.change) && <p className={`mt-2 text-sm font-semibold tabular-nums ${history.change >= 0 ? "text-kelp" : "text-coral"}`}>{history.change > 0 ? "+" : ""}{nativeMoney(history.change, data.currency)}{isKnown(history.change_pct) ? ` (${history.change_pct > 0 ? "+" : ""}${history.change_pct.toFixed(1)}%)` : ""}<span className="ml-2 text-xs font-normal text-mist">balance change</span></p>}</div>
          <div role="group" aria-label="Net assets time range" className="flex rounded-lg bg-surface2 p-1">{RANGES.map(([range, label]) => <button key={range} aria-pressed={filters.range === range} disabled={refreshing || archivingId !== null} className={`min-h-11 min-w-11 rounded-md px-2 text-xs sm:px-3 ${filters.range === range ? "bg-white font-semibold shadow-sm" : "text-mist hover:text-foam"}`} onClick={() => changeFilters({ range })}>{label}</button>)}</div>
        </div>
        {(history.points.length > 0 || (data.history?.investment_history?.points.length ?? 0) > 0) ? <div className="wealth-chart-slot"><Suspense fallback={<ChartSkeleton label="Loading net assets history…" height={480} />}><WealthHistoryChart key={`${data.currency}|${data.range}|${history.account_ids.join("|")}`} points={history.points} investmentPoints={data.history?.investment_history?.points} investmentAccounts={data.history?.investment_history?.account_ids.length ?? 0} currency={data.currency} selectedAccounts={data.summary.selected_accounts} /></Suspense></div> : <Empty>{data.summary.selected_accounts === 0 ? "Choose accounts to build the history." : "Dated balances do not yet cover every selected account. The current total is shown above."}</Empty>}
        {data.history?.investment_history?.start_date && data.history.investment_history.start_date < (history.net_start_date ?? "9999") && <p className="mt-2 text-xs text-mist">Older investment values cover the selected investment accounts only. Bank and card balances are unavailable for those dates, so full net assets begin at {history.net_start_date ? portfolioDate(history.net_start_date) : "a later supported date"}. Missing days remain gaps. <button className="underline decoration-line underline-offset-4" onClick={showNotes}>View coverage</button></p>}
        {!data.history?.investment_history?.points.length && history.net_start_date && data.history?.cohort?.points.some(point => point.date < history.net_start_date!) && <p className="mt-2 text-xs text-mist">Older records cover fewer selected accounts, so they are excluded from this net-assets chart. Missing days remain gaps. <button className="underline decoration-line underline-offset-4" onClick={showNotes}>View coverage</button></p>}
      </Card>

      <Card className="!p-0 overflow-hidden"><div className="space-y-3 p-3 sm:p-4"><div className="flex flex-wrap items-center justify-between gap-3"><Heading level={2} variant="section">Account breakdown <span className="ml-1 text-xs font-normal text-mist">{accounts.length} selected</span></Heading><div className="flex rounded-lg bg-surface2 p-1" role="group" aria-label="Balance type">{([['all', 'All'], ['assets', 'Positive'], ['debt', 'Debt'], ['unknown', 'Unknown']] as const).map(([value, label]) => <button key={value} className={`min-h-11 rounded-md px-3 text-xs ${balanceScope === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`} aria-pressed={balanceScope === value} onClick={() => setBalanceScope(value)}>{label}</button>)}</div></div>
        {composition.length > 0 && <div><div className="flex h-2 overflow-hidden rounded-full bg-surface2" aria-hidden="true">{composition.map((part) => <span key={part.kind} style={{ width: `${part.value / positiveTotal * 100}%`, background: COLORS[part.kind] }} />)}</div><dl className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs">{composition.map((part) => <div key={part.kind} className="flex items-center gap-2"><span className="h-2 w-2 rounded-full" style={{ background: COLORS[part.kind] }} aria-hidden="true" /><dt className="text-mist">{part.name}</dt><dd className="font-medium tabular-nums">{nativeMoney(part.value, data.currency)}</dd></div>)}</dl></div>}
        <input type="search" aria-label="Search net assets accounts" className="input w-full sm:max-w-sm" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find an account or institution" />
      </div>
      {!visibleAccounts.length ? <Empty>{accounts.length ? "No accounts match these filters." : "No accounts selected."}</Empty> : <>
        <div className="divide-y divide-line border-t border-line sm:hidden">{visibleAccounts.map(account => <div key={account.id} className="px-3 py-3">
          <div className="flex items-center gap-2">
            <button className="flex min-w-0 flex-1 items-center justify-between gap-3 text-left hover:text-teal" onClick={() => openAccount(account)} aria-label={`Open ${account.name}`}>
              <span className="min-w-0"><span className="block break-words text-sm font-medium">{account.name}</span><TableMeta>{account.institution} · {wealthSourceDate(account)}{account.stale ? " · older source" : ""}{account.estimated ? " · estimated" : ""}</TableMeta>{(account.balance_source === "manual" || ownershipLabel(account)) && <TableMeta>{[account.balance_source === "manual" ? "Manual balance" : "", ownershipLabel(account)].filter(Boolean).join(" · ")}</TableMeta>}</span>
              <span className="shrink-0 text-right"><span className={`block text-sm font-semibold tabular-nums ${isKnown(account.value) && account.value < 0 ? "text-coral" : ""}`}>{account.valuation?.current_unknown ? "Unknown" : nativeMoney(account.value, data.currency)}</span>{account.currency !== data.currency && <TableMeta>{nativeMoney(account.native_value, account.currency)}</TableMeta>}</span>
            </button>
            {account.kind === "investment" && <button className="table-action" disabled={loading || refreshing || archivingId !== null} onClick={() => void archiveSource(account, !account.archived_by_user)} aria-label={`${account.archived_by_user ? "Reinstate" : "Archive"} ${account.name}`} title={account.archived_by_user ? "Reinstate account" : "Archive account"}><Icon name={account.archived_by_user ? "restore" : "archive"} /></button>}
          </div>
          {wealthBalanceNeedsReview(account) && <p className="mt-2 text-xs text-amber">{account.valuation && account.valuation.basis !== "reported" ? account.valuation.explanation : `Balance check: ${account.balance_check?.message}`}</p>}
        </div>)}</div>
        <div className="hidden overflow-x-auto sm:block" role="region" aria-label="Net assets account sources" tabIndex={0}><DataTable className="w-full min-w-[600px] text-left text-sm"><thead className="border-y border-line bg-surface2/60 text-xs text-mist"><tr><th className="px-5 py-3 font-medium">Account</th><th className="px-4 py-3 font-medium">Source / date</th><th className="px-4 py-3 text-right font-medium">Value · {data.currency}</th><th className="w-px px-4 py-3"><span className="sr-only">Account actions</span></th></tr></thead><tbody>{visibleAccounts.map((account) => <tr key={account.id} className="border-b border-line/60 last:border-0"><td className="px-5 py-4"><button className="text-left font-medium underline decoration-line underline-offset-4 hover:text-teal" onClick={() => openAccount(account)}>{account.name}</button><TableMeta>{account.institution}{account.estimated ? " · estimated value" : ""}</TableMeta>{wealthBalanceNeedsReview(account) && <p className="mt-2 max-w-md text-xs text-amber">{account.valuation && account.valuation.basis !== "reported" ? account.valuation.explanation : `Balance check: ${account.balance_check?.message}`}</p>}</td><td className="px-4 py-4 text-xs text-mist"><p>{sourceLabel(account)}</p><TableMeta tone={account.stale ? "warning" : "muted"}>{wealthSourceDate(account)}{account.stale ? " · older" : ""}</TableMeta></td><td className="whitespace-nowrap px-4 py-4 text-right tabular-nums"><p className={`font-semibold ${isKnown(account.value) && account.value < 0 ? "text-coral" : ""}`}>{account.valuation?.current_unknown ? "Unknown" : nativeMoney(account.value, data.currency)}</p>{account.currency !== data.currency && <TableMeta>{nativeMoney(account.native_value, account.currency)} native</TableMeta>}</td><td className="px-4 py-4"><div className="table-actions"><button className="table-action" onClick={() => openAccount(account)} aria-label={`Open ${account.name}`} title={`Open ${account.name}`}>↗</button>{account.kind === "investment" && <button className="table-action" disabled={loading || refreshing || archivingId !== null} onClick={() => void archiveSource(account, !account.archived_by_user)} aria-label={`${account.archived_by_user ? "Reinstate" : "Archive"} ${account.name}`} title={account.archived_by_user ? "Reinstate account" : "Archive account"}><Icon name={account.archived_by_user ? "restore" : "archive"} /></button>}</div></td></tr>)}</tbody></DataTable></div>
      </>}
      </Card>

      <RecentTransactions state={recent} reportingCurrency={data.currency} />

      {archived.length > 0 && <details className="rounded-xl border border-line bg-white px-4">
        <summary className="cursor-pointer py-3 text-sm font-medium">Archived sources · {archived.length}</summary>
        <p className="pb-3 text-xs text-mist">These sources are outside the default net assets. Saved balances, transactions and historical records are retained.</p>
        <ul className="divide-y divide-line">{archived.map((account) => <li key={account.id} className="flex flex-wrap items-start justify-between gap-3 py-3"><div className="min-w-0 flex-1"><button className="text-left text-sm font-medium underline decoration-line underline-offset-4 hover:text-teal" onClick={() => openAccount(account)}>{account.name}</button><p className="mt-1 text-xs text-mist">{account.institution} · {sourceLabel(account)}</p><p className="mt-1 text-xs text-mist">{account.exclusion_reason || (account.history_only ? "Historical activity only; no supported account valuation." : "Archived from default net assets. Saved records are retained.")}</p></div>{account.archived_by_user && <button className="btn shrink-0" disabled={loading || refreshing || archivingId !== null} onClick={() => void archiveSource(account, false)} aria-label={`Reinstate ${account.name}`}>Reinstate</button>}{checkedIds.includes(account.id) && <button className="btn shrink-0" disabled={loading} onClick={() => { changeFilters({ accountIds: data.selected_account_ids.filter((id) => id !== account.id) }); setDraftIds(null); }}>Remove from selection</button>}</li>)}</ul>
      </details>}

      <details ref={notesPanel} className="scroll-mt-24 rounded-xl border border-line bg-white px-4 text-xs text-mist"><summary className="min-h-11 cursor-pointer py-3 font-medium">Coverage, sources &amp; calculations{notes.length ? ` · ${notes.length} notes` : ""}</summary><div className="space-y-3 pb-4"><p>{history.coverage_note}</p>{(data.history?.cohort?.excluded_account_ids.length ?? 0) > 0 && <p>Older fixed-account records omit: {data.accounts.filter((account) => data.history?.cohort?.excluded_account_ids.includes(account.id)).map((account) => account.name).join(", ")}.</p>}{notes.map((note, index) => <p key={`note-${index}`} className="text-amber">{note}</p>)}{data.methodology.slice(0, 2).map((text, index) => <p key={`method-${index}`}>{text}</p>)}{data.methodology.length > 2 && <details className="border-t border-line pt-2"><summary className="min-h-11 cursor-pointer py-2 font-medium text-foam">Source and history details</summary><div className="space-y-2 pb-2">{data.methodology.slice(2).map((text, index) => <p key={`method-detail-${index}`}>{text}</p>)}</div></details>}{linked.length > 0 && <div><p className="font-medium text-foam">Linked sources counted once</p><ul className="mt-2 list-disc space-y-1 pl-4">{linked.map((account) => <li key={account.id}>{account.name} → {data.accounts.find((canonical) => canonical.id === account.linked_to)?.name || "connected account"}</li>)}</ul></div>}<p>Calculated {portfolioDate(data.generated_at, true)}. Source dates vary by account. Refresh retrieves bank, brokerage and reference-rate data without placing trades or forcing a provider sync.</p>{data.cache.saved_at && <p>Investment snapshot saved locally {portfolioDate(data.cache.saved_at, true)}.</p>}
      {data.points.length > 0 && <details className="border-t border-line pt-2"><summary className="min-h-11 cursor-pointer py-3">All historical coverage observations</summary><div className="max-h-60 overflow-auto"><DataTable className="w-full min-w-[360px] text-left"><thead><tr><th className="py-2 font-medium">Date</th><th className="py-2 text-right font-medium">Known total</th><th className="py-2 text-right font-medium">Accounts</th></tr></thead><tbody>{data.points.map((point, index) => <tr key={`${point.date}-${index}`} className="border-t border-line"><td className="py-2">{portfolioDate(point.date)}</td><td className="py-2 text-right tabular-nums">{nativeMoney(point.net_worth, data.currency)}</td><td className="py-2 text-right">{point.known_accounts} / {data.summary.selected_accounts}</td></tr>)}</tbody></DataTable></div></details>}
      </div></details>
    </>}
  </div>;
}
