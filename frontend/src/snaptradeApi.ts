import { authRequest } from "./authRequest";
import { dataChanged } from "./dataEvents";
import type { ProviderSyncStatus } from "./providerRefresh";

export type PortfolioSection<T> = {
  status: "ok" | "empty" | "error" | "unsupported";
  data: T[];
  error: string | null;
  truncated: boolean;
  stale?: boolean;
  cached_at?: string | null;
  as_of?: string | null;
};
export type ConnectedBalance = { currency: string | null; cash: number | null; buying_power: number | null };
export type ConnectedPosition = {
  id: string; symbol: string; name: string; asset_type: string | null;
  quantity: number | null; price: number | null; value: number | null;
  currency: string | null; average_cost: number | null; unrealized_pnl: number | null; is_cash: boolean;
};
export type ConnectedOrder = {
  id: string; symbol: string; action: string | null; status: string | null;
  quantity: number | null; filled_quantity: number | null; limit_price: number | null;
  execution_price: number | null; currency: string | null; placed_at: string | null;
  updated_at: string | null; type: string | null;
};
export type ConnectedActivity = {
  id: string; type: string; description: string; symbol: string | null;
  amount: number | null; currency: string | null; quantity: number | null; date: string | null;
};
export type BrokerageConnection = {
  id: string; name: string; institution: string; disabled: boolean | null;
  is_read_only: boolean | null; created_at: string | null;
};
export type ConnectedAccount = {
  id: string; name: string; institution: string; number_suffix: string | null;
  type: string | null; currency: string | null; total_value: number | null;
  connection_id: string | null; holdings_synced_at: string | null;
  activities_synced_at: string | null; initial_sync_complete: boolean | null;
  is_paper?: boolean; holdings_unavailable?: boolean;
  balances: PortfolioSection<ConnectedBalance>; positions: PortfolioSection<ConnectedPosition>;
  orders: PortfolioSection<ConnectedOrder>; activities: PortfolioSection<ConnectedActivity>;
  history?: PortfolioSection<Record<string, unknown>>;
  return_rates?: PortfolioSection<{ timeframe: string; return_percent: number; as_of: string | null; method: "unknown"; source: "broker_reported" }>;
};
export type ConnectedPortfolioData = {
  connection_statuses?: ProviderSyncStatus[];
  configured: boolean; retrieved_at: string; error: string | null;
  accounts: ConnectedAccount[]; connections: PortfolioSection<BrokerageConnection>;
  refresh_schedule?: { enabled: boolean; interval_seconds: number };
  cache?: {
    saved_at: string | null;
    last_attempt_at: string | null;
    last_successful_refresh_at: string | null;
    is_cached: boolean;
    stale: boolean;
    refresh_error: string | null;
  };
};

export async function fetchConnectedPortfolio(signal?: AbortSignal, refresh = false): Promise<ConnectedPortfolioData> {
  const response = await authRequest(`/api/investments/connected${refresh ? "/refresh" : ""}`, { signal, ...(refresh ? { method: "POST" } : {}) });
  // Never surface raw upstream response bodies, which can contain account data.
  if (!response.ok) {
    if (response.status === 401) throw new Error("Your session has expired. Sign in to continue.");
    if (response.status === 429) throw new Error("Too many refreshes. Wait a moment and try again.");
    throw new Error(`Couldn’t retrieve your connected portfolio (${response.status}). Please try again.`);
  }
  const result: ConnectedPortfolioData = await response.json();
  if (!Array.isArray(result.accounts) || !result.connections || typeof result.configured !== "boolean") {
    throw new Error("The portfolio response was incomplete. Please try again.");
  }
  if (refresh) dataChanged("investments/connected");
  return result;
}

export type PortfolioSnapshotSummary = { id: string | number; retrieved_at: string; account_count: number; position_count: number };
export type RecordedPortfolioSnapshot = {
  id: string | number; retrieved_at: string;
  accounts: {
    id: string; name: string; institution: string; currency: string | null;
    total_value: number | null; is_paper: boolean; observed_at: string | null;
    source_time_known: boolean; positions: PortfolioSection<ConnectedPosition>;
  }[];
};

export async function fetchPortfolioSnapshots(signal?: AbortSignal): Promise<PortfolioSnapshotSummary[]> {
  const response = await authRequest("/api/investments/connected/snapshots", { signal });
  if (!response.ok) throw new Error("Saved holdings dates could not be loaded. Latest holdings remain available.");
  const result = await response.json();
  if (!Array.isArray(result.snapshots)) throw new Error("The saved holdings list was incomplete.");
  return result.snapshots;
}

export async function fetchPortfolioSnapshot(id: string, signal?: AbortSignal): Promise<RecordedPortfolioSnapshot> {
  const response = await authRequest(`/api/investments/connected/snapshots/${encodeURIComponent(id)}`, { signal });
  if (!response.ok) throw new Error("This saved holdings snapshot could not be loaded. Choose another date or Latest saved holdings.");
  const result = await response.json();
  if (!Array.isArray(result.accounts)) throw new Error("The saved holdings snapshot was incomplete.");
  return result;
}
