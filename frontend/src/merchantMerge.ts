import type { MerchantAlias, MerchantRule } from "./api";
import { authRequest } from "./authRequest";
import { dataChanged } from "./dataEvents";

export interface MerchantMergeInput {
  target_id: number;
  source_ids: number[];
}

export interface MerchantMergePreview {
  target: MerchantRule;
  sources: MerchantRule[];
  aliases: MerchantAlias[];
  matched_transactions: number;
  changed_transactions: number;
  preserved_reviews: number;
  warnings: string[];
  preview_token: string;
}

export interface MerchantMergeResult {
  ok: boolean;
  target_id: number;
  merged_ids: number[];
  updated_transactions: number;
  preserved_reviews: number;
  message: string;
}

export interface MerchantMergeSuggestion {
  target_id: number;
  source_id: number;
  target_name: string;
  source_name: string;
  reason: string;
  match_type: string;
  score: number;
}

export function fetchMerchantMergeSuggestions(signal?: AbortSignal): Promise<{ items: MerchantMergeSuggestion[]; total: number }> {
  return request("/suggestions?limit=100", { signal });
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  options.signal?.addEventListener("abort", cancel, { once: true });
  if (options.signal?.aborted) controller.abort();
  const saving = path === "" && options.method === "POST";
  const timer = setTimeout(cancel, saving ? 120000 : 30000);
  try {
    const response = await authRequest(`/api/merchants/merge${path}`, { ...options, signal: controller.signal });
    if (!response.ok) {
      let detail = "";
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
        else if (Array.isArray(body.detail)) detail = body.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join("; ");
      } catch { /* A proxy error may not contain JSON. */ }
      throw Object.assign(new Error(detail || `Could not ${saving ? "merge these names" : "check these names"} (${response.status}). Please try again.`), { status: response.status });
    }
    return await response.json() as T;
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error(saving
      ? "The merge is taking longer than expected and may have completed. Refresh saved names before trying again."
      : "The server took too long to check these names. Refresh the preview to try again.");
    if (error instanceof TypeError) throw new Error("Could not reach your Am I Broke? server. Check your connection and try again.");
    throw error;
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", cancel);
  }
}

const post = (body: unknown, signal?: AbortSignal): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
});

export function previewMerchantMerge(input: MerchantMergeInput, signal?: AbortSignal): Promise<MerchantMergePreview> {
  return request("/preview", post(input, signal));
}

export async function saveMerchantMerge(input: MerchantMergeInput & { preview_token: string }): Promise<MerchantMergeResult> {
  const result = await request<MerchantMergeResult>("", post(input));
  dataChanged("merchants/merge");
  return result;
}
