import { syncBanking } from "./bankingApi";
import { fetchConnectedPortfolio } from "./snaptradeApi";
import { refreshWealthRates } from "./wealthApi";
import { connectionRefreshIssue, portfolioRefreshIssue } from "./providerRefresh";

export const WEALTH_REFRESH_SOURCES = ["banks", "investments", "rates"] as const;
export type WealthRefreshSource = typeof WEALTH_REFRESH_SOURCES[number];
export type WealthRefreshOutcome = { status: "updated" | "failed" | "unavailable"; message?: string };
export type WealthRefreshTasks = Record<WealthRefreshSource, (signal: AbortSignal) => Promise<WealthRefreshOutcome>>;
export const WEALTH_REFRESH_LABELS: Record<WealthRefreshSource, string> = { banks: "Banks", investments: "Investments", rates: "Exchange rates" };

const tasks: WealthRefreshTasks = {
  banks: async signal => {
    const data = await syncBanking(signal);
    if (!data.configured) return { status: "unavailable", message: "Bank connections are not configured. Saved history remains available." };
    const issue = connectionRefreshIssue(data.connection_statuses);
    if (issue) return { status: "failed", message: issue };
    if (data.sync.error) return { status: "failed", message: data.sync.error };
    const failed = data.accounts.filter(account => account.last_error && account.present !== false).length;
    if (failed) return { status: "failed", message: `${failed} account update${failed === 1 ? "" : "s"} failed. Saved values are retained.` };
    return { status: "updated" };
  },
  investments: async signal => {
    const data = await fetchConnectedPortfolio(signal, true);
    if (!data.configured) return { status: "unavailable", message: "Investment connections are not configured. Saved history remains available." };
    const issue = portfolioRefreshIssue(data);
    return issue ? { status: "failed", message: issue } : { status: "updated", message: "Latest available account balances saved. Price and history coverage are reported separately in Investments." };
  },
  rates: async signal => {
    const result = await refreshWealthRates(signal);
    return result.missing && result.missing > 0
      ? { status: "failed", message: "Some dated exchange rates remain unavailable. Saved rates are retained where available." }
      : { status: "updated" };
  },
};

/** Provider updates are independent; FX discovers currencies from their saved results. */
export async function refreshWealthSources(
  sources: readonly WealthRefreshSource[], signal: AbortSignal,
  onStart: (source: WealthRefreshSource) => void,
  onResult: (source: WealthRefreshSource, result: WealthRefreshOutcome) => void,
  implementations: WealthRefreshTasks = tasks,
): Promise<void> {
  const requested = [...new Set(sources)];
  async function run(source: WealthRefreshSource) {
    if (signal.aborted) return;
    onStart(source);
    try {
      const result = await implementations[source](signal);
      if (!signal.aborted) onResult(source, result);
    } catch (error) {
      if (!signal.aborted) onResult(source, { status: "failed", message: error instanceof Error ? error.message : "Update failed. Saved values remain available." });
    }
  }
  await Promise.all(requested.filter(source => source !== "rates").map(run));
  if (requested.includes("rates")) await run("rates");
}
