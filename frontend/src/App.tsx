import { Heading } from "./components/Heading";
import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import BrandMark from "./components/BrandMark";
import Icon from "./components/Icon";
import { PageSkeleton } from "./components/Skeleton";
import { watchRecurringNavigation } from "./recurring";
import { FINANCE_PAGES, financePageFromHash, validFinancePage, watchFinanceNavigation, bankingWorkspace, primaryFinancePage } from "./navigation";
const Settings = lazy(() => import("./views/Settings"));
const Overview = lazy(() => import("./views/Overview"));
const NetWorth = lazy(() => import("./views/NetWorth"));
const Analytics = lazy(() => import("./views/Analytics"));
const ConnectedPortfolio = lazy(() => import("./views/ConnectedPortfolio"));
const Transactions = lazy(() => import("./views/Transactions"));
const Recurring = lazy(() => import("./views/Recurring"));
const Transfers = lazy(() => import("./views/Transfers"));
const Anomalies = lazy(() => import("./views/Anomalies"));
const Advice = lazy(() => import("./views/Advice"));
const Planning = lazy(() => import("./views/Planning"));
const Merchants = lazy(() => import("./views/Merchants"));
const Banking = lazy(() => import("./views/Banking"));
import { watchBankingNavigation } from "./banking";
import { useWorkspaceScroll } from "./workspaceScroll";

const TABS = [
  { id: "settings", label: "Settings", section: "Make it yours", path: "M12 8a4 4 0 100 8 4 4 0 000-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2", el: <Settings /> },
  {
    id: "overview",
    label: "Overview",
    section: "Your finances",
    path: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
    el: <Overview />,
  },
  {
    id: "banking",
    label: "Banks & accounts",
    section: "Your finances",
    path: "M3 10h18M12 3l9 5H3l9-5M5 10v8m7-8v8m7-8v8M3 21h18",
    el: <Banking />,
  },
  {
    id: "net-worth",
    label: "Net assets",
    section: "Your finances",
    path: "M3 18l5-5 4 2 8-10M15 5h5v5M3 21h18",
    el: <NetWorth />,
  },
  {
    id: "analytics",
    label: "Spending",
    section: "Your finances",
    path: "M4 20V10m8 10V4m8 16v-7",
    el: <Analytics />,
  },
  {
    id: "transactions",
    label: "Transactions",
    section: "Your finances",
    path: "M8 5h13M8 12h13M8 19h13M3 5h.01M3 12h.01M3 19h.01",
    el: <Transactions />,
  },
  {
    id: "planning",
    label: "Planning",
    section: "Your finances",
    path: "M3 17l6-6 4 4 8-11M15 4h6v6",
    el: <Planning />,
  },
  {
    id: "connected-portfolio",
    label: "Investments",
    section: "Your finances",
    path: "M3 17l6-6 4 4 8-11M15 4h6v6M3 21h18",
    el: <ConnectedPortfolio />,
  },
  {
    id: "merchants",
    label: "People & merchants",
    section: "Make it yours",
    path: "M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 3a4 4 0 100 8 4 4 0 000-8M22 21v-2a4 4 0 00-3-3.87",
    el: <Merchants />,
  },
  {
    id: "anomalies",
    label: "Review",
    section: "Make it yours",
    path: "M9 3h6l6 6v6l-6 6H9l-6-6V9zM12 7v6M12 17h.01",
    el: <Anomalies />,
  },
  {
    id: "transfers",
    label: "Transfers",
    section: "Make it yours",
    path: "M3 7h17l-4-4M21 17H4l4 4",
    el: <Transfers />,
  },
  {
    id: "recurring",
    label: "Recurring",
    section: "Make it yours",
    path: "M20 7a9 9 0 00-15-2L2 8m0-6v6h6M4 17a9 9 0 0015 2l3-3m0 6v-6h-6",
    el: <Recurring />,
  },
  {
    id: "advice",
    label: "Savings",
    section: "Make it yours",
    path: "M12 3v3M5 7l2 2M19 7l-2 2M8 17h8M9 21h6M8 14a6 6 0 118 0v3H8z",
    el: <Advice />,
  },
  {
    id: "investments",
    label: "Investments",
    section: "Make it yours",
    path: "M4 3h16v18H4zM8 7h8M8 12h8M8 17h5",
    el: <ConnectedPortfolio />,
  },

] as const;
const ORDERED_TABS = FINANCE_PAGES.map((id) => TABS.find((tab) => tab.id === id)!);
// Old statement links remain valid; the archive now lives inside Investments.
const NAV_TABS = ORDERED_TABS.filter((tab) => tab.id !== "investments" && primaryFinancePage(tab.id) === tab.id).map(tab => ({ ...tab, label: bankingWorkspace(tab.id)?.label ?? tab.label }));
const QUICK_PAGES = ["net-worth", "connected-portfolio", "banking", "transactions"];
const sectionFor = (id: string) => ["net-worth", "connected-portfolio", "investments", "banking"].includes(id) ? "Your wealth" : bankingWorkspace(id)?.label ?? (["overview", "analytics"].includes(id) ? "Day to day" : "Tools & history");
const navigationSection = (id: string) => bankingWorkspace(id) ? "Day to day" : sectionFor(id);

class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <div role="alert" className="rounded-xl border border-line bg-white p-5">
      <p className="font-medium text-foam">This page couldn’t open.</p>
      <p className="mt-1 text-sm text-mist">Reload to get the latest version of Am I Broke? Your saved records are safe.</p>
      <button className="btn mt-4" onClick={() => window.location.reload()}>Reload page</button>
    </div>;
    return this.props.children;
  }
}

export default function App() {
  const [investmentAccount, setInvestmentAccount] = useState<string | undefined>();
  const [bankAccount, setBankAccount] = useState<string | undefined>();
  const [pageSearch, setPageSearch] = useState<string | undefined>();
  const [recurringPayment, setRecurringPayment] = useState<number | null>(null);
  const [tab, setTab] = useState<string>(() => {
    const linked = financePageFromHash(window.location.hash);
    if (linked) return linked;
    try {
      const saved = sessionStorage.getItem("ledger.tab") || localStorage.getItem("ledger.tab");
      return saved === "import" ? "banking" : validFinancePage(saved) ? saved : "net-worth";
    } catch { return "net-worth"; }
  });
  const currentTab = useRef(tab);
  const topNavigation = useRef<HTMLElement>(null);
  const quickNavigation = useRef<HTMLElement>(null);
  const [navigation, setNavigation] = useState({ sequence: 0, restore: true });
  const rememberScroll = useWorkspaceScroll(tab, navigation);
  const active = ORDERED_TABS.find((t) => t.id === tab) ?? ORDERED_TABS[0];
  const navigationId = active.id === "investments" ? "connected-portfolio" : primaryFinancePage(active.id);
  const workspace = bankingWorkspace(active.id);
  useEffect(() => {
    const root = document.documentElement;
    const updateInsets = () => {
      root.style.setProperty("--navigation-top", `${topNavigation.current?.getBoundingClientRect().height ?? 72}px`);
      root.style.setProperty("--navigation-bottom", `${quickNavigation.current?.getBoundingClientRect().height ?? 72}px`);
    };
    const observer = new ResizeObserver(updateInsets);
    if (topNavigation.current) observer.observe(topNavigation.current);
    if (quickNavigation.current) observer.observe(quickNavigation.current);
    updateInsets();
    return () => {
      observer.disconnect();
      root.style.removeProperty("--navigation-top");
      root.style.removeProperty("--navigation-bottom");
    };
  }, []);
  useEffect(() => {
    document.title = `${active.label} · Am I Broke?`;
    return () => { document.title = "Am I Broke? · Personal finance"; };
  }, [active.label]);
  function go(id: string, record = true, restoreScroll = true) {
    if (!validFinancePage(id)) return;
    rememberScroll();
    setNavigation(previous => ({ sequence: previous.sequence + 1, restore: restoreScroll }));
    setBankAccount(undefined);
    setInvestmentAccount(undefined);
    setRecurringPayment(null);
    setPageSearch(undefined);
    currentTab.current = id;
    setTab(id);
    window.scrollTo({ top: 0, behavior: "auto" });
    if (record && window.location.hash !== `#${id}`) window.history.pushState(null, "", `#${id}`);
    try { sessionStorage.setItem("ledger.tab", id); } catch { /* storage may be disabled */ }
  }
  useEffect(() => {
    const canonicalize = () => {
      if (window.location.hash !== `#${active.id}`) window.history.replaceState(null, "", `#${active.id}`);
    };
    canonicalize();
    const restore = () => {
      const page = financePageFromHash(window.location.hash) || "net-worth";
      if (window.location.hash !== `#${page}`) window.history.replaceState(null, "", `#${page}`);
      if (page !== currentTab.current) go(page, false);
    };
    window.addEventListener("popstate", restore);
    window.addEventListener("hashchange", restore);
    return () => { window.removeEventListener("popstate", restore); window.removeEventListener("hashchange", restore); };
  }, []);
  useEffect(() => watchFinanceNavigation(({ page, accountId, query }) => {
    go(page, true, accountId === undefined && query === undefined);
    setPageSearch(query);
    if (page === "banking") setBankAccount(accountId);
    if (page === "connected-portfolio") setInvestmentAccount(accountId);
  }), []);
  useEffect(() => watchRecurringNavigation((id) => {
    go("recurring", true, false);
    setRecurringPayment(id);
  }), []);
  useEffect(() => watchBankingNavigation((id) => {
    go("banking", true, false);
    setBankAccount(id);
  }), []);
  return (
    <div className="min-h-screen lg:pl-56">
      <a
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          const main = document.getElementById("main-content");
          main?.focus({ preventScroll: true });
          main?.scrollIntoView({ behavior: "auto", block: "start" });
        }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-white focus:p-3"
      >
        Skip to content
      </a>
      <aside ref={topNavigation} className="sticky top-0 z-30 bg-abyss text-[#e6eae3] lg:fixed lg:inset-y-0 lg:left-0 lg:w-56 lg:overflow-y-auto">
        <div className="flex flex-wrap items-center gap-2 px-4 py-3 lg:gap-3 lg:px-5 lg:pb-3 lg:pt-6">
          <div className="flex min-w-0 max-w-full shrink-0 items-center gap-2 lg:gap-3">
          <BrandMark className="h-8 w-8 lg:h-9 lg:w-9" />
          <div className="min-w-0">
            <div className="text-lg font-semibold tracking-tight">
              Am I Broke<span className="text-[#db704a]">?</span>
            </div>
            <div className="hidden text-[10px] uppercase tracking-[.15em] text-[#a6b2a6] lg:block">
              Personal finance
            </div>
          </div>
          </div>
          <label className="relative ml-auto min-w-0 flex-1 basis-28 lg:hidden">
            <span className="sr-only">Go to page</span>
            <select className="navigation-select min-h-11 w-full min-w-0 appearance-none rounded-lg border border-[#536052] bg-[#363e37] py-2 pl-3 pr-9 text-sm text-white [color-scheme:dark]" value={navigationId} onChange={(e) => go(e.target.value)}>
              {[...new Set(NAV_TABS.map((t) => navigationSection(t.id)))].map((section) => <optgroup key={section} label={section}>
                {NAV_TABS.filter((t) => navigationSection(t.id) === section).map((t) => <option key={t.id} value={t.id}>{t.id === "banking" ? "Banks" : t.id === "merchants" ? "Merchants" : t.label}</option>)}
              </optgroup>)}
            </select>
            <svg aria-hidden="true" className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6" /></svg>
          </label>
        </div>
        <nav
          aria-label="Main navigation"
          className="hidden px-3 pb-3 lg:block lg:space-y-0.5"
        >
          {NAV_TABS.map((t, i) => (
            <div key={t.id} className="shrink-0">
              {(i === 0 || navigationSection(NAV_TABS[i - 1].id) !== navigationSection(t.id)) && (
                <p className="mb-2 mt-5 hidden px-3 text-[10px] font-semibold uppercase tracking-[.15em] text-[#a6b2a6] lg:block">
                  {navigationSection(t.id)}
                </p>
              )}
              <button
                aria-current={navigationId === t.id ? "page" : undefined}
                onClick={() => go(t.id)}
                className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-[13px] transition-colors ${navigationId === t.id ? "bg-[#363e37] font-semibold text-white" : "text-[#bdc7bd] hover:bg-[#303731] hover:text-white"}`}
              >
                <svg
                  aria-hidden="true"
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke={navigationId === t.id ? "#e5a080" : "currentColor"}
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d={t.path} />
                </svg>
                {t.label}
              </button>
            </div>
          ))}
        </nav>
        <div className="mx-6 mt-6 hidden border-t border-[#414a40] py-5 text-xs leading-relaxed text-[#a6b2a6] lg:block">
          <span className="mb-1 block font-medium text-[#d5ded2]">
            Your workspace
          </span>
          Saved history and corrections stay with this installation.
        </div>
      </aside>
      <div className="mx-auto min-w-0 max-w-[1500px] px-4 pb-24 pt-4 sm:px-6 lg:pb-8 lg:pt-5">
        <header className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-line pb-3">
          <Heading id="page-title" level={1} variant="page" className="text-foam">{active.label}</Heading>
          {workspace?.label === "Activity" && <button className="btn ml-auto" onClick={() => go("analytics")}><Icon name="chart" className="hidden sm:block" />Spending stats</button>}
          <div className="hidden items-center gap-3 sm:flex">
            {!workspace && <span className="text-xs text-mist">{sectionFor(active.id)}</span>}
          </div>
        </header>
        <main id="main-content" aria-labelledby="page-title" className="min-w-0 scroll-mt-24" tabIndex={-1}
          onFocusCapture={event => {
            const target = event.target as HTMLElement;
            if (target !== event.currentTarget && !target.closest('[role="dialog"], dialog') && window.matchMedia("(max-width: 1023px)").matches) {
              target.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "auto" });
            }
          }}>
          {workspace && <nav aria-label={`${workspace.label} sections`} className={`workspace-nav mb-5 grid gap-1 rounded-xl border border-line bg-white p-1 ${workspace.pages.length === 4 ? "grid-cols-4" : "grid-cols-3"}`}>
            {workspace.pages.map(item => <button key={item.id} aria-label={item.label} aria-current={active.id === item.id ? "page" : undefined} onClick={() => go(item.id)} className={`min-h-11 min-w-0 rounded-lg px-1.5 py-2 text-center text-xs font-medium transition sm:px-3 sm:text-sm ${active.id === item.id ? "bg-abyss text-white" : "text-mist hover:bg-surface2 hover:text-foam"}`}>{item.id === "transactions" ? <><span className="sm:hidden">All</span><span className="hidden sm:inline">{item.label}</span></> : item.label}</button>)}
          </nav>}
          <PageBoundary key={`${active.id}:${navigation.sequence}`}>
          <Suspense fallback={<PageSkeleton label={`Opening ${active.label.toLowerCase()}…`} variant={active.id === "net-worth" ? "networth" : active.id === "banking" ? "banking" : active.id === "connected-portfolio" ? "portfolio" : ["transactions", "transfers", "merchants", "recurring"].includes(active.id) ? "table" : "dashboard"} />}>
          {active.id === "settings" ? <Settings initialSection={pageSearch} /> : active.id === "overview" ? <Overview onOpenNetWorth={() => go("net-worth")} onOpenInvestments={() => go("connected-portfolio")} /> : active.id === "connected-portfolio" ? <ConnectedPortfolio initialAccountId={investmentAccount} /> : active.id === "banking" ? <Banking initialAccountId={bankAccount} /> : active.id === "recurring" ? <Recurring transactionId={recurringPayment} initialSearch={pageSearch} /> : active.id === "transactions" ? <Transactions initialSearch={pageSearch} /> : active.id === "merchants" ? <Merchants initialSearch={pageSearch} /> : active.el}
          </Suspense>
          </PageBoundary>
        </main>
        <footer className="mt-10 flex flex-wrap justify-between gap-2 border-t border-line pt-4 text-xs text-mist">
          <span>{active.id === "net-worth" ? "Am I Broke? · Net assets over time" : active.id === "connected-portfolio" ? "Am I Broke? · Read-only connected investing" : "Am I Broke? · Accounts and activity"}</span>
          <span>Accounts, activity and history</span>
        </footer>
      </div>
      <nav ref={quickNavigation} aria-label="Quick navigation" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-line bg-white/95 px-2 pt-1 backdrop-blur lg:hidden" style={{ paddingBottom: "max(6px, env(safe-area-inset-bottom))" }}>
        {QUICK_PAGES.map(id => { const item = NAV_TABS.find(t => t.id === id)!; return <button key={id} aria-current={(navigationId === id || id === "transactions" && workspace?.label === "Activity") ? "page" : undefined} onClick={() => go(id)} className={`flex min-h-14 flex-col items-center justify-center gap-1 rounded-lg px-1 text-xs font-medium ${(navigationId === id || id === "transactions" && workspace?.label === "Activity") ? "text-teal" : "text-mist"}`}>
          <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={item.path} /></svg>
          <span className="min-w-0 max-w-full [overflow-wrap:anywhere]">{id === "banking" ? "Banks" : item.label}</span>
        </button>; })}
      </nav>
    </div>
  );
}
