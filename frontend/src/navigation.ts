export const FINANCE_PAGES = ["net-worth", "connected-portfolio", "banking", "overview", "analytics", "transactions", "recurring", "planning", "merchants", "anomalies", "transfers", "advice", "investments", "settings"] as const;
export type FinancePage = typeof FINANCE_PAGES[number];
export type FinanceDestination = { page: FinancePage; accountId?: string; query?: string };
const NAVIGATE = "ledger:navigate";

export function validFinancePage(value: unknown): value is FinancePage {
  return typeof value === "string" && (FINANCE_PAGES as readonly string[]).includes(value);
}

export function financePageFromHash(hash: string): FinancePage | null {
  const page = hash.replace(/^#\/?/, "");
  // Retired upload bookmarks open the connected account workspace.
  if (page === "import") return "banking";
  return validFinancePage(page) ? page : null;
}

export function openFinancePage(page: FinancePage, accountId?: string, query?: string) {
  window.dispatchEvent(new CustomEvent<FinanceDestination>(NAVIGATE, { detail: { page, accountId, query } }));
}

export function watchFinanceNavigation(onNavigate: (destination: FinanceDestination) => void) {
  const listener = (event: Event) => {
    const destination = (event as CustomEvent<FinanceDestination>).detail;
    if (destination && validFinancePage(destination.page)) onNavigate({ ...destination, query: typeof destination.query === "string" ? destination.query.slice(0, 200) : undefined });
  };
  window.addEventListener(NAVIGATE, listener);
  return () => window.removeEventListener(NAVIGATE, listener);
}

export const BANKING_WORKSPACES = [
  { label: "Activity", home: "transactions", pages: [
    { id: "transactions", label: "Transactions" }, { id: "anomalies", label: "Review" },
    { id: "transfers", label: "Transfers" }, { id: "merchants", label: "Names" },
  ] },
  { label: "Plan", home: "recurring", pages: [
    { id: "recurring", label: "Regular payments" }, { id: "planning", label: "Scenarios" },
    { id: "advice", label: "Savings ideas" },
  ] },
] as const;

export function bankingWorkspace(page: string) {
  return BANKING_WORKSPACES.find(workspace => workspace.pages.some(item => item.id === page));
}
export function primaryFinancePage(page: FinancePage): FinancePage {
  return bankingWorkspace(page)?.home ?? page;
}
