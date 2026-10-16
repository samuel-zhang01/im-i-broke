import { accountSelectionRequest } from "./accountSelectionRequest";

export type CalendarSource = "estimate" | "recorded";
export type CalendarDay = {
  date: string; start_value: number | null; end_value: number | null; change: number | null;
  change_pct: number | null; return_pct: null; profit: null; external_flows: null;
  observation_change: number | null; observation_start_date: string | null;
  observation_start_value: number | null; observation_change_pct: number | null; observation_days: number | null;
  known_accounts: number; selected_accounts: number; complete: boolean;
  source: CalendarSource; estimated: boolean; reason: string | null;
};
export type CalendarPeriod = {
  month?: number; year?: number; start_date: string; end_date: string | null;
  start_value: number | null; end_value: number | null; change: number | null;
  change_pct: number | null; return_pct: null; profit: null; external_flows: null;
  complete: boolean; daily_coverage_complete: boolean; observed_days: number; expected_days: number;
  positive_days: number; negative_days: number; flat_days: number;
  source: CalendarSource; estimated: boolean;
};
export type InvestmentCalendarData = {
  currency: string; year: number; month: number; source: CalendarSource; metric: "account_value_change";
  selected_account_ids: string[]; paper: boolean; available_years: number[];
  available_sources: { id: CalendarSource; label: string; points: number }[];
  days: CalendarDay[]; months: CalendarPeriod[]; summary: CalendarPeriod; year_summary: CalendarPeriod;
  points: { date: string; value: number | null; known_accounts: number; complete: boolean; estimated: boolean }[];
  retrieved_at: string | null; warnings: string[]; methodology: string[];
};

export function calendarMonthLabel(year: number, month: number): string {
  return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

export function calendarOffset(year: number, month: number): number {
  return (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7;
}

export function compactCalendarChange(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const amount = new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: Math.abs(value) < 10 ? 2 : Math.abs(value) < 1000 ? 0 : 1 }).format(value);
  return value > 0 ? `+${amount}` : amount;
}

export async function fetchInvestmentCalendar(accountIds: string[], currency: string, year: number, month: number, source: CalendarSource, signal?: AbortSignal): Promise<InvestmentCalendarData> {
  const params = new URLSearchParams({ currency, year: String(year), month: String(month), source });
  const response = await accountSelectionRequest("/api/investments/analysis/calendar", params, accountIds, signal);
  if (!response.ok) throw new Error(`The value calendar could not be loaded (${response.status}).`);
  const data = await response.json() as InvestmentCalendarData;
  if (data.metric !== "account_value_change" || !Array.isArray(data.days) || !Array.isArray(data.months) || !Array.isArray(data.points) || !data.summary) throw new Error("The value calendar response was incomplete.");
  return data;
}
