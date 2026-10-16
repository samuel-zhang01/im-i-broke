import { useEffect, useId, useState, type FormEvent } from "react";
import {
  api,
  type MerchantKind,
  type SpendingTreatment,
  type ReviewResult,
  type TransactionReview as ReviewFields,
} from "../api";
import { DialogActions, ErrorBox, Modal, Pill, Spinner } from "./ui";
import { useCategoryOptions, CategoryOptionsError } from "./CategoryOptions";
import { shortDate } from "../format";
import { nativeMoney, bankTimestamp } from "../banking";
import MerchantQuickClassify from "./MerchantQuickClassify";
import { Disclosure } from "./Disclosure";
import SimilarNameSuggestions from "./SimilarNameSuggestions";
import type { MerchantQuickInput, SimilarNameCandidate } from "../merchantInbox";

const kindLabels: Record<MerchantKind, string> = {
  unknown: "Unknown",
  merchant: "Business / merchant",
  person: "Friend / person",
  self: "Me / my own account",
  financial: "Financial provider",
};
const treatmentLabels: Record<SpendingTreatment, string> = {
  normal: "Count in spending / income",
  self_transfer: "Own-account transfer",
  reimbursement: "Reimbursement / shared-cost payback",
};

type Form = {
  category: string;
  merchant: string;
  merchant_kind: MerchantKind | "";
  spending_treatment: SpendingTreatment | "";
  exclude_from_analysis: boolean;
  note: string;
};

export default function TransactionReview({
  id,
  reason,
  onClose,
  onSaved,
}: {
  id: number;
  reason?: string;
  onClose: () => void;
  onSaved: (message?: string) => void;
}) {
  const formId = useId();
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [teachName, setTeachName] = useState(false);
  const [teachValues, setTeachValues] = useState<Partial<MerchantQuickInput>>({});
  const [showSimilar, setShowSimilar] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const [resolution, setResolution] = useState<"expected" | "issue" | "pending">("expected");
  const [customCategory, setCustomCategory] = useState(false);

  useEffect(() => {
    let active = true;
    setResult(null);
    setForm(null);
    setError(null);
    api
      .review(id)
      .then((data) => {
        if (!active) return;
        setResult(data);
        setResolution(data.review?.resolution && data.review.resolution !== "pending" ? data.review.resolution : "expected");
        const saved = data.review;
        setShowDetails(Boolean(saved?.note || saved?.exclude_from_analysis || data.transaction.exclude_from_analysis));
        setForm({
          category: saved?.category ?? "",
          merchant: saved?.merchant ?? "",
          merchant_kind: saved?.merchant_kind ?? "",
          spending_treatment: saved?.spending_treatment ?? "",
          exclude_from_analysis: Boolean(
            saved?.exclude_from_analysis ??
              data.transaction.exclude_from_analysis,
          ),
          note: saved?.note ?? "",
        });
      })
      .catch((e) => active && setError(String(e)));
    return () => {
      active = false;
    };
  }, [id, nonce]);

  function change<K extends keyof Form>(key: K, value: Form[K]) {
    setForm((previous) =>
      previous ? { ...previous, [key]: value } : previous,
    );
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setBusy(true);
    setError(null);
    const review: ReviewFields = {
      ...(reason ? {resolution} : {}),
      category: form.category || null,
      merchant: form.merchant.trim() || null,
      merchant_kind: form.merchant_kind || null,
      spending_treatment: form.spending_treatment || null,
      exclude_from_analysis: form.exclude_from_analysis,
      note: form.note.trim() || null,
    };
    try {
      await api.saveReview(id, review);
      onSaved();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function reset() {
    setBusy(true);
    setError(null);
    try {
      await api.resetReview(id);
      onSaved();
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  const t = result?.transaction;
  const source = t?.classification;
  const sourceUrl =
    typeof source?.source_url === "string" &&
    /^https?:\/\//.test(source.source_url)
      ? source.source_url
      : null;
  const categories = useCategoryOptions(t?.category, form?.category);
  const categoryOptions = categories.options;
  const hasReview =
    !!result?.review &&
    Object.entries(result.review).filter(([key]) => key !== "resolution").map(([, value]) => value).some(
      (value) => value !== null && value !== undefined,
    );
  const treatment =
    form?.spending_treatment ||
    (form?.merchant_kind === "self"
      ? "self_transfer"
      : t?.spending_treatment) ||
    "normal";
  const changedIdentity = form && t && ((form.merchant.trim() && form.merchant.trim() !== t.merchant)
    || (form.merchant_kind && form.merchant_kind !== t.merchant_kind)
    || (form.spending_treatment && form.spending_treatment !== t.spending_treatment));
  const draftMerchantId = changedIdentity ? null : (source?.merchant_id as number | undefined) ?? null;

  function openMatching(item?: SimilarNameCandidate) {
    if (!form || !t) return;
    setTeachValues({
      merchant_id: draftMerchantId,
      update_category: draftMerchantId !== null && !!form.category && form.category !== t.category,
      canonical_name: form.merchant.trim() || t.merchant || t.source_merchant || "",
      category: form.category || t.category || "Uncategorized",
      kind: form.merchant_kind || t.merchant_kind || "merchant",
      spending_treatment: treatment,
      additional_example_ids: item ? [item.example_id] : [],
    });
    setTeachName(true);
  }
  const unsavedDetails = form && result && (
    form.note.trim() !== (result.review?.note || "") ||
    form.exclude_from_analysis !== Boolean(result.review?.exclude_from_analysis ?? t?.exclude_from_analysis) ||
    Boolean(reason && resolution !== (result.review?.resolution || "pending"))
  );
  if (teachName) return <MerchantQuickClassify id={id} initialValues={teachValues}
    unsavedReviewDetails={Boolean(unsavedDetails)}
    onClose={() => setTeachName(false)} onSaved={message => { onSaved(message); onClose(); }} />;

  return (
    <Modal title="Review transaction" onClose={() => !busy && onClose()} footer={t && form && <DialogActions formId={formId} onCancel={onClose} busy={busy} label={busy ? "Saving…" : reason && resolution !== "pending" ? "Save & resolve" : "Save review"}>
      {hasReview && <button type="button" className="btn text-coral" disabled={busy} onClick={() => void reset()}>Reset detail edits</button>}
    </DialogActions>}>
      <CategoryOptionsError state={categories} />
      {!result && !error && <Spinner label="Loading transaction…" />}
      {error && (
        <ErrorBox
          error={error}
          retry={!result ? () => setNonce((n) => n + 1) : undefined}
        />
      )}
      {t && form && (
        <form id={formId} onSubmit={save}><fieldset disabled={busy} className="min-w-0 space-y-4">
          {reason && <label className="block text-sm font-medium">Review outcome<select className="input mt-2 w-full" value={resolution} onChange={e => setResolution(e.target.value as typeof resolution)}><option value="expected">Looks OK — resolve as expected</option><option value="issue">Issue confirmed — resolve</option><option value="pending">Keep open for investigation</option></select><span className="mt-2 block text-xs text-mist">Expected payments teach similar flags to leave the queue. Spending stays unchanged. Check Learned as normal or Resolved history to return payments to review.</span></label>}
          <div className="rounded-lg border border-line bg-surface2 p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0 flex-1 basis-48">
                <p className="font-semibold">{t.merchant || t.description}</p>
                <p className="mt-1 text-xs text-mist">
                  {shortDate(t.txn_date)} · {t.account}
                </p>
              </div>
              <p
                className={`shrink-0 text-xl font-semibold tabular-nums ${t.amount < 0 ? "text-coral" : "text-kelp"}`}
              >
                {nativeMoney(t.amount, t.currency || "GBP")}
              </p>
            </div>
            <p className="mt-3 break-words text-xs text-mist">
              <span className="font-medium">Original description:</span>{" "}
              {t.description}
            </p>
            {result?.bank_evidence && <Disclosure title="Original bank feed evidence" className="mt-3"><div className="space-y-2 break-words text-xs leading-relaxed text-mist">{result.bank_evidence.merchant && <p><span className="font-medium text-foam">Bank label:</span> {result.bank_evidence.merchant}</p>}{result.bank_evidence.description && <p><span className="font-medium text-foam">Description:</span> {result.bank_evidence.description}</p>}<p>{result.bank_evidence.bank} · {shortDate(result.bank_evidence.date)} · {nativeMoney(result.bank_evidence.amount, result.bank_evidence.currency)}</p><p>Fetched {bankTimestamp(result.bank_evidence.fetched_at)} · {result.bank_evidence.status === "matched" ? "Linked to saved history" : "Imported from bank feed"}</p><p className="border-t border-line pt-2">{result.bank_evidence.note}</p></div></Disclosure>}
            {reason && (
              <p className="mt-3 text-xs text-amber">
                Flagged for review: {reason}
              </p>
            )}
            <details className="mt-3 text-xs text-mist">
              <summary className="cursor-pointer underline underline-offset-4">
                Why this name and category?
              </summary>
              <div className="mt-2 space-y-2 leading-relaxed">
                <p>
                  {source?.explanation ||
                    "The current category comes from the statement or a local description rule."}
                </p>
                {typeof source?.category_explanation === "string" && (
                  <p>{source.category_explanation}</p>
                )}
                {typeof source?.review_explanation === "string" && (
                  <p>{source.review_explanation}</p>
                )}
                {t.source_merchant && (
                  <p>Original merchant label: {t.source_merchant}</p>
                )}
                {source?.source && (
                  <Pill tone="mute">{source.source.replace(/_/g, " ")}</Pill>
                )}
                {sourceUrl && (
                  <a
                    href={sourceUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="ml-2 underline"
                  >
                    Merchant source ↗
                  </a>
                )}
              </div>
            </details>
          </div>
          <div className="grid gap-4 sm:grid-cols-2 [&>label]:min-w-0 [&_input]:min-w-0 [&_select]:min-w-0">
            <label className="space-y-1 text-xs font-medium">
              Name for this transaction
              <input
                className="input w-full"
                maxLength={120}
                placeholder={t.merchant || t.description}
                value={form.merchant}
                onChange={(e) => change("merchant", e.target.value)}
              />
              <span className="block font-normal text-mist">
                Leave blank to follow merchant rules.
              </span>
            </label>
            <label className="space-y-1 text-xs font-medium">
              Spending category
              <select
                className="input w-full"
                value={customCategory ? "__custom" : form.category}
                onChange={(e) => { setCustomCategory(e.target.value === "__custom"); if (e.target.value !== "__custom") change("category", e.target.value); }}
              >
                <option value="">
                  Automatic ({t.category || "Uncategorized"})
                </option>
                {categoryOptions.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
                <option value="__custom">Custom category…</option>
              </select>
              {customCategory && <input className="input w-full" aria-label="Custom transaction category" required maxLength={120} value={form.category} onChange={e => change("category", e.target.value)} />}
            </label>
            <label className="space-y-1 text-xs font-medium">
              Who is this?
              <select
                className="input w-full"
                value={form.merchant_kind}
                onChange={(e) =>
                  change("merchant_kind", e.target.value as MerchantKind | "")
                }
              >
                <option value="">
                  Automatic ({kindLabels[t.merchant_kind || "unknown"]})
                </option>
                {Object.entries(kindLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1 text-xs font-medium">
              How should it count?
              <select
                className="input w-full"
                value={form.spending_treatment}
                onChange={(e) =>
                  change(
                    "spending_treatment",
                    e.target.value as SpendingTreatment | "",
                  )
                }
              >
                <option value="">
                  Automatic ({treatmentLabels[t.spending_treatment || "normal"]}
                  )
                </option>
                {Object.entries(treatmentLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {t.account_type !== "investment" && <div className="space-y-3 rounded-lg border border-line p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div><p className="text-sm font-medium">Apply to matching payments</p><p className="mt-1 text-xs text-mist">Preview an exact rule for past and future imports.</p></div>
              <button type="button" className="btn text-xs" onClick={() => openMatching()}>Use this name for matching payments…</button>
            </div>
            <button type="button" className="min-h-11 text-xs text-mist underline underline-offset-4" aria-expanded={showSimilar} aria-controls="transaction-similar-names" onClick={() => setShowSimilar(value => !value)}>{showSimilar ? "Hide" : "Find"} similar statement names</button>
            {showSimilar && <div id="transaction-similar-names"><SimilarNameSuggestions query={{
              example_id: id, canonical_name: form.merchant.trim() || t.merchant || t.source_merchant || "",
              kind: form.merchant_kind || t.merchant_kind || "merchant", spending_treatment: treatment,
              merchant_id: draftMerchantId,
            }} onReview={openMatching} disabled={busy} /></div>}
          </div>}
          <button type="button" className="flex min-h-11 w-full items-center justify-between border-t border-line pt-3 text-left text-sm font-medium" aria-expanded={showDetails} aria-controls="transaction-review-details" onClick={() => setShowDetails(value => !value)}>Notes &amp; exclusions<span aria-hidden="true">{showDetails ? "−" : "+"}</span></button>
          {showDetails && <div id="transaction-review-details" className="space-y-4">
          <p className="rounded-lg border border-line px-3 py-2 text-xs leading-relaxed text-mist">
            {treatment === "self_transfer"
              ? "Moving your own money is excluded from both spending and income. It stays in the transaction record and account balance."
              : treatment === "reimbursement"
                ? "Money received offsets spending; money paid remains an expense. Choose the category of the shared purchase when you know it."
                : "Payments to friends remain expenses. Money received counts as income unless you identify it as a reimbursement or your own transfer."}
            {t.transfer_group &&
              treatment === "normal" &&
              " This row is currently paired with another account transfer. An explicit “Count in spending / income” choice overrides that link."}
          </p>
          <label className="flex items-start gap-3 rounded-lg border border-line p-3 text-sm">
            <input
              type="checkbox"
              className="mt-1 shrink-0"
              checked={form.exclude_from_analysis}
              onChange={(e) =>
                change("exclude_from_analysis", e.target.checked)
              }
            />
            <span>
              Exclude this transaction from spending analytics
              <span className="mt-1 block text-xs leading-relaxed text-mist">
                Use for a one-off you want outside your planning baseline. The
                original amount and account balance stay intact, and the
                exclusion is shown in the totals breakdown.
              </span>
            </span>
          </label>
          <label className="block space-y-1 text-xs font-medium">
            Review note
            <textarea
              className="input min-h-20 w-full resize-y"
              maxLength={2000}
              placeholder="For example: my share of dinner, holiday one-off, or moving money to savings"
              value={form.note}
              onChange={(e) => change("note", e.target.value)}
            />
          </label>
          </div>}

        </fieldset></form>
      )}
    </Modal>
  );
}
