import { authRequest } from "./authRequest";
import { clearReadCache, readCached } from "./readCache";
// Typed client for the FastAPI backend. All paths are relative (/api/...) so the
// Vite dev proxy and the nginx prod config both route to the backend.
import { dataChanged } from "./dataEvents";
import type { MerchantInbox, MerchantQuickInput, MerchantQuickPreview, MerchantQuickResult, SimilarNameCandidate } from "./merchantInbox";

export interface Account {
  id: string;
  bank: string;
  account_type: string;
  display_name: string;
  sort_code: string | null;
  account_number: string | null;
  balance: number | null;
  balance_asof?: string | null;
  currency?: string;
  native_balance?: number | null;
  balance_source?: string;
  balance_fetched_at?: string | null;
  balance_stale?: boolean;
  reconciled?: boolean | null;
  n_txns: number;
}

export interface Summary {
  accounts: Account[];
  net_worth: number;
  income: number;
  spending: number;
  net: number;
  n_transactions: number;
  date_range: [string | null, string | null];
  recurring_count: number;
  anomaly_count: number;
  transfer_count: number;
  cash_total: number;
  holdings_value: number;
  unrealised_pl: number | null;
  realised_pl: number | null;
  estimated_unrealised_pl?: number | null;
  estimated_realised_pl?: number | null;
  foreign_cash_value?: number;
  prices_asof: string | null;
  holdings_unpriced: number;
  holdings_stale?: number;
  wealth_partial?: boolean;
}

export interface ValHolding {
  account_ids?: string[];
  account_names?: string[];
  ticker: string;
  isin: string;
  symbol: string | null;
  shares: number;
  avg_cost: number | null;
  cost_basis: number | null;
  price: number | null;
  currency: string | null;
  market_value: number | null;
  unrealised: number | null;
  unrealised_pct: number | null;
  realised: number | null;
  priced: boolean;
  cost_basis_complete?: boolean;
  position_complete?: boolean;
  position_basis?: string;
  corporate_actions?: {
    id: number;
    ex_date: string;
    ratio_new: number;
    ratio_old: number;
    source_url: string | null;
    source_title: string;
  }[];
  stale?: boolean;
  asof?: string | null;
  first_trade?: string;
  last_trade?: string;
  trades?: number;
  bought_shares?: number;
  sold_shares?: number;
  corporate_action_shares?: number;
  gift_shares?: number;
  history_complete?: boolean;
  snapshot_sources?: { account_id: string; date: string; source_file: string; net_shares: number }[];
  bought?: number | null;
  sold?: number | null;
  bought_known?: number;
  sold_known?: number;
  native_currency?: string | null;
  native_bought?: number | null;
  native_sold?: number | null;
  native_cost_basis?: number | null;
  native_avg_cost?: number | null;
  native_realised?: number | null;
  native_price?: number | null;
  native_market_value?: number | null;
  native_unrealised?: number | null;
  reported_results?: { currency: string; amount: number }[];
  missing_gbp_trade_count?: number;
  warnings?: string[];
  quote_asof?: string | null;
  fetched_at?: string | null;
  fx_asof?: string | null;
  fx_rate_to_gbp?: number | null;
  provider?: string | null;
  source_url?: string | null;
  identity_verified?: boolean;
  estimated_cost_basis?: number | null;
  estimated_avg_cost?: number | null;
  estimated_realised?: number | null;
  estimated_unrealised?: number | null;
  estimated_unrealised_pct?: number | null;
  estimated_trade_count?: number;
  estimated_bought?: number | null;
  estimated_sold?: number | null;
  reference_fx?: { date: string; currency: string; rate_to_gbp: number; observed_date: string; source: string; source_url: string; fetched_at: string }[];
}

export interface PortfolioTrade {
  id: number; account: string; date: string; time: string | null; time_timezone: string | null;
  action: string; ticker: string; shares: number | null;
  price: number | null; price_currency: string | null;
  price_major: number | null; currency_major: string | null;
  cash_amount: number; cash_currency: string; gbp_cash_amount: number | null;
  exchange_rate: number | null; fees: { name: string; amount: number; currency: string }[];
  broker_result: number | null; broker_result_currency: string | null;
  sources: string[];
}
export interface PortfolioTrades { isin: string; total: number; offset: number; limit: number; trades: PortfolioTrade[] }

export interface Valuation {
  selected_account_ids?: string[];
  holdings: ValHolding[];
  closed_positions?: ValHolding[];
  refresh?: {
    requested: boolean;
    status: "not_needed" | "success" | "partial" | "failed";
    attempted: number;
    updated: number;
    failed: number;
    retained: number;
    failed_isins: string[];
    attempted_at: string | null;
    fx?: { historical_attempted: number; historical_updated: number; historical_failed: number;
      cash_attempted: number; cash_updated: number; cash_failed: number };
  };
  totals: {
    market_value: number;
    cost_basis: number | null;
    unrealised: number | null;
    unrealised_pct: number | null;
    realised: number | null;
    total_pl: number | null;
    n_priced: number;
    n_unpriced: number;
    asof: string | null;
    n_stale?: number;
    n_unknown_cost?: number;
    valuation_complete?: boolean;
    valuation_fresh?: boolean;
    cost_basis_complete?: boolean;
    position_complete?: boolean;
    foreign_cash?: { currency: string; amount: number; value_gbp: number | null; asof?: string | null; stale?: boolean }[];
    foreign_cash_unvalued?: number;
    foreign_cash_value?: number;
    known_estimated_cost_basis?: number;
    known_estimated_unrealised?: number;
    known_estimated_realised?: number;
    n_unknown_estimated_cost?: number;
    n_unsupported_unrealised?: number;
    estimated_cost_basis?: number | null;
    estimated_unrealised?: number | null;
    estimated_unrealised_pct?: number | null;
    estimated_realised?: number | null;
    estimated_total_pl?: number | null;
    n_estimated_cost?: number;
    native_totals?: { currency: string; cost_basis: number | null; market_value: number | null;
      unrealised: number | null; realised: number | null; n_holdings: number; n_unknown_cost: number; n_unpriced: number }[];
  };
}

export interface Txn {
  account_type?: string;
  currency?: string | null;
  id: number;
  account_id: string;
  account: string;
  txn_date: string | null;
  description: string;
  merchant: string | null;
  amount: number;
  balance: number | null;
  category: string | null;
  category_source: string | null;
  recurring_id: number | null;
  transfer_group: string | null;
  is_anomaly: number;
  source_merchant?: string | null;
  merchant_kind?: MerchantKind;
  spending_treatment?: SpendingTreatment;
  exclude_from_analysis?: boolean | number;
  classification?: {
    explanation?: string;
    reason?: string;
    source?: string;
    confidence?: number;
    [key: string]: unknown;
  };
}

export type MerchantKind =
  | "unknown"
  | "merchant"
  | "person"
  | "self"
  | "financial";
export type SpendingTreatment = "normal" | "self_transfer" | "reimbursement";
export interface MerchantAlias {
  id?: number;
  pattern: string;
  match_mode: "exact" | "prefix" | "contains";
  context: "any" | "transfer" | "card" | "funding" | "spending" | "spending_card" | "spending_transfer" | "spending_bank";
}
export interface MerchantRule {
  id: number;
  canonical_name: string;
  kind: MerchantKind;
  category: string | null;
  spending_treatment: SpendingTreatment;
  source?: string;
  source_url: string | null;
  notes: string | null;
  enabled: boolean;
  aliases: MerchantAlias[];
  n_transactions: number;
  revision?: string;
}
export type MerchantInput = Omit<
  MerchantRule,
  "id" | "n_transactions" | "source" | "revision"
> & { expected_revision?: string };
export interface SimilarNamesQuery {
  canonical_name: string; kind: MerchantKind; spending_treatment: SpendingTreatment;
  merchant_id?: number | null; example_id?: number; patterns?: string[];
}
export interface MerchantDirectory {
  items: MerchantRule[];
  suggestions: {
    merchant: string;
    suggested_merchant_id: number;
    suggested_name: string;
    category: string | null;
    similarity: number;
    n_transactions: number;
    example_id: number;
    explanation: string;
    match_warning?: string | null;
    examples?: { id: number; description: string; txn_date: string; amount: number; account: string }[];
  }[];
  dismissed_suggestions?: { merchant: string; suggested_merchant_id: number; suggested_name: string; updated_at: string }[];
  privacy_note: string;
}
export interface AliasApprovalResult {
  ok: boolean; merchant_id: number; canonical_name: string; alias_id: number;
  pattern: string; already_exists: boolean; matched_transactions: number;
  updated_transactions: number; preserved_reviews: number; message: string;
}
export interface TransactionReview {
  resolution?: "expected" | "issue" | "pending" | null;
  category?: string | null;
  merchant?: string | null;
  merchant_kind?: MerchantKind | null;
  spending_treatment?: SpendingTreatment | null;
  exclude_from_analysis?: boolean | null;
  note?: string | null;
}
export interface BankEvidence {
  merchant: string | null; description: string; date: string | null; amount: number; currency: string;
  status: string; fetched_at: string | null; bank: string; note: string;
}
export interface MerchantActivity {
  merchant: { id: number; canonical_name: string; kind: MerchantKind; category: string | null; spending_treatment: SpendingTreatment; source: string | null; source_url: string | null };
  currency: string;
  summary: { transactions: number; money_in: number; money_out: number; net: number; first_seen: string | null; last_seen: string | null; active_months: number; accounts: number };
  accounts: { id: string; name: string }[];
  monthly: { month: string; money_in: number; money_out: number; payments: number }[];
  transactions: { id: number; txn_date: string | null; description: string; merchant: string | null; category: string | null; amount: number; account_id: string; account: string }[];
  bank_labels: { label: string; bank: string; transactions: number; fetched_at: string | null }[];
  method: string;
}
export interface ReviewResult {
  bank_evidence?: BankEvidence | null;
  transaction: Txn;
  review: TransactionReview | null;
}
export interface MerchantMatch {
  id: string;
  label: string;
  description: string;
  source_url: string;
  suggested_category?: string | null;
  instance_of?: string[];
}
export interface MerchantLookup {
  query: string;
  provider: string;
  status: string;
  cached: boolean;
  stale: boolean;
  fetched_at: string | null;
  retry_after?: number | null;
  search_url?: string;
  message: string;
  matches: MerchantMatch[];
}
export interface AnalyticsQuery {
  account?: string;
  date_from?: string;
  date_to?: string;
  exclude_outliers?: boolean;
  outlier_z?: number;
}

export interface TxnPage {
  total: number;
  limit: number;
  offset: number;
  sum_in: number;
  sum_out: number;
  items: Txn[];
}

export interface NetPoint {
  period: string;
  income: number;
  spending: number;
  net: number;
  cumulative: number;
}

export interface TimeSeries {
  interval: string;
  net: NetPoint[];
  balances: Record<string, { period: string; balance: number }[]>;
  balances_currency?: string;
  excluded_balance_accounts?: number;
}

export interface Category {
  category: string;
  n: number;
  spent: number;
  received: number;
}

export interface Recurring {
  pattern_type?: "regular_payment" | "variable_cost" | "spending_habit";
  forecast_eligible?: boolean;
  forecast_reason?: string;
  amount_consistency?: number;
  regularity?: number;
  evidence_count?: number;
  observed_interval?: string;
  account_id?: string;
  merchant: string;
  cadence: string;
  cadence_days: number;
  typical_amount: number;
  amount_spread: number;
  n_occurrences: number;
  first_seen: string;
  last_seen: string;
  category: string | null;
  monthly_cost: number;
  historical_monthly_cost?: number;
  status: string;
  data_stale?: boolean;
  observed_through?: string;
  current_amount: number;
  next_expected: string | null;
  next_expected_earliest?: string | null;
  next_expected_latest?: string | null;
  amount_min?: number;
  amount_max?: number;
  amount_basis?: string;
  price_change_confirmed?: boolean;
  price_change_percent?: number | null;
  price_change_evidence?: number;
  merchant_labels?: string[];
  missed_cycles?: number;
  schedule_basis?: string;
  schedule_evidence_count?: number;
  schedule_start?: string | null;
  anchor_kind?: string;
  anchor_day?: number | null;
  txn_ids: number[];
}

export interface RecurringPayment {
  id: number;
  account_id: string;
  account: string;
  txn_date: string;
  description: string;
  source_merchant: string | null;
  merchant: string;
  amount: number;
  category: string | null;
  analysis_amount?: number;
  analysis_category?: string;
  analysis_component?: string;
}

export interface RecurringSeries extends Recurring {
  key: string;
  account: string;
  payments: RecurringPayment[];
}

export interface SubscriptionCandidate {
  key: string;
  account_id: string;
  account: string;
  merchant: string;
  category: string;
  first_seen: string;
  last_seen: string;
  n_occurrences: number;
  txn_ids: number[];
  payments: RecurringPayment[];
  reason: string;
}

export interface RecurringOverview {
  patterns: RecurringSeries[];
  candidates: SubscriptionCandidate[];
}

export interface RecurringConfig {
  min_occurrences: number;
  amount_tol: number;
  regularity: number;
  tol_weekly: number;
  tol_fortnightly: number;
  tol_monthly: number;
  tol_bimonthly: number;
  tol_quarterly: number;
  tol_annual: number;
}

export interface Transfer {
  matched?: boolean;
  txn_id?: number;
  evidence?: string;
  counterparty_imported?: boolean;
  group: string;
  amount: number;
  date: string;
  from: string;
  to: string;
  from_desc: string;
  to_desc: string;
}

export interface Anomaly {
  kind?: "large_amount" | "possible_duplicate";
  priority?: "high" | "medium" | "low";
  priority_reason?: string;
  evidence_strength?: "moderate" | "limited";
  baseline_amount?: number;
  threshold_amount?: number;
  history_count?: number;
  comparison_basis?: string;
  baseline_from?: string;
  baseline_to?: string;
  comparison_transaction_ids?: number[];
  duplicate_transaction_ids?: number[];
  related_kinds?: ("large_amount" | "possible_duplicate")[];
  method?: string;
  evidence_note?: string;
  explanation?: string;
  outcome?: "expected" | "issue" | "learned";
  learning_reason?: string;
  learning_source_id?: number;
  reviewed_at?: string;
  id: number;
  account: string;
  txn_date: string;
  description: string;
  merchant: string | null;
  amount: number;
  category: string | null;
  reason: string;
  z: number | null;
}

export interface Advice {
  type: string;
  title: string;
  detail: string;
  items?: { merchant: string; monthly: number; account_id?: string; txn_ids?: number[] }[];
  potential_monthly_saving: number;
}

export interface Analytics {
  stats: {
    months: number;
    avg_monthly_income: number;
    avg_monthly_spend: number;
    avg_monthly_net: number;
    savings_rate: number | null;
    busiest_month: string | null;
    busiest_month_spend: number;
    biggest_expense: {
      txn_date: string;
      description: string;
      merchant: string | null;
      amount: number;
    } | null;
    total_income: number;
    total_spend: number;
    gross_spend: number;
    total_net: number;
    recorded_spend: number;
    refunds: number;
    outlier_candidates: number;
    median_monthly_spend: number | null;
    complete_months: number;
    typical_monthly_spend: number | null;
    excluded_outlier_count: number;
    excluded_outlier_spend: number;
  };
  top_merchants: { merchant: string; n: number; spend: number }[];
  by_account: { account: string; spend: number; income: number; n: number }[];
  category_months: {
    months: string[];
    series: { name: string; data: number[] }[];
  };
  monthly: {
    m: string;
    income: number;
    gross_spend: number;
    refunds: number;
    spend: number;
    net: number;
    recorded_spend: number;
    excluded_outlier_spend: number;
    coverage_complete: boolean;
    coverage_label: string;
    missing_accounts: string[];
  }[];
  exclusions: {
    reason: string;
    count: number;
    outflow: number;
    inflow: number;
    net_spending_impact: number;
  }[];
  comparison: {
    current_month: string | null;
    previous_month: string | null;
    current_spend: number | null;
    previous_spend: number | null;
    change_amount: number | null;
    change_pct: number | null;
    comparable: boolean;
    explanation: string;
  };
  drivers: {
    category: string;
    spend: number;
    previous_spend: number;
    change_amount: number;
    change_pct: number | null;
  }[];
  definitions: Record<string, string>;
  insights: { title: string; detail: string; severity?: string }[];
}

export interface Investments {
  accounts: { id: string; display_name: string; bank: string; history_start?: string | null;
    history_end?: string | null; statement_asof?: string | null; history_complete?: boolean | null;
    history_note?: string | null; statement_snapshot?: {date: string; base_currency: string;
      net_asset_value: number | null; securities_value: number | null; cash_balances: Record<string, number>;
      n_positions: number; reconciled: boolean} }[];
  selected_account_ids: string[];
  account: string;
  stats: {
    cash_balance: number;
    deposited: number | null;
    withdrawn: number | null;
    net_contributed: number | null;
    invested: number | null;
    divested: number | null;
    net_invested: number | null;
    dividends: number | null;
    interest: number | null;
    cashback: number | null;
    income: number | null;
    estimated_invested?: number | null;
    estimated_divested?: number | null;
    estimated_net_invested?: number | null;
    estimated_dividends?: number | null;
    estimated_interest?: number | null;
    estimated_cashback?: number | null;
    estimated_income?: number | null;
    estimated_deposited?: number | null;
    estimated_withdrawn?: number | null;
    estimated_net_contributed?: number | null;
    cash_history_complete?: boolean;
    history_note?: string | null;
    n_trades: number;
    n_holdings: number;
  };
  holdings: {
    ticker: string;
    bought: number | null;
    sold: number | null;
    net: number | null;
    estimated_bought?: number | null;
    estimated_sold?: number | null;
    estimated_net?: number | null;
    trades: number;
    last: string;
  }[];
  timeline_currency?: string;
  timeline: { month: string; bought: number; sold: number; income: number;
    estimated_bought?: number | null; estimated_sold?: number | null; estimated_income?: number | null }[];
  native_currency_totals?: { currency: string; invested: number; divested: number; dividends: number; interest: number; income: number; cash_balance: number; deposited: number; withdrawn: number }[];
  actions: { action: string; n: number; total: number | null; known_total?: number; native_totals?: {currency: string; amount: number}[] }[];
}

async function response<T>(res: Response, changedPath?: string): Promise<T> {
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail))
        detail = body.detail
          .map((e: { msg?: string }) => e.msg)
          .filter(Boolean)
          .join("; ");
    } catch {
      /* A proxy may return a non-JSON error page. */
    }
    throw Object.assign(
      new Error(detail || (res.status === 413 ? "Request is too large. Reduce its size and try again." : `Request failed (${res.status}). Please try again.`)),
      { status: res.status },
    );
  }
  const result = await res.json() as T;
  if (changedPath) { clearReadCache(); dataChanged(changedPath); }
  return result;
}

async function localRequest(path: string, options: RequestInit = {}, readOnly = false): Promise<Response> {
  const controller = new AbortController();
  const writing = !readOnly && options.method && options.method !== "GET";
  const cancel = () => controller.abort();
  options.signal?.addEventListener("abort", cancel, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(() => controller.abort(), writing ? 120000 : 30000);
  try {
    return await authRequest(path, { ...options, signal: controller.signal });
  } catch (error) {
    if (options.signal?.aborted) throw new DOMException("The request was cancelled.", "AbortError");
    if (error instanceof Error && error.name === "AbortError")
      throw new Error(writing
        ? "The save is taking longer than expected. It may have completed; check the updated results before trying again."
        : "The local server took too long to respond. Try again.");
    throw new Error("Could not reach your local Am I Broke? server. Check that it is running and try again.");
  } finally { clearTimeout(timer); options.signal?.removeEventListener("abort", cancel); }
}

async function get<T>(
  path: string,
  params?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const qs = params
    ? "?" +
      Object.entries(params)
        .filter(([, v]) => v !== undefined && v !== null && v !== "")
        .flatMap(([k, v]) => (Array.isArray(v) ? (v.length ? v : ["__none__"]) : [v])
          .map(item => `${encodeURIComponent(k)}=${encodeURIComponent(String(item))}`))
        .join("&")
    : "";
  const url = `/api/${path}${qs}`;
  const load = async (readSignal?: AbortSignal) => response<T>(await localRequest(url, { cache: "no-store", signal: readSignal }));
  // Investment clients retain their existing fetch behavior.
  return /^(summary|transactions|analytics|timeseries|category-options|workspace\/category-options|categories|recurring|transfers|anomalies|planning|scenarios|merchants|preferences)([/?]|$)/.test(path)
    ? readCached(url, load, signal) : load(signal);
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await localRequest(`/api/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response<T>(res, path);
}

async function investmentData<T>(
  path: string,
  params?: Record<string, unknown>,
): Promise<T | null> {
  try {
    return await get<T>(path, params);
  } catch (error) {
    if (error instanceof Error && "status" in error && error.status === 404)
      return null;
    throw error;
  }
}

export interface TxnQuery {
  q?: string;
  account?: string;
  category?: string;
  date_from?: string;
  date_to?: string;
  min_amount?: number;
  max_amount?: number;
  direction?: "in" | "out";
  anomaly?: boolean;
  recurring?: boolean;
  transfer?: boolean;
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export interface PlanningQuery {
  years: number;
  annual_return: number;
  inflation: number;
  target: number;
  monthly_contribution?: number | null;
  initial_wealth?: number | null;
  baseline_method?: "mean" | "median";
  exclude_outliers?: boolean;
  one_off_amount?: number;
  one_off_month?: number;
}
export interface SavedScenario {
  id: number;
  name: string;
  parameters: PlanningQuery;
  created_at?: string;
  updated_at?: string;
}

export interface Planning {
  as_of: string;
  wealth: {
    known_net_worth: number;
    cash_assets: number;
    investment_cash: number;
    holdings_value: number;
    liabilities: number;
    liquid_after_debt: number;
    unknown_balance_accounts: string[];
    unpriced_holdings: number;
    stale_accounts: number;
    is_partial: boolean;
    accounts: {
      id: string;
      name: string;
      type: string;
      balance: number | null;
      balance_asof: string | null;
      age_days: number | null;
      reconciled: boolean | null;
    }[];
  };
  baseline: {
    months: number;
    period_start: string | null;
    period_end: string | null;
    avg_income: number | null;
    avg_spending: number | null;
    avg_surplus: number | null;
    median_surplus: number | null;
    savings_rate: number | null;
    median_income: number | null;
    median_spending: number | null;
    recorded_avg_surplus: number | null;
    recorded_avg_spending: number | null;
    excluded_outlier_spend: number;
    excluded_outlier_count: number;
    selected_contribution: number | null;
    baseline_method: "mean" | "median";
    exclude_outliers: boolean;
    monthly: {
      month: string;
      income: number;
      spending: number;
      surplus: number;
      recorded_spending: number;
      recorded_surplus: number;
      excluded_outlier_spend: number;
      excluded_outlier_count: number;
    }[];
    excluded_months: { month: string; reason: string }[];
  };
  resilience: {
    runway_months: number | null;
    three_month_buffer: number | null;
    six_month_buffer: number | null;
    recurring_monthly: number;
  };
  quality: {
    verified_statements: number;
    failed_statements: number;
    unverified_statements: number;
    uncategorized_count: number;
    uncategorized_spend: number;
    undated_transactions: number;
  };
  assumptions: Omit<
    PlanningQuery,
    "monthly_contribution" | "initial_wealth"
  > & {
    monthly_contribution: number | null;
    initial_wealth: number;
    contribution_source: string;
  };
  scenarios: {
    name: string;
    annual_return: number;
    points: {
      year: number;
      nominal: number;
      real: number;
      contributed: number;
      planned_expenses: number;
    }[];
    target_month: number | null;
    ending_above_target: boolean;
  }[];
  insights: { severity: "info" | "warning"; title: string; detail: string }[];
  methodology: string[];
}

export const api = {
  similarMerchantNames: async (data: SimilarNamesQuery) => response<{items: SimilarNameCandidate[]; method: string}>(await localRequest('/api/merchants/similar', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }, true)),
  merchantInbox: (signal?: AbortSignal) => get<MerchantInbox>('merchants/inbox', undefined, signal),
  previewMerchantClassification: async (data: MerchantQuickInput) => response<MerchantQuickPreview>(await localRequest('/api/merchants/classify/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }, true)),
  classifyMerchant: (data: MerchantQuickInput) => post<MerchantQuickResult>('merchants/classify', data),
  preferences: <T>(key: string) => get<{ value: T }>(`preferences/${key}`),
  savePreferences: async (key: string, value: unknown) =>
    response(
      await localRequest(`/api/preferences/${key}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value),
      }),
      `preferences/${key}`,
    ),
  lookupMerchant: async (query: string): Promise<MerchantLookup> => {
    const result = await get<MerchantLookup>("merchant-lookup", { query });
    if (result.status !== "browser_lookup_required") return result;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    let retryAfter: number | null = null;
    const cooldownKey = "finance.wikidata.retryUntil";
    const setCooldown = (seconds: number) => {
      retryAfter = Math.max(5, Math.min(86400, seconds));
      try {
        sessionStorage.setItem(
          cooldownKey,
          String(Date.now() + retryAfter * 1000),
        );
      } catch {
        /* Storage may be disabled. */
      }
    };
    const wikidata = async <T>(params: URLSearchParams): Promise<T> => {
      // This is an explicit user interaction. MediaWiki reserves maxlag for
      // background work; interactive reads still honor rate limits below.
      const res = await fetch(`https://www.wikidata.org/w/api.php?${params}`, {
        signal: controller.signal,
        credentials: "omit",
      });
      if (res.status === 429 || res.status === 503) {
        const retry = res.headers.get("Retry-After");
        const seconds =
          retry && /^\d+$/.test(retry)
            ? Number(retry)
            : retry
              ? Math.ceil((Date.parse(retry) - Date.now()) / 1000)
              : 60;
        setCooldown(Number.isFinite(seconds) ? seconds : 60);
        throw new Error(
          `Wikidata is busy. Try again in ${retryAfter} seconds.`,
        );
      }
      const payload = await response<T & { error?: { code?: string } }>(res);
      if (payload.error) {
        if (
          payload.error.code === "maxlag" ||
          payload.error.code === "ratelimited"
        )
          setCooldown(60);
        const code =
          payload.error.code && /^[a-zA-Z0-9_-]{1,40}$/.test(payload.error.code)
            ? ` (${payload.error.code})`
            : "";
        throw new Error(
          `Wikidata could not complete this lookup${code}. Try again later or open the public search link.`,
        );
      }
      return payload;
    };
    try {
      try {
        retryAfter = Math.ceil(
          (Number(sessionStorage.getItem(cooldownKey)) - Date.now()) / 1000,
        );
      } catch {
        /* Storage may be disabled. */
      }
      if (retryAfter !== null && retryAfter > 0)
        throw new Error(
          `Wikidata asked this browser to wait ${retryAfter} seconds before retrying.`,
        );
      retryAfter = null;
      const params = new URLSearchParams({
        action: "wbsearchentities",
        search: result.query,
        language: "en",
        type: "item",
        limit: "5",
        format: "json",
        origin: "*",
      });
      const search = await wikidata<{
        search?: { id: string; label?: string; description?: string }[];
      }>(params);
      if (!Array.isArray(search.search))
        throw new Error("Wikidata returned an unexpected search response.");
      const candidates = search.search
        .slice(0, 5)
        .filter(
          (c) => c && typeof c.id === "string" && /^Q[1-9]\d{0,14}$/.test(c.id),
        );
      let claims: Record<
        string,
        {
          claims?: {
            P31?: { mainsnak?: { datavalue?: { value?: { id?: string } } } }[];
          };
        }
      > = {};
      if (candidates.length) {
        const details = new URLSearchParams({
          action: "wbgetentities",
          ids: candidates.map((c) => c.id).join("|"),
          props: "claims",
          format: "json",
          origin: "*",
        });
        const fetched = await wikidata<{ entities?: typeof claims }>(details);
        if (
          !fetched.entities ||
          typeof fetched.entities !== "object" ||
          Array.isArray(fetched.entities)
        )
          throw new Error("Wikidata returned an unexpected entity response.");
        claims = fetched.entities;
        if (candidates.some((c) => !claims[c.id]))
          throw new Error(
            "Wikidata could not verify every candidate. Try again later.",
          );
      }
      return await post<MerchantLookup>("merchant-lookup/cache", {
        query: result.query,
        matches: candidates.map((c) => ({
          id: c.id,
          label: c.label ?? "",
          description: c.description ?? "",
          instance_of: (claims[c.id]?.claims?.P31 ?? [])
            .map((p) => p.mainsnak?.datavalue?.value?.id)
            .filter(Boolean),
        })),
      });
    } catch (error) {
      return {
        ...result,
        status: retryAfter ? "rate_limited" : "unavailable",
        retry_after: retryAfter,
        stale: result.cached || result.stale,
        message:
          error instanceof Error && error.name === "AbortError"
            ? "Wikidata lookup timed out. Try again later."
            : error instanceof Error
              ? error.message
              : "Wikidata lookup failed. Try again later.",
      };
    } finally {
      clearTimeout(timeout);
    }
  },
  merchantActivity: (id: number, signal?: AbortSignal) => get<MerchantActivity>(`merchants/${id}/activity`, undefined, signal),
  merchants: (signal?: AbortSignal) => get<MerchantDirectory>("merchants", undefined, signal),
  approveMerchantAlias: (id: number, pattern: string, example_id?: number) =>
    post<AliasApprovalResult>(`merchants/${id}/aliases`, {pattern, example_id}),
  dismissMerchantSuggestion: (id: number, pattern: string) =>
    post<{ok:boolean}>(`merchants/${id}/suggestions/dismiss`, {pattern}),
  restoreMerchantSuggestion: (id: number, pattern: string) =>
    post<{ok:boolean}>(`merchants/${id}/suggestions/restore`, {pattern}),
  createMerchant: async (data: MerchantInput) =>
    (await post<{ merchant: MerchantRule }>("merchants", data)).merchant,
  updateMerchant: async (id: number, data: MerchantInput) =>
    (
      await response<{ merchant: MerchantRule }>(
        await localRequest(`/api/merchants/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }),
        `merchants/${id}`,
      )
    ).merchant,
  disableMerchant: async (id: number) =>
    response<{ ok: boolean }>(
      await localRequest(`/api/merchants/${id}`, { method: "DELETE" }),
      `merchants/${id}`,
    ),
  review: (id: number, signal?: AbortSignal) => get<ReviewResult>(`transactions/${id}/review`, undefined, signal),
  saveReview: (id: number, review: TransactionReview) =>
    post<ReviewResult>(`transactions/${id}/review`, review),
  resetReview: async (id: number) =>
    response<ReviewResult>(
      await localRequest(`/api/transactions/${id}/review`, { method: "DELETE" }),
      `transactions/${id}/review`,
    ),
  scenarios: (signal?: AbortSignal) => get<SavedScenario[]>("scenarios", undefined, signal),
  saveScenario: (name: string, parameters: PlanningQuery, id?: number) =>
    post<SavedScenario>("scenarios", { name, parameters, id }),
  deleteScenario: async (id: number) =>
    response<{ ok: boolean }>(
      await localRequest(`/api/scenarios/${id}`, { method: "DELETE" }),
      `scenarios/${id}`,
    ),
  planning: (q: PlanningQuery, signal?: AbortSignal) =>
    get<Planning>("planning", q as unknown as Record<string, unknown>, signal),
  summary: (signal?: AbortSignal) => get<Summary>("summary", undefined, signal),
  transactions: (q: TxnQuery, signal?: AbortSignal) =>
    get<TxnPage>("transactions", q as Record<string, unknown>, signal),
  timeseries: (interval: "day" | "month", account?: string, signal?: AbortSignal) =>
    get<TimeSeries>("timeseries", { interval, account }, signal),
  categories: () => get<Category[]>("categories"),
  categoryOptions: (signal?: AbortSignal) => get<string[]>("workspace/category-options", undefined, signal),
  recurring: (cfg?: RecurringConfig) =>
    get<Recurring[]>(
      "recurring",
      cfg as unknown as Record<string, unknown> | undefined,
    ),
  recurringOverview: (cfg?: RecurringConfig, signal?: AbortSignal) =>
    get<RecurringOverview>("recurring", { ...cfg, details: true }, signal),
  transfers: (signal?: AbortSignal) => get<Transfer[]>("transfers", undefined, signal),
  anomalies: (status: "open" | "resolved" | "learned" = "open", signal?: AbortSignal) => get<Anomaly[]>(`anomalies?status=${status}`, undefined, signal),
  advice: () => get<Advice[]>("advice"),
  analytics: (q: AnalyticsQuery = {}, signal?: AbortSignal) =>
    get<Analytics>("analytics", q as Record<string, unknown>, signal),
  investments: (account?: string[]) => investmentData<Investments>("investments", { account }),
  investmentTrades: (isin: string, offset = 0, account?: string[]) => get<PortfolioTrades>("investments/trades", { isin, offset, limit: 100, account }),
  valuation: (refresh = false, account?: string[]) =>
    investmentData<Valuation>(
      "valuation",
      { refresh: refresh || undefined, account },
    ),
  setCategory: (id: number, category: string) =>
    post<{ ok: boolean }>(`transactions/${id}/category`, { category }),

};
