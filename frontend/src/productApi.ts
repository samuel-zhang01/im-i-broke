import { authRequest } from "./authRequest";
import { dataChanged } from "./dataEvents";

export async function productRequest<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await authRequest(`/api/${path}`, { method, signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error(response.status === 413 ? "The request is too large. Split this CSV into smaller files and try again." : "The server returned an unreadable response. Please try again."); }
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((item: { msg?: string }) => item.msg || "Invalid value").join(". ")
      : "The change could not be saved. Please try again.");
  }
  if (method !== "GET" && !path.endsWith("/preview") && !path.endsWith("/validate")) dataChanged(path);
  return result as T;
}

export type WorkspacePreferences = { reporting_currency: string; locale: string; timezone: string };
export const fetchWorkspacePreferences = (signal?: AbortSignal) => productRequest<WorkspacePreferences>("workspace/preferences", "GET", undefined, signal);
export type WorkspaceCalendar = { today: string; timezone: string };
export const fetchWorkspaceCalendar = (signal?: AbortSignal) => productRequest<WorkspaceCalendar>("workspace/calendar", "GET", undefined, signal);
export type CategoryTreatment = "normal" | "income" | "transfer" | "investment" | "refund";
export type WorkspaceCategory = { id: string; name: string; archived: boolean; sort_order: number; treatment: CategoryTreatment };
export type WorkspaceAccount = {
  id: string; display_name: string | null; account_type: string; institution: string; currency: string;
  aliases: string[]; include_in_reports: boolean; include_in_planning: boolean; archived: boolean;
  ownership_share: number; opened_on: string | null; closed_on: string | null; origin: string; transaction_count: number;
  manual_balance?: { amount: number; currency: string; as_of: string; recorded_at: string } | null;
  connections: { id: string; name: string; amount_multiplier: number; balance_multiplier: number; present: boolean; excluded: boolean }[];
};
export type ConnectionProvider = { id: string; label: string; auth_methods: string[]; capabilities: string[]; credential_fields: { name: string; label: string; secret: boolean }[] };
export type Connection = { id: string; provider: string; label: string; source: "saved" | "environment"; status: string; enabled: boolean; auth_method?: string;
  created_at: string | null; updated_at: string | null; last_checked_at: string | null; last_synced_at: string | null; last_error: string | null; capabilities: string[] };
export type CsvMapping = { date_column: string; description_column: string; amount_column?: string; debit_column?: string; credit_column?: string;
  date_format: string; currency: string; currency_column?: string; id_column?: string; delimiter: string; decimal_separator: string; amount_multiplier: number };
export type CsvInput = { account_id: string; csv_text: string; mapping: CsvMapping; source_name: string; source_filename?: string };
export type CsvPreviewRow = { row: number; date: string; description: string; amount: number; currency: string; status: "new" | "duplicate" | "review"; reason?: string };
export type CsvPreview = { preview_token: string; columns: string[]; rows: CsvPreviewRow[]; review_rows: CsvPreviewRow[];
  row_count: number; new_count: number; duplicate_count: number; review_count: number; errors: (string | { row?: number; message?: string; error?: string })[]; can_import: boolean };
export type CsvTemplate = { id: string; name: string; mapping: CsvMapping };

/** Parse just the header for mapping suggestions; the server validates every row. */
export function csvColumns(text: string, delimiter: string): string[] {
  const columns: string[] = []; let value = ""; let quoted = false;
  const input = text.replace(/^\uFEFF/, "");
  for (let index = 0; index < input.length; index++) {
    const char = input[index];
    if (char === '"') {
      if (quoted && input[index + 1] === '"') { value += '"'; index++; } else quoted = !quoted;
    } else if (!quoted && char === delimiter) { columns.push(value.trim()); value = ""; }
    else if (!quoted && (char === "\n" || char === "\r")) break;
    else value += char;
  }
  columns.push(value.trim());
  return columns.filter(Boolean);
}
