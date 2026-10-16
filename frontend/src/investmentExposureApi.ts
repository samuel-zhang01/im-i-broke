import { accountSelectionRequest } from "./accountSelectionRequest";

export type InvestmentExposureData = {
  currency: string; selected_account_ids: string[]; paper: boolean; retrieved_at: string | null;
  status: "ready" | "partial" | "unavailable";
  summary: {
    gross_security_value: number | null; holdings: number; known_holdings: number;
    instruments: number; selected_accounts: number; accounts_with_securities: number;
    largest_weight_pct: number | null; top5_weight_pct: number | null;
    hhi_fraction: number | null; effective_instruments: number | null;
  };
  instruments: {
    instrument_id: string; symbol: string; name: string; listing_currency: string; exchange: string;
    asset_type: string; identity_verified: boolean; gross_value: number | null; net_value: number | null;
    weight_pct: number | null; missing_values: number;
    accounts: { id: string; name: string; institution: string; gross_value: number | null; positions: number }[];
  }[];
  account_overlap: {
    first_account_id: string; first_account_name: string; second_account_id: string; second_account_name: string;
    overlap_pct: number | null; shared_instruments: number | null; reason: string | null;
  }[];
  scenarios: { shock_pct: number; largest_gross_impact_pct: number | null; top5_gross_impact_pct: number | null;
    largest_value_change: number | null; top5_value_change: number | null };
  warnings: string[]; methodology: string[];
};

export async function fetchInvestmentExposure(accountIds: string[], currency: string, signal?: AbortSignal): Promise<InvestmentExposureData> {
  const query = new URLSearchParams({ currency });
  const response = await accountSelectionRequest("/api/investments/analysis/exposure", query, accountIds, signal);
  if (!response.ok) throw new Error(`Security exposure could not be loaded (${response.status}).`);
  const result: InvestmentExposureData = await response.json();
  if (!result.summary || !result.scenarios || !Array.isArray(result.instruments) || !Array.isArray(result.account_overlap)) {
    throw new Error("The security exposure response was incomplete.");
  }
  return result;
}
