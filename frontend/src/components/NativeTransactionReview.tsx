import { useState } from "react";
import { productRequest } from "../productApi";
import { nativeMoney } from "../banking";
import { ErrorBox, Modal } from "./ui";
import { CategoryOptionsError, useCategoryOptions } from "./CategoryOptions";

export type NativeReportEvent = {
  event_id: string; account_name: string; txn_date: string; description: string; category: string;
  native_amount: number; currency: string; reporting_amount: number | null;
  fx: null | { source: string; rate: number; date: string };
  included: boolean; omission_reason?: string; analysis_component?: string;
  evidence_version?: string; reviewed?: boolean; review_stale?: boolean; review_note?: string;
  possible_overlap?: boolean; overlap_decision?: "distinct" | "duplicate";
  spending_treatment?: string; exclude_from_analysis?: boolean | number;
  account_id?: string; source_review?: boolean; review_reason?: string | null;
};

export default function NativeTransactionReview({ event, onClose, onSaved }: {
  event: NativeReportEvent; onClose: () => void; onSaved: () => void;
}) {
  const [category, setCategory] = useState(event.category || "Uncategorized");
  const categories = useCategoryOptions(category);
  const [treatment, setTreatment] = useState(event.spending_treatment || "normal");
  const [excluded, setExcluded] = useState(Boolean(event.exclude_from_analysis));
  const [overlap, setOverlap] = useState(event.overlap_decision || "");
  const [note, setNote] = useState(event.review_note || "");
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const path = `workspace/native-transactions/${event.event_id.slice(5)}/review`;
  async function save(reset = false) {
    setBusy(true); setError("");
    try {
      await productRequest(path, reset ? "DELETE" : "PUT", reset ? undefined : {
        evidence_version: event.evidence_version, category, spending_treatment: treatment,
        excluded, overlap_decision: overlap || null, note,
      });
      onSaved();
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  return <Modal title="Review native bank transaction" onClose={onClose}>
    <form onSubmit={e => { e.preventDefault(); void save(); }} className="space-y-4">
      <p className="text-sm">{event.description}<br /><span className="text-mist">{event.txn_date} · {event.account_name} · {nativeMoney(event.native_amount, event.currency)}</span></p>
      <p className="text-xs text-mist">Your review changes report treatment. The bank’s original amount and evidence are retained.</p>
      {event.review_stale && <p role="status" className="text-sm">The bank evidence changed after your last review. Check the original amount before saving again.</p>}
      {error && <ErrorBox error={error} />}
      <CategoryOptionsError state={categories} />
      <fieldset disabled={busy} className="space-y-4">
        <label className="block text-sm">Category<select className="input mt-1 block w-full" value={category} onChange={e => setCategory(e.target.value)}>{categories.options.map(name => <option key={name}>{name}</option>)}</select></label>
        <label className="block text-sm">Treatment<select className="input mt-1 block w-full" value={treatment} onChange={e => setTreatment(e.target.value)}><option value="normal">Count in income / spending</option><option value="self_transfer">Own-account transfer</option><option value="reimbursement">Reimbursement / shared costs</option></select></label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={excluded} onChange={e => setExcluded(e.target.checked)} />Exclude from analysis</label>
        {event.possible_overlap && <label className="block text-sm">Possible source overlap<select className="input mt-1 block w-full" value={overlap} onChange={e => setOverlap(e.target.value)}><option value="">Keep pending review</option><option value="duplicate">Same transaction already imported</option><option value="distinct">Separate transaction — include both</option></select><span className="mt-1 block text-xs text-mist">Confirm using the source records and explain your decision below.</span></label>}
        <label className="block text-sm">Review note<textarea className="input mt-1 block w-full" maxLength={1000} required={Boolean(overlap)} minLength={overlap ? 8 : undefined} value={note} onChange={e => setNote(e.target.value)} /></label>
        <div className="flex flex-wrap gap-2"><button className="btn">{busy ? "Saving…" : "Save transaction review"}</button>{(event.reviewed || event.review_stale) && <button type="button" className="btn" onClick={() => void save(true)}>Reset review</button>}</div>
      </fieldset>
    </form>
  </Modal>;
}
