import type { MerchantAlias, MerchantKind, SpendingTreatment } from "./api";

export interface MerchantExample {
  id: number; description: string; txn_date: string; amount: number;
  account: string; merchant: string | null; category: string | null;
}
export interface LearnedCategory {
  category: string; support: number; reviewed: number; conflicting: number; explanation: string;
}
export interface MerchantInboxItem {
  key: string; label: string; context: string; example_id: number; n_transactions: number;
  outgoing_total: number; uncategorized_count: number; needs_identity_count: number;
  categories: string[]; merchant_ids: number[]; last_date: string;
  examples: MerchantExample[]; learned_suggestion: LearnedCategory | null;
}
export interface MerchantInbox {
  items: MerchantInboxItem[]; total_labels: number; uncategorized_labels: number; method: string;
}
export interface MerchantQuickInput {
  example_id: number; merchant_id?: number | null; canonical_name: string;
  category: string; kind: MerchantKind; spending_treatment: SpendingTreatment; preview_token?: string;
  additional_example_ids?: number[];
  confirm_own_transfer?: boolean;
  update_category?: boolean;
}
export interface SimilarNameCandidate {
  example_id: number; label: string; context: MerchantAlias["context"]; n_transactions: number;
  outgoing_total: number; examples: MerchantExample[]; score: number; reason: string;
}
export interface MerchantQuickPreview {
  pattern: string; context: string; canonical_name: string; category: string | null;
  kind: MerchantKind; spending_treatment: SpendingTreatment; matched_transactions: number;
  outgoing_total: number; preserved_reviews: number; examples: MerchantExample[];
  conflict: { merchant_id: number; canonical_name: string } | null;
  own_transfer_confirmation_required?: boolean;
  updates_saved_category?: boolean;
  preview_token: string; learned_suggestion: LearnedCategory | null;
  similar_names: SimilarNameCandidate[]; additional_aliases: SimilarNameCandidate[]; alias_count: number;
}
export interface MerchantQuickResult extends MerchantQuickPreview {
  ok: boolean; merchant_id: number; updated_transactions: number; message: string;
}
