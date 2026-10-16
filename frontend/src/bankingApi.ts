import { authRequest } from "./authRequest";
import { readCached } from "./readCache";
import { dataChanged } from "./dataEvents";
import type { ProviderSyncStatus } from "./providerRefresh";
import type { BalanceValuation, WealthBalanceCheck } from "./wealthApi";

export type BankBalance = { amount: number | null; currency: string; fetched_at: string; reported_amount?: number | null; valuation?: BalanceValuation };
export type BankBalanceRule = { balance_type: "available_credit" | "reported"; provider_id: string; account_id: string | null; currency: string | null; credit_limit: number | null; confirmed_at: string; legacy_fractional_limit?: boolean };
export type BankAccount = {
  id: string; account_id: string | null; institution_name: string; name: string; currency: string | null;
  status: string; excluded: boolean; present?: boolean; requires_balance_direction?: boolean; mapping_status: "mapped" | "review" | "new" | "excluded";
  connection_enabled?: boolean;
  mapping_reason?: string | null; mapping_candidates: string[]; balance: BankBalance | null; last_synced_at: string | null;
  amount_multiplier?: 1 | -1; balance_multiplier?: 1 | -1;
  last_transaction_at?: string | null; last_error: string | null;
  counts: { cached: number; imported: number; matched: number; pending: number; review: number };
  balance_check?: WealthBalanceCheck;
  valuation?: BalanceValuation;
  balance_rule?: BankBalanceRule | null;
};
export type LocalBankAccount = {
  id: string; bank: string; account_type: string; display_name: string; connected: boolean;
  currency?: string; balance?: number | null; balance_asof?: string | null;
  n_txns?: number; reconciled?: boolean | null;
  balance_source?: "manual" | "statement" | "lunchflow"; balance_recorded_at?: string | null;
  reported_balance?: number | null; valuation?: BalanceValuation;
};
export type BankingData = {
  connection_statuses?: ProviderSyncStatus[];
  configured: boolean; accounts: BankAccount[]; local_accounts: LocalBankAccount[];
  institutions: { name: string; account_ids: string[]; totals_by_currency: Record<string, number> }[];
  totals_by_currency: Record<string, number>;
  balance_totals_by_currency?: Record<string, { assets: number; liabilities: number; net: number }>;
  statement_liabilities_by_currency?: Record<string, number>;
  statement_reported_liabilities_by_currency?: Record<string, number>;
  estimated_accounts?: number; unknown_balance_accounts?: number;
  balance_checks?: (WealthBalanceCheck & { account_id: string; name: string })[];
  sync: { last_attempt_at: string | null; last_success_at: string | null; error: string | null; running: boolean; interval_seconds: number };
  stats: { accounts: number; mapped: number; pending: number; review: number; cached: number; imported: number };
};
export type BankTransaction = {
  id: number; provider_id: string; date: string | null; amount: number; currency: string;
  merchant: string | null; description: string; is_pending: boolean; status: string;
  transaction_id: number | null; review_reason: string | null;
};
export type BankReview = BankTransaction & {
  candidates: { id: number; txn_date: string | null; description: string; merchant: string | null; amount: number; category: string | null }[];
};
export type BankAccountDetail = {
  account: BankAccount; transactions: BankTransaction[]; total: number;
  balance_history: BankBalance[]; reviews: BankReview[];
  totals_by_currency?: Record<string, { money_in: number; money_out: number }>;
};
export type BankActivityQuery = {
  limit?: number; offset?: number; status?: "all" | "booked" | "pending" | "review";
  q?: string; date_from?: string; date_to?: string;
};

async function uncachedRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await authRequest(`/api/banking${path}`, options);
  // Raw upstream bodies may include personal data. Only show locally defined errors.
  if (!response.ok) {
    if (response.status === 401) throw new Error("Your session has expired. Sign in to continue.");
    if (response.status === 429) throw new Error("A refresh was requested recently. Wait a moment before trying again.");
    if (response.status === 409) throw new Error("This account or transaction changed. Reload the page before trying again.");
    if (response.status === 400 || response.status === 422) throw new Error("That change could not be applied. Check the account or transaction selected and try again.");
    throw new Error(`Bank data could not be updated (${response.status}). Your saved history is still available.`);
  }
  return response.json() as Promise<T>;
}
function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  return !options.method || options.method === "GET"
    ? readCached(`/api/banking${path}`, signal => uncachedRequest<T>(path, { ...options, signal }), options.signal || undefined)
    : uncachedRequest<T>(path, options);
}

function mutation(body?: unknown): RequestInit {
  return { method: "POST", ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) };
}
export async function fetchBanking(signal?: AbortSignal, fresh = false): Promise<BankingData> {
  const data = await (fresh ? uncachedRequest<BankingData>("", { signal }) : request<BankingData>("", { signal }));
  if (!Array.isArray(data.accounts) || !Array.isArray(data.local_accounts) || !data.sync) throw new Error("The saved bank response was incomplete. Please try again.");
  return data;
}
export async function syncBanking(signal?: AbortSignal): Promise<BankingData> {
  const data = await request<BankingData>("/sync", { ...mutation(), signal });
  dataChanged("banking/sync");
  return data;
}
export function fetchBankAccount(id: string, query: BankActivityQuery = {}, signal?: AbortSignal): Promise<BankAccountDetail> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== "") params.set(key, String(value));
  return request(`/accounts/${encodeURIComponent(id)}?${params}`, { signal });
}
export type BankMappingOptions = { amount_multiplier?: 1 | -1; balance_multiplier?: 1 | -1; account_type?: "current" | "savings" | "credit" | "cash"; replace_connection_id?: string; confirm_reconnection?: boolean; confirm_distinct_account?: boolean };
export async function mapBankAccount(id: string, accountId: string | null, createNew = false, settings: BankMappingOptions = {}): Promise<unknown> {
  const data = await request(`/accounts/${encodeURIComponent(id)}/mapping`, mutation({ account_id: accountId, create_new: createNew, ...settings }));
  dataChanged("banking/mapping");
  return data;
}
export async function resolveBankReview(id: number, action: "match" | "import" | "ignore", transactionId?: number): Promise<unknown> {
  const data = await request(`/reviews/${id}`, mutation({ action, ...(transactionId === undefined ? {} : { transaction_id: transactionId }) }));
  dataChanged("banking/review");
  return data;
}
export async function setBankBalanceRule(id: string, rule: { balance_type: "available_credit"; credit_limit: number; currency: string } | { balance_type: "reported" }): Promise<BankingData> {
  const data = await request<BankingData>(`/accounts/${encodeURIComponent(id)}/balance-rule`, mutation(rule));
  dataChanged("banking/balance-rule");
  return data;
}

export type BankingHealth = {
  configured: boolean; schema_ready: boolean; checked_at: string; read_only: true; freshness_basis: string;
  cache?: Record<string, number>;
  connections?: Record<string, number>;
  review_queue?: { awaiting_review: number; awaiting_mapping: number; pending_authorizations: number; native_currency_records: number };
  freshness?: { last_success_at: string | null; last_attempt_at: string | null; latest_balance_fetched_at: string | null; last_attempt_failed: boolean };
  scheduler?: { enabled: boolean; interval_seconds: number; running: boolean; expired_lease: boolean };
  integrity_issues?: Record<string, number>;
  timings: { scope: string; operations: Record<string, { requests: number; client_errors: number; server_errors: number; mean_ms: number; recent_p50_ms: number; recent_p95_ms: number }> };
};
export function fetchBankingHealth(signal?: AbortSignal): Promise<BankingHealth> { return request<BankingHealth>("/health", { signal }); }
