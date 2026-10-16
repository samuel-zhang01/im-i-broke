import type { ConnectedPortfolioData } from "./snaptradeApi";

export type ProviderSyncStatus = { id: string; label: string; enabled: boolean; status: string; last_synced_at: string | null; last_error: string | null };

export function connectionRefreshIssue(statuses: ProviderSyncStatus[] = []) {
  const incomplete = statuses.filter(row => row.enabled && (row.last_error || !row.last_synced_at));
  return incomplete.length ? `${incomplete.length} connection${incomplete.length === 1 ? " has" : "s have"} not refreshed successfully. Review ${incomplete.map(row => row.label).join(", ")} in Provider connections.` : null;
}

export function portfolioRefreshIssue(data: ConnectedPortfolioData) {
  if (!data.configured) return "Investment connections are not configured. Saved history remains available.";
  const connectionIssue = connectionRefreshIssue(data.connection_statuses);
  if (connectionIssue) return connectionIssue;
  if (data.error || data.cache?.refresh_error) return data.error || data.cache?.refresh_error || null;
  const incomplete = data.accounts.some(account => [account.balances, account.positions]
    .some(section => section.status === "error" || section.stale || section.truncated));
  return incomplete ? "Some investment balances or holdings could not update. Saved values are retained." : null;
}
