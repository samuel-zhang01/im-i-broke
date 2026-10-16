import { accountSelectionRequest } from "./accountSelectionRequest";
import { authRequest } from "./authRequest";
import type { ConnectedActivity } from "./snaptradeApi";

export type ActivityCoverage = {
  account_id: string; is_paper: boolean | null; stored_activities: number;
  first_activity_at: string | null; last_activity_at: string | null; last_attempt_at: string | null;
  last_fully_fetched_window: { start: string; end: string; retrieved_at: string } | null;
  fully_fetched_windows: { start: string; end: string; retrieved_at: string }[];
  partial_or_failed_requests: number; history_complete: false; limitations: string[];
};
export type InvestmentCoverage = {
  generated_at: string; retrieved_at: string | null; selected_account_ids: string[]; paper: boolean;
  accounts: {
    id: string; name: string; institution: string; is_paper: boolean; connection_disabled: boolean; account_value_known: boolean;
    initial_sync_complete: boolean | null;
    sections: { key: string; status: string; rows: number; retained: boolean; truncated: boolean;
      source_at: string | null; source_age_days: number | null; source_outdated: boolean;
      retrieved_at: string | null; first_date: string | null; last_date: string | null; error: string | null }[];
    activity_archive: ActivityCoverage | null;
    observations: { retrieved_at: string; observed_at: string; source_time_known: number; holdings_status: string; holdings_truncated: number }[];
  }[];
  performance: { personal_return_available: false; reason: string }; methodology: string[];
};
export type SavedActivityAccount = { id: string; name: string; institution: string; is_paper: boolean; stored_activities: number };
export type SavedActivities = { activities: ConnectedActivity[]; pagination: { limit: number; offset: number; total: number; has_more: boolean }; coverage: ActivityCoverage };

async function read<T>(url: string, signal?: AbortSignal, accountIds?: string[]): Promise<T> {
  const [endpoint, query] = url.split("?");
  const response = accountIds === undefined ? await authRequest(url, { signal })
    : await accountSelectionRequest(endpoint, new URLSearchParams(query), accountIds, signal);
  if (!response.ok) throw new Error(`Saved investment data could not be loaded (${response.status}). Please retry.`);
  return response.json();
}
export async function fetchInvestmentCoverage(accountIds: string[], currency: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ currency });
  const data = await read<InvestmentCoverage>(`/api/investments/analysis/coverage?${params}`, signal, accountIds);
  if (!Array.isArray(data.accounts) || !data.performance || !Array.isArray(data.methodology)) throw new Error("Investment coverage was incomplete.");
  return data;
}
export async function fetchSavedActivityAccounts(signal?: AbortSignal) {
  const data = await read<{ accounts: SavedActivityAccount[] }>("/api/investments/connected/activities/accounts", signal);
  if (!Array.isArray(data.accounts)) throw new Error("Saved activity accounts were incomplete.");
  return data.accounts;
}
export async function fetchSavedActivities(accountId: string, offset: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ account_id: accountId, limit: "25", offset: String(offset) });
  const data = await read<SavedActivities>(`/api/investments/connected/activities?${params}`, signal);
  if (!Array.isArray(data.activities) || !data.pagination || !data.coverage) throw new Error("Saved activity history was incomplete.");
  return data;
}
