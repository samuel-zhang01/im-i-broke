import { accountSelectionRequest } from "./accountSelectionRequest";

export type MarketValueRange = "1d" | "1w" | "1m";
export type MarketValueInterval = "1h" | "1d";
export type InvestmentValueView = { mode: "compare" | "history" | "recent"; range: MarketValueRange; interval: MarketValueInterval };
export type MarketValuePoint = {
  timestamp: string; value: number | null; priced_holdings: number; holdings_count: number;
  known_accounts: number; missing_accounts: number; oldest_price_at: string | null; newest_price_at?: string | null;
  sessions?: string[]; estimated: true; coverage_key: string;
};
export type MarketValueData = {
  generated_at: string; currency: string; range: MarketValueRange; interval: MarketValueInterval;
  basis: "current_holdings"; holdings_as_of: string | null; market_as_of: string | null;
  selected_account_ids: string[]; status: "ready" | "partial" | "unavailable";
  summary: { value: number | null; change: number | null; change_pct: number | null;
    selected_accounts: number; known_accounts: number; positions: number; priced_positions: number; partial: boolean };
  points: MarketValuePoint[]; warnings: string[]; methodology: string[];
};

export async function fetchMarketValue(accountIds: string[], currency: string, range: MarketValueRange,
  interval: MarketValueInterval, signal?: AbortSignal): Promise<MarketValueData> {
  const params = new URLSearchParams({ currency, range, interval });
  const response = await accountSelectionRequest("/api/investments/market/value", params, accountIds, signal);
  if (!response.ok) throw new Error(`Recent cached prices could not be loaded (${response.status}).`);
  const data = await response.json() as MarketValueData;
  if (data.basis !== "current_holdings" || !data.summary || !Array.isArray(data.points) || !Array.isArray(data.warnings) || !Array.isArray(data.methodology)) throw new Error("The recent-prices response was incomplete.");
  return data;
}
