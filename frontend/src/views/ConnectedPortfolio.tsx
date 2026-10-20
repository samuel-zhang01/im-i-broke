import { useWorkspaceState } from "../workspaceState";
import { useReportingCurrency } from "../useReportingCurrency";
import MobileDisclosure from "../components/MobileDisclosure";
import { useEffect, useRef, useState } from "react";
import { fetchConnectedPortfolio, type ConnectedPortfolioData } from "../snaptradeApi";
import { accountLabel, portfolioDate, sectionIssues } from "../connectedPortfolio";
import { Card, Empty } from "../components/ui";
import { LoadingRegion, PageSkeleton, SkeletonBlock } from "../components/Skeleton";
import { PortfolioActivity, PortfolioOrders } from "../components/ConnectedPortfolio/OrdersAndActivity";
import ConnectionStatus from "../components/ConnectedPortfolio/ConnectionStatus";
import InvestmentWorkspace, { type InvestmentWorkspaceMode } from "../components/InvestmentWorkspace/InvestmentWorkspace";
import { refreshInvestmentMarket, type InvestmentRange } from "../investmentAnalysisApi";
import { fetchWealthSources, refreshWealthRates, type WealthSource } from "../wealthApi";
import InvestmentCalendar from "../components/InvestmentWorkspace/InvestmentCalendar";
import SavedInvestmentActivity from "../components/ConnectedPortfolio/SavedInvestmentActivity";
import { openFinancePage } from "../navigation";
import { connectionRefreshIssue, portfolioRefreshIssue } from "../providerRefresh";

const TABS = [["performance", "Portfolio"], ["sectors", "Analysis"], ["activity", "Activity"], ["connections", "Connections"]] as const;
type PrimaryTab = typeof TABS[number][0];
type Tab = "performance" | "accounts" | "sectors" | "compare" | "review" | "activity" | "orders" | "calendar" | "connections" | "saved-activity";
const SUBVIEWS: Record<PrimaryTab, readonly (readonly [Tab, string])[]> = {
  performance: [["performance", "Holdings"], ["accounts", "Accounts"]],
  sectors: [["sectors", "Allocation"], ["compare", "Compare"], ["review", "Review"]],
  activity: [["activity", "Transactions"], ["saved-activity", "Saved history"], ["orders", "Orders"], ["calendar", "Value calendar"]],
  connections: [],
};
const primaryFor = (tab: Tab): PrimaryTab => ["performance", "accounts"].includes(tab) ? "performance" : ["sectors", "compare", "review"].includes(tab) ? "sectors" : tab === "connections" ? "connections" : "activity";

function refreshScheduleDescription(data: ConnectedPortfolioData) {
  const schedule = data.refresh_schedule;
  if (!data.configured) return "Automatic SnapTrade refresh is unavailable until the connection is configured.";
  if (!schedule) return "Automatic refresh schedule is not reported.";
  if (!schedule.enabled) return "Automatic SnapTrade refresh is off. Use Refresh to retrieve current data.";
  const seconds = schedule.interval_seconds;
  if (!Number.isFinite(seconds) || seconds <= 0) return "Automatic refresh schedule is not reported.";
  const unit = seconds % 3600 === 0 ? "hour" : seconds % 60 === 0 ? "minute" : "second";
  const value = seconds / (unit === "hour" ? 3600 : unit === "minute" ? 60 : 1);
  return `Balances and holdings refresh every ${value} ${unit}${value === 1 ? "" : "s"} while the Am I Broke? server is running.`;
}

export default function ConnectedPortfolio({ initialAccountId, initialSection }: { initialAccountId?: string; initialSection?: "portfolio" | "analysis" | "activity" } = {}) {
  const [data, setData] = useState<ConnectedPortfolioData | null>(null);
  const [loading, setLoading] = useState(true);
  const [sources, setSources] = useState<WealthSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [request, setRequest] = useState({ count: 0, refresh: false });
  const [completedRefresh, setCompletedRefresh] = useState(0);
  const [coolingDown, setCoolingDown] = useState(false);
  const [selected, setSelected] = useWorkspaceState("portfolio:selected", initialAccountId || "all");
  useEffect(() => { if (initialAccountId) { setSelected(initialAccountId); setTab("performance"); } }, [initialAccountId]);
  const [tab, setTab] = useWorkspaceState<Tab>("portfolio:view", initialSection === "analysis" ? "sectors" : initialSection === "activity" ? "activity" : "performance");
  const [lastView, setLastView] = useWorkspaceState<Record<PrimaryTab, Tab>>("portfolio:last-views", { performance: "performance", sectors: "sectors", activity: "activity", connections: "connections" });
  const primary = primaryFor(tab);
  function chooseView(next: Tab) { setLastView(previous => ({ ...previous, [primaryFor(next)]: next })); setTab(next); }
  useEffect(() => { if (initialSection) chooseView(initialSection === "analysis" ? "sectors" : initialSection === "activity" ? "activity" : "performance"); }, [initialSection]);
  const [currency, setCurrency] = useWorkspaceState("portfolio:currency", "GBP");
  useReportingCurrency("portfolio:currency", setCurrency);
  const [range, setRange] = useWorkspaceState<InvestmentRange>("portfolio:range", "1m");
  const [activityScope, setActivityScope] = useWorkspaceState<"all" | "dividends">(`portfolio:${selected}:activity-scope`, "all");
  const [refreshMessages, setRefreshMessages] = useState<string[]>([]);
  const tabButtons = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    queueMicrotask(() => {
      if (controller.signal.aborted) return;
      setLoading(true); setError(null);
      if (request.refresh) { setCoolingDown(true); timer = setTimeout(() => setCoolingDown(false), 60_000); }
      void (async () => {
        if (request.refresh) {
          const messages: string[] = [];
          try { const issue = portfolioRefreshIssue(await fetchConnectedPortfolio(controller.signal, true)); if (issue) messages.push(issue); }
          catch (reason) { if (!controller.signal.aborted) messages.push(reason instanceof Error ? reason.message : "Brokerage refresh failed."); }
          if (controller.signal.aborted) return null;
          try { const rates = await refreshWealthRates(controller.signal); if (rates.missing > 0) messages.push("Some exchange rates are unavailable. Totals include only accounts with supported conversion rates."); }
          catch (reason) { if (!controller.signal.aborted) messages.push(reason instanceof Error ? reason.message : "Reference FX refresh failed."); }
          if (controller.signal.aborted) return null;
          try { const market = await refreshInvestmentMarket(controller.signal); if (market.failed > 0 || (market.incomplete ?? 0) > 0) messages.push(market.message || "Some market data could not be refreshed."); }
          catch (reason) { if (!controller.signal.aborted) messages.push(reason instanceof Error ? reason.message : "Market refresh failed."); }
          if (!controller.signal.aborted) setRefreshMessages(messages);
        }
        if (controller.signal.aborted) return null;
        const [portfolio, sourceList] = await Promise.all([fetchConnectedPortfolio(controller.signal), fetchWealthSources(controller.signal)]);
        if (!controller.signal.aborted) setSources(sourceList);
        return portfolio;
      })().then((result) => {
        if (controller.signal.aborted || !result) return;
        if (result.configured && result.error && result.accounts.length === 0) throw new Error(result.error);
        setData(result);
        // Child views refresh once the new snapshot is ready, never once at
        // refresh start and again when the loading flag changes back.
        setCompletedRefresh(request.count);
      }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Couldn’t retrieve your portfolio."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    });
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [request]);

  const hasPortfolio = data !== null;
  useEffect(() => {
    if (!hasPortfolio || loading) return;
    const controller = new AbortController();
    let busy = false;
    async function readSaved() {
      if (busy || controller.signal.aborted || document.visibilityState !== "visible") return;
      busy = true;
      try {
        const [portfolio, sourceList] = await Promise.all([fetchConnectedPortfolio(controller.signal), fetchWealthSources(controller.signal)]);
        if (controller.signal.aborted) return;
        if (portfolio.configured && portfolio.error && portfolio.accounts.length === 0) throw new Error(portfolio.error);
        setData(previous => {
          if (previous && Date.parse(portfolio.retrieved_at) < Date.parse(previous.retrieved_at)) return previous;
          return JSON.stringify(previous) === JSON.stringify(portfolio) ? previous : portfolio;
        });
        setSources(previous => JSON.stringify(previous) === JSON.stringify(sourceList) ? previous : sourceList);
        setError(null);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Saved portfolio updates are unavailable.");
      } finally { busy = false; }
    }
    const timer = window.setInterval(() => { void readSaved(); }, 60_000);
    const visible = () => { if (document.visibilityState === "visible") void readSaved(); };
    document.addEventListener("visibilitychange", visible);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [hasPortfolio, loading]);

  const all = data?.accounts ?? [];
  const paper = all.filter((account) => account.is_paper === true);
  const live = all.filter((account) => account.is_paper !== true);
  const activeLive = live.filter(account => {
    const source = sources.find(row => row.id === `connected:${account.id}`);
    return source ? source.default_included : true;
  });
  const accounts = selected === "all" ? activeLive : selected === "paper" ? paper : all.filter((account) => account.id === selected);
  const activityCount = accounts.reduce((count, account) => count + account.activities.data.length, 0);
  const dividendCount = accounts.reduce((count, account) => count + account.activities.data.filter((item) => item.type.toLowerCase().includes("dividend")).length, 0);
  const activityAccounts = activityScope === "all" ? accounts : accounts.map((account) => ({ ...account, activities: { ...account.activities, data: account.activities.data.filter((item) => item.type.toLowerCase().includes("dividend")) } }));
  const showingPaper = accounts.length > 0 && accounts.every((account) => account.is_paper === true);
  const missingSelected = !["all", "paper"].includes(selected) && !all.some((account) => account.id === selected);
  const sourceIssues = accounts.map(account => ({ account, sections: (["balances", "positions", "activities", "orders"] as const)
    .filter(key => sectionIssues([account], key).length > 0) })).filter(issue => issue.sections.length > 0);
  const selectedLabel = selected === "all" ? "Active investment accounts" : selected === "paper" ? "All paper accounts" : accounts[0] ? accountLabel(accounts[0]) : "Unavailable account";
  const disabledConnection = data?.connections.data.some((connection) => connection.disabled === true && accounts.some((account) => account.connection_id === connection.id));
  const analyticsTab = ["performance", "accounts", "sectors", "compare", "review"].includes(tab);
  const dataRevision = `${data?.retrieved_at || ""}|${completedRefresh}`;
  const currencies = [...new Set(["GBP", currency, ...all.map((account) => account.currency).filter((value): value is string => !!value)])].sort();
  function chooseAccount(id: string) { setSelected(id); chooseView("performance"); }
  function refreshPortfolio() { setRequest((previous) => ({ count: previous.count + 1, refresh: true })); }
  function retryRequest() { setRequest((previous) => ({ ...previous, count: previous.count + 1 })); }
  function openNotes() { const notes = document.getElementById(analyticsTab ? "investment-analysis-notes" : "investment-data-notes") as HTMLDetailsElement | null; if (notes) { notes.open = true; notes.scrollIntoView({ behavior: "smooth", block: "start" }); } }
  const sourceNotes = <>
    <p>SnapTrade supplies current balances and holdings; saved observations build account history. Broker transactions sync daily.</p>
    <p>Browsing uses saved data. Refresh updates balances, holdings, market prices and exchange rates.</p>
    {!analyticsTab && <p>Portfolio retrieved {data ? portfolioDate(data.retrieved_at, true) : "not yet"}{data?.cache?.saved_at ? `; saved locally ${portfolioDate(data.cache.saved_at, true)}` : ""}.</p>}
    {showingPaper && <p className="text-seablue">Paper accounts contain simulated values and are kept separate from live investments.</p>}
    {data && !data.configured && <div className="space-y-2"><p className="text-amber">Add an investment provider connection in Settings. Stored accounts and recorded history remain available.</p><button className="btn" onClick={() => openFinancePage("settings", undefined, "connections")}>Manage investment connections</button></div>}
    {disabledConnection && <p className="text-amber">A selected brokerage connection is disabled. Review it in SnapTrade Personal; returned figures may be historical.</p>}
    {sourceIssues.length > 0 && <ul className="space-y-2">{sourceIssues.map(({ account, sections }) => <li key={account.id}><p className="font-medium text-foam">{accountLabel(account)}</p><ul>{sections.map(key => {
      const section = account[key], label = { balances: "Cash balances", positions: "Holdings", activities: "Activity", orders: "Orders" }[key];
      const status = section.status === "unsupported" ? "not supported by this connection" : section.status === "error"
        ? section.data.length ? `refresh failed; ${section.data.length} saved ${section.data.length === 1 ? "record" : "records"} retained` : "unavailable"
        : section.truncated ? "partial records" : "using previously saved records";
      return <li key={key} className="break-words">{label}: {status}{section.truncated && section.status === "error" ? " · partial records" : ""}.{section.error && <span className="text-amber"> {section.error}</span>}</li>;
    })}</ul></li>)}</ul>}
    {data?.cache?.refresh_error && <p className="text-amber">Last refresh: {data.cache.refresh_error}</p>}
    {data && connectionRefreshIssue(data.connection_statuses) && <p className="text-amber">{connectionRefreshIssue(data.connection_statuses)}</p>}
    {(data?.error || error) && <p className="text-amber">{data?.error || error}</p>}
    {refreshMessages.map((message, index) => <p key={index} className="text-amber">{message}</p>)}
  </>;

  return <div className="space-y-3">
    <div role="tablist" aria-label="Investment views" className="workspace-nav grid max-w-full grid-cols-4 gap-1 border-b border-line pb-1 sm:flex sm:overflow-x-auto sm:pb-2">
      {TABS.map(([id, label], index) => <button key={id} id={`portfolio-tab-${id}`} ref={(element) => { tabButtons.current[index] = element; }} role="tab" aria-selected={primary === id} aria-controls={`portfolio-panel-${id}`} tabIndex={primary === id ? 0 : -1} className={`min-h-11 min-w-0 rounded-lg px-1 text-xs transition-colors sm:shrink-0 sm:px-3 sm:text-sm ${primary === id ? "bg-abyss font-semibold text-white" : "text-mist hover:text-foam"}`} onClick={() => chooseView(lastView[id])} onKeyDown={(event) => {
        const next = event.key === "ArrowRight" ? (index + 1) % TABS.length : event.key === "ArrowLeft" ? (index + TABS.length - 1) % TABS.length : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : null;
        if (next === null) return; event.preventDefault(); chooseView(lastView[TABS[next][0]]); tabButtons.current[next]?.focus();
      }} aria-label={label}><span className="sm:hidden">{id === "connections" ? "Connect" : label}</span><span className="hidden sm:inline">{label}</span></button>)}
    </div>
    {SUBVIEWS[primary].length > 0 && <div role="group" aria-label={`${TABS.find(([id]) => id === primary)?.[1]} sections`} className="flex gap-1 overflow-x-auto pb-1">{SUBVIEWS[primary].map(([id, label]) => <button key={id} className={`min-h-11 shrink-0 rounded-md px-2.5 text-xs ${tab === id ? "bg-surface2 font-semibold text-foam" : "text-mist hover:text-foam"}`} aria-pressed={tab === id} onClick={() => chooseView(id)}>{label}</button>)}</div>}
    <MobileDisclosure label="Portfolio filters" summary={`${selected === "all" ? "Active accounts" : selected === "paper" ? "Paper accounts" : accounts[0]?.name || "Selected account"} · ${currency} · ${range.toUpperCase()}`}><section aria-label="Portfolio controls" className="flex flex-wrap items-end gap-2">
      <select aria-label="Account selection" className="input min-h-11 min-w-0 flex-1 basis-full !text-base sm:basis-56 sm:!text-sm" value={selected} disabled={!all.length} onChange={(event) => setSelected(event.target.value)}><option value="all">Active accounts ({activeLive.length})</option>{paper.length > 0 && <option value="paper">All paper accounts ({paper.length})</option>}{live.length > 0 && <optgroup label="Live accounts">{live.map((account) => <option key={account.id} value={account.id}>{accountLabel(account)}{sources.some(source => source.id === `connected:${account.id}` && source.archived_by_user) ? " · Archived" : ""}</option>)}</optgroup>}{paper.length > 0 && <optgroup label="Paper accounts">{paper.map((account) => <option key={account.id} value={account.id}>{accountLabel(account)} · Paper</option>)}</optgroup>}{missingSelected && <option value={selected}>Previously selected account unavailable</option>}</select>
      {(analyticsTab || tab === "calendar") && <select aria-label="Investment reporting currency" className="input min-h-11 w-24 !text-base sm:!text-sm" value={currency} onChange={(event) => setCurrency(event.target.value)}>{currencies.map((value) => <option key={value}>{value}</option>)}</select>}
      {["performance", "compare"].includes(tab) && <label className="flex flex-col gap-1 text-[10px] text-mist">Period<select aria-label="Investment price period" className="input min-h-11 w-28 !text-base sm:!text-sm" value={range} onChange={(event) => setRange(event.target.value as InvestmentRange)}>{([['1w', '1 week'], ['1m', '1 month'], ['3m', '3 months'], ['6m', '6 months'], ['ytd', 'This year'], ['1y', '1 year'], ['all', 'All history']] as const).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
      <button className="btn min-h-11" disabled={loading || coolingDown} onClick={refreshPortfolio} title={coolingDown ? "Refresh is available one minute after the last request" : "Update brokerage, market and reference-rate data"}>{loading ? request.refresh ? "Refreshing…" : "Loading…" : "↻ Refresh"}</button>
      {!analyticsTab && <button className="min-h-11 px-2 text-sm text-mist underline" onClick={openNotes}>Data details</button>}
    </section>
    <div aria-label="SnapTrade refresh status" className="space-y-1 text-xs leading-relaxed text-mist">
      {data ? <><p><span className="font-medium text-foam">Source: SnapTrade</span> · {data.cache?.last_successful_refresh_at ? <>Last successful retrieval <time dateTime={data.cache.last_successful_refresh_at}>{portfolioDate(data.cache.last_successful_refresh_at, true)}</time>.</> : "Last successful retrieval time is not reported."}</p>
      <p>{refreshScheduleDescription(data)}</p>
      {data.cache?.stale && <p className="text-amber">{data.cache.refresh_error ? "Last refresh incomplete. Saved data remains available." : "Saved data may be out of date."} Check data details for the last refresh result.</p>}</>
      : loading ? <LoadingRegion label="Loading saved refresh details…"><div className="space-y-1"><SkeletonBlock className="h-5 w-2/3" /><SkeletonBlock className="h-5 w-3/4" /></div></LoadingRegion> : <p>Saved refresh details are unavailable.</p>}
    </div>
    </MobileDisclosure>
    {data?.cache?.stale && <p className="text-xs text-amber sm:hidden">Saved investment data may be out of date. Open portfolio filters for refresh details.</p>}
    {showingPaper && <p className="text-xs font-medium text-seablue">Paper accounts · simulated investments</p>}
    {(error || refreshMessages.length > 0) && <p role="alert" className="text-xs text-amber">{error ? "Update failed. Saved data remains visible." : "Refresh incomplete. Saved data remains available."} <button className="underline" onClick={openNotes}>Details</button>{!coolingDown && <button className="ml-2 underline" onClick={retryRequest}>Retry</button>}</p>}
    {loading && !data && <PageSkeleton variant={analyticsTab ? "portfolio" : "table"} label="Loading your saved portfolio…" />}
    {data && <div aria-busy={loading} className="async-content content-enter">
      <section id={`portfolio-panel-${primary}`} role="tabpanel" aria-labelledby={`portfolio-tab-${primary}`} tabIndex={0} className="min-w-0 outline-hidden">
        {tab === "connections" ? <ConnectionStatus accounts={accounts} connections={data.connections} selectedAccount={selected} currency={currency} revision={dataRevision} />
          : tab === "saved-activity" ? <SavedInvestmentActivity accounts={accounts} scope={selected} revision={dataRevision} />
          : tab === "calendar" ? <InvestmentCalendar accountIds={accounts.map((account) => account.id)} currency={currency} revision={dataRevision} />
          : !accounts.length ? <Card><Empty>{missingSelected ? "This account was not returned. Choose another account above." : !all.length ? "No investment accounts are available in the saved portfolio." : selected === "all" && paper.length > 0 ? "No live accounts are available. Choose Paper accounts above to see simulated holdings." : "No accounts match this selection."}</Empty>{!all.length && <div className="mt-3 flex justify-center"><button className="btn-primary" onClick={() => openFinancePage("settings", undefined, "connections")}>{data.configured ? "Check investment connections" : "Set up an investment connection"}</button></div>}</Card>
          : analyticsTab ? <InvestmentWorkspace mode={tab as InvestmentWorkspaceMode} accountIds={accounts.map((account) => account.id)} currency={currency} range={range} revision={dataRevision} extraNotes={sourceNotes} returnRates={accounts.length === 1 ? accounts[0].return_rates : undefined} onOpenAccount={chooseAccount} onShowPerformance={() => chooseView("performance")} onNavigate={(next) => chooseView(next)} />
          : tab === "activity" ? <div className="space-y-3">{dividendCount > 0 && <div className="flex gap-2" role="group" aria-label="Activity focus"><button className={`btn ${activityScope === "all" ? "bg-surface2 font-semibold" : ""}`} aria-pressed={activityScope === "all"} onClick={() => setActivityScope("all")}>All activity · {activityCount}</button><button className={`btn ${activityScope === "dividends" ? "bg-surface2 font-semibold" : ""}`} aria-pressed={activityScope === "dividends"} onClick={() => setActivityScope("dividends")}>Dividends · {dividendCount}</button></div>}<PortfolioActivity key={activityScope} accounts={activityAccounts} /></div> : <PortfolioOrders accounts={accounts} />}
      </section>
      {!analyticsTab && <details id="investment-data-notes" className="mt-5 rounded-xl border border-line bg-white px-4 text-xs text-mist"><summary className="cursor-pointer py-3 font-medium">Data coverage &amp; sources · {selectedLabel}</summary><div className="space-y-3 pb-4">{sourceNotes}</div></details>}
    </div>}
  </div>;
}
