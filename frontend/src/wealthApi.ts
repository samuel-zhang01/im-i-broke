import { accountSelectionRequest } from "./accountSelectionRequest";
import { authRequest } from "./authRequest";
import { dataChanged } from "./dataEvents";

export type WealthRange = "1w" | "1m" | "3m" | "6m" | "ytd" | "1y" | "all";
export type WealthBalanceCheck = {
  status: "discrepancy" | "statement_only" | "unverified" | "no_discrepancy" | "available_credit";
  requires_review?: boolean;
  message: string; currency: string; statement_date: string | null;
  statement_balance: number | null; transaction_estimate: number | null;
  reported_balance: number | null; difference: number | null;
  pending_amount: number; pending_count: number; review_count: number;
  potential_repayments: { date: string; amount: number }[]; bank_as_of: string | null;
};
export type BalanceValuation = {
  basis: "reported" | "statement_activity_estimate" | "unknown" | "available_credit";
  estimated: boolean; current_unknown: boolean;
  effective_value: number | null; effective_as_of: string | null;
  reported_balance: number | null; reported_as_of: string | null;
  reported_source?: "statement" | "lunchflow" | "manual";
  explanation: string; pending_amount?: number; balance_check?: WealthBalanceCheck;
  available_credit?: number; credit_limit?: number; credit_limit_confirmed_at?: string;
};
export type WealthHistoryAlternative = {
  source_id: string; name: string; start_date: string | null; end_date: string | null;
  valued_days: number; reconciled_holdings: number; quantity_check_date: string;
  identity_link_required: true;
};
export type WealthAccount = {
  id: string; name: string; institution: string; kind: "cash" | "credit" | "investment";
  source: "statement" | "snaptrade" | "lunchflow"; currency: string | null; default_included: boolean;
  balance_source?: "manual"; ownership_share?: number | null;
  exclusion_reason: string | null; native_value: number | null; value: number | null;
  reported_native_value?: number | null; reported_value?: number | null; valuation?: BalanceValuation;
  as_of: string | null; stale: boolean; estimated: boolean; history_points: number;
  canonical_id?: string; source_ids?: string[]; linked_to?: string | null;
  source_status?: "active" | "linked";
  archived?: boolean; archived_by_user?: boolean; history_only?: boolean;
  balance_check?: WealthBalanceCheck;
  history_provider_conflict?: { dates: number; first_date: string; threshold_pct: number };
  history_discontinuities?: number;
  history_reconstruction?: Array<{ days?: number; valued_days?: number; start_date?: string | null; end_date?: string | null; [key: string]: unknown }>;
  history_alternatives?: WealthHistoryAlternative[];
  drilldown?: { page: "banking" | "connected-portfolio" | "investments"; account_id: string | null };
};
export type WealthPoint = {
  observed_at?: string | null;
  date: string; net_worth: number | null; assets: number | null; liabilities: number | null;
  known_accounts: number; missing_accounts: number; estimated: boolean; carried_accounts: number;
  coverage_key?: string;
  complete?: boolean;
  valuation_basis?: "daily_holdings" | "provider_estimate" | "recorded_balance" | "mixed";
  valuation_key?: string;
  reconstructed_accounts?: number;
  source_break?: boolean;
};
export type WealthHistory = {
  mode: "net_assets" | "comparable_cohort"; account_ids: string[]; excluded_account_ids: string[];
  account_count: number; selected_accounts: number; start_date: string | null; end_date: string | null;
  current_value: number | null; end_value?: number | null; change: number | null; change_pct: number | null;
  change_comparable: boolean; points: WealthPoint[]; coverage_note: string;
  net_start_date?: string | null; net_points?: WealthPoint[];
  investment_history?: { account_ids: string[]; start_date: string | null; end_date: string | null; points: WealthPoint[] };
  cohort?: { account_ids: string[]; excluded_account_ids: string[]; points: WealthPoint[] };
  continuity?: { last_continuous_date: string | null; recorded_start_date: string | null; recorded_end_date: string | null; recorded_days: number;
    gaps: Array<{ start_date: string; end_date: string; days: number; reason: string; account_ids: string[] }> };
  account_coverage?: WealthHistoryAccountCoverage[];
  coverage_summary?: {
    included_accounts: number; included_nonzero_accounts: number; included_zero_accounts: number;
    included_unknown_accounts: number; excluded_nonzero_accounts: number; excluded_zero_accounts: number;
    excluded_unknown_accounts: number;
  };
};
export type WealthHistoryAccountCoverage = {
  account_id: string; included: boolean;
  reason: "included" | "zero_balance" | "current_value_unknown" | "insufficient_history" | "shorter_shared_history";
  current_value: number | null; latest_observation_date: string | null;
  history_end_date: string | null; history_end_value: number | null;
  history_valuation_basis: NonNullable<WealthPoint["valuation_basis"]> | null;
  provider_conflict_dates: number; source_discontinuities: number;
};
export type WealthData = {
  generated_at: string; currency: string; currencies: string[]; range: WealthRange;
  selected_account_ids: string[]; accounts: WealthAccount[];
  summary: {
    net_worth: number | null; assets: number | null; liabilities: number | null;
    known_accounts: number; selected_accounts: number; missing_accounts: number; stale_accounts: number;
    partial: boolean; unmapped_bank_accounts?: number; change: number | null; change_pct: number | null; change_comparable: boolean;
    balance_check_accounts?: number;
    estimated_accounts?: number; unknown_liability_accounts?: number;
  };
  points: WealthPoint[]; warnings: string[]; methodology: string[];
  history?: WealthHistory;
  cache: { saved_at: string | null; stale: boolean; refresh_error: string | null };
};
export type WealthFilters = { currency: string; range: WealthRange; accountIds: string[] | null };

export async function fetchWealth(filters: WealthFilters, signal?: AbortSignal): Promise<WealthData> {
  const params = new URLSearchParams({ currency: filters.currency, range: filters.range });
  const response = filters.accountIds === null ? await authRequest(`/api/wealth?${params}`, { signal })
    : await accountSelectionRequest("/api/wealth", params, filters.accountIds, signal);
  if (!response.ok) throw new Error(`Couldn’t load net worth (${response.status}). Please try again.`);
  const value: WealthData = await response.json();
  if (!value.summary || !Array.isArray(value.accounts) || !Array.isArray(value.points)) throw new Error("The net worth response was incomplete. Please try again.");
  return value;
}

export type WealthRateRefresh = { updated: number; attempted: number; missing: number };
export async function refreshWealthRates(signal?: AbortSignal): Promise<WealthRateRefresh> {
  const response = await authRequest("/api/wealth/refresh", { method: "POST", signal });
  if (!response.ok) throw new Error(`Currency reference rates could not be refreshed (${response.status}). Saved rates are still used where available.`);
  const data = await response.json();
  if (!data || ![data.updated, data.attempted, data.missing].every(value => Number.isInteger(value) && value >= 0)
    || data.updated + data.missing !== data.attempted) throw new Error("The exchange-rate update result was incomplete. Saved rates are still used where available.");
  dataChanged("wealth/refresh");
  return { updated: data.updated, attempted: data.attempted, missing: data.missing };
}

export async function archiveWealthSource(sourceId: string, archived: boolean): Promise<void> {
  const response = await authRequest("/api/wealth/sources/archive", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_id: sourceId, archived }),
  });
  if (!response.ok) throw new Error(`The account could not be ${archived ? "archived" : "reinstated"} (${response.status}). Please try again.`);
  dataChanged("wealth/sources/archive");
}

export type WealthSource = Pick<WealthAccount, "id" | "default_included" | "archived_by_user" | "linked_to">;
export async function fetchWealthSources(signal?: AbortSignal): Promise<WealthSource[]> {
  const response = await authRequest("/api/wealth/sources", { signal });
  if (!response.ok) throw new Error("Account preferences could not be loaded. Please try again.");
  const data = await response.json();
  if (!Array.isArray(data.sources)) throw new Error("Account preferences were incomplete. Please try again.");
  return data.sources;
}
