import { accountSelectionRequest } from "./accountSelectionRequest";
import { authRequest } from "./authRequest";
import { dataChanged } from "./dataEvents";

export type InvestmentRange = "1w" | "1m" | "3m" | "6m" | "ytd" | "1y" | "all";
export type PriceReturnPeriod = "1d" | "1w" | "1m" | "3m" | "ytd" | "1y";
export type AnalysedHolding = {
  id: string; account_id: string; account_name: string; institution: string;
  position_id: string; instrument_id: string | null; symbol: string; name: string;
  asset_type: string | null; currency: string | null; quantity: number | null;
  native_price: number | null; native_value: number | null; native_average_cost: number | null;
  native_unrealized_pnl: number | null; value: number | null; unrealized_pnl: number | null;
  unrealized_pct: number | null; weight_pct: number | null; sector: string | null;
  market_symbol: string | null; market_currency: string | null; market_as_of: string | null;
  market_status: string; returns: Record<PriceReturnPeriod, number | null>;
  period_return_pct: number | null; day_move_estimate: number | null;
  day_move_unavailable_reason?: "unsupported_security" | "missing_price_history" | "stale_prices" | "stale_holdings" | "missing_quantity" | "share_units_changed" | "missing_previous_session_close" | "missing_exchange_rate" | null; day_move_missing_date?: string | null; is_cash: boolean; stale: boolean;
};
export type AnalysedAccount = {
  id: string; name: string; institution: string; number_suffix: string | null; type: string | null;
  connection_id: string | null; is_paper: boolean; currency: string | null;
  native_value: number | null; value: number | null; cash: number | null;
  holdings_count: number; unrealized_pnl: number | null; day_move_estimate: number | null;
  holdings_as_of: string | null; stale: boolean; positions_status: string; selected: boolean;
};
export type AnalysedSector = {
  name: string; value: number | null; weight_pct: number | null; holdings_count: number;
  priced_holdings: number; day_move_estimate: number | null;
};
export type InvestmentAnalysis = {
  retrieved_at: string | null; market_as_of: string | null; currency: string; range: InvestmentRange;
  balance_as_of?: string | null; holdings_as_of?: string | null;
  selected_account_ids: string[]; paper: boolean; accounts: AnalysedAccount[];
  summary: {
    value: number | null; cash: number | null; unrealized_pnl: number | null;
    day_move_estimate: number | null; holdings_count: number; selected_accounts: number;
    known_accounts: number; missing_accounts: number; valued_holdings: number;
    priced_holdings: number; classified_holdings: number; partial: boolean;
    pnl_holdings?: number; direct_holdings?: number; fund_holdings?: number; unclassified_holdings?: number;
  };
  holdings: AnalysedHolding[]; sectors: AnalysedSector[]; warnings: string[]; methodology: string[];
};
export type SecurityAnalysis = {
  holding: AnalysedHolding; currency: string; range: InvestmentRange;
  points: { date: string; close: number | null; return_pct: number | null }[];
  market_currency: string | null; price_basis: "price_return";
  source_url: string | null; warnings: string[];
};
export type BenchmarkSeries = { id: string; name: string; currency: string | null; as_of: string | null; status: string; points: { date: string; close: number | null; return_pct: number | null }[]; source_url: string | null };

export type InvestmentInsightAction = { label: string; target: "account" | "holding" | "calendar" | "allocation"; account_id: string | null; position_id: string | null };
export type InvestmentInsightsData = {
  as_of: string | null; retrieved_at: string | null; currency: string; selected_account_ids: string[]; paper: boolean;
  summary: { review_count: number; quality_count: number; movement_count: number; context_count: number; accounts: number; holdings: number };
  items: { id: string; category: "quality" | "movement" | "context"; severity: "warning" | "info"; code: string; title: string; detail: string; action: InvestmentInsightAction; account_ids: string[]; holding_ids: string[]; evidence: { label: string; value: number | null; unit: "count" | "percent" | "days"; as_of: string | null }[] }[];
  coverage: { accounts: number; known_accounts: number; holdings: number; usable_cost_holdings: number; verified_market_holdings: number; unique_instruments: number; movement_eligible_instruments: number; history_accounts: number; history_interval_days: number | null; activity_rows: number; personal_profit_available: false };
  methodology: string[];
};

export async function fetchInvestmentInsights(accountIds: string[], currency: string, signal?: AbortSignal): Promise<InvestmentInsightsData> {
  const params = new URLSearchParams({ currency });
  const result = await readAnalysis<InvestmentInsightsData>(`/api/investments/analysis/insights?${params}`, signal, accountIds);
  if (!result.summary || !result.coverage || !Array.isArray(result.items)) throw new Error("The investment review response was incomplete.");
  return result;
}

export async function fetchInvestmentBenchmarks(range: InvestmentRange, signal?: AbortSignal): Promise<BenchmarkSeries[]> {
  const result = await readAnalysis<{ series: BenchmarkSeries[] }>(`/api/investments/market/benchmarks?range=${encodeURIComponent(range)}`, signal);
  if (!Array.isArray(result.series)) throw new Error("Benchmark history was incomplete.");
  return result.series;
}

async function readAnalysis<T>(path: string, signal?: AbortSignal, accountIds?: string[]): Promise<T> {
  const [endpoint, query] = path.split("?");
  const response = accountIds === undefined ? await authRequest(path, { signal })
    : await accountSelectionRequest(endpoint, new URLSearchParams(query), accountIds, signal);
  if (!response.ok) throw new Error(`Investment analysis could not be loaded (${response.status}). Saved portfolio data remains available in Portfolio.`);
  return response.json() as Promise<T>;
}

export async function fetchInvestmentAnalysis(accountIds: string[], currency: string, range: InvestmentRange, signal?: AbortSignal): Promise<InvestmentAnalysis> {
  const params = new URLSearchParams({ currency, range });
  const result = await readAnalysis<InvestmentAnalysis>(`/api/investments/analysis?${params}`, signal, accountIds);
  if (!result.summary || !Array.isArray(result.holdings) || !Array.isArray(result.accounts)) throw new Error("The investment analysis response was incomplete.");
  return result;
}

export async function fetchSecurityAnalysis(accountId: string, positionId: string, currency: string, range: InvestmentRange, signal?: AbortSignal): Promise<SecurityAnalysis> {
  const params = new URLSearchParams({ account: accountId, position: positionId, currency, range });
  const result = await readAnalysis<SecurityAnalysis>(`/api/investments/analysis/security?${params}`, signal);
  if (!result.holding || !Array.isArray(result.points)) throw new Error("The security history response was incomplete.");
  return result;
}

export async function refreshInvestmentMarket(signal?: AbortSignal): Promise<{ attempted: number; updated: number; failed: number; retained: number; unsupported: number; incomplete?: number; fetched_at: string | null; message: string }> {
  const response = await authRequest("/api/investments/market/refresh", { method: "POST", signal });
  if (!response.ok) throw new Error(response.status === 429 ? "Market refresh is cooling down. Saved prices remain available." : `Market data could not be refreshed (${response.status}). Saved prices remain available.`);
  const result = await response.json();
  dataChanged("investments/market");
  return result;
}
