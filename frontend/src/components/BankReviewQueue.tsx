import { Heading } from "./Heading";
import { useEffect, useRef, useState } from "react";
import { resolveBankReview, type BankReview } from "../bankingApi";
import { nativeMoney } from "../banking";
import { shortDate } from "../format";
import { useWorkspaceState } from "../workspaceState";
import { Empty, ErrorBox } from "./ui";

function ReviewComparison({ item, onResolved, onSaving, loading }: {
  item: BankReview; onResolved: (id: number) => void; onSaving: (saving: boolean) => void; loading: boolean;
}) {
  // A suggested candidate is evidence, never an automatic matching decision.
  const [candidate, setCandidate] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const linkedRevision = item.transaction_id != null;
  const chosen = item.candidates.find(value => value.id === Number(candidate));
  async function resolve(action: "match" | "import" | "ignore") {
    if (saving || loading || (action === "match" && !chosen)) return;
    setSaving(true); onSaving(true); setError(null);
    try { await resolveBankReview(item.id, action, action === "match" ? chosen!.id : undefined); onResolved(item.id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "This transaction could not be reviewed."); }
    finally { setSaving(false); onSaving(false); }
  }
  return <div className="space-y-3">
    <div className="grid items-stretch gap-3 lg:grid-cols-2">
      <section aria-label="Bank transaction" className="min-w-0 rounded-lg border border-line bg-deep/40 p-3">
        <p className="eyebrow">Bank transaction</p>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-2"><Heading level={4} variant="subsection" data-review-heading tabIndex={-1} className="min-w-0 break-words outline-none scroll-mt-24">{item.merchant || item.description || "Bank transaction"}</Heading><p className="text-base font-semibold tabular-nums">{nativeMoney(item.amount, item.currency)}</p></div>
        <p className="mt-1 text-xs text-mist">{shortDate(item.date)} · {item.currency}{item.is_pending ? " · Pending" : " · Booked"}</p>
        {item.description && item.description !== item.merchant && <p className="mt-2 break-words text-xs leading-relaxed text-mist">{item.description}</p>}
      </section>
      <section aria-label="Saved transaction comparison" className="min-w-0 rounded-lg border border-line p-3">
        <p className="eyebrow">Saved transaction</p>
        {linkedRevision ? <p className="mt-2 text-xs leading-relaxed text-mist">This bank item is already linked. The original ledger entry is retained; a changed bank response cannot overwrite it here.</p> : item.candidates.length ? <>
          <label className="field-label mt-2"><span>Existing transaction</span><select className="input w-full" value={candidate} onChange={event => setCandidate(event.target.value)} disabled={saving || loading}><option value="">Choose a saved transaction to compare</option>{item.candidates.map(value => <option key={value.id} value={value.id}>{shortDate(value.txn_date)} · {value.merchant || value.description} · {nativeMoney(value.amount, item.currency)}</option>)}</select></label>
          {chosen ? <div className="mt-3 space-y-1 text-xs"><div className="flex flex-wrap justify-between gap-2"><p className="min-w-0 break-words text-sm font-medium">{chosen.merchant || chosen.description}</p><p className="text-sm font-semibold tabular-nums">{nativeMoney(chosen.amount, item.currency)}</p></div><p className="text-mist">{shortDate(chosen.txn_date)} · {item.currency}{chosen.category ? ` · ${chosen.category}` : ""}</p>{chosen.description && chosen.description !== chosen.merchant && <p className="break-words leading-relaxed text-mist">{chosen.description}</p>}<p className={chosen.amount === item.amount && chosen.txn_date === item.date ? "text-kelp" : "text-amber"}>{chosen.amount === item.amount ? "Same amount" : "Different amount"} · {chosen.txn_date === item.date ? "Same date" : "Different date"}</p></div> : <p className="mt-2 text-xs leading-relaxed text-mist">Compare the amount, date and description before matching.</p>}
        </> : <p className="mt-2 text-xs leading-relaxed text-mist">No saved transaction was suggested. Add this item only if it is missing from your history.</p>}
      </section>
    </div>
    <p className="text-xs leading-relaxed text-mist">{item.review_reason || "This bank transaction may overlap your existing history. Confirm its treatment before adding it to your ledger."}</p>
    <div className="flex flex-wrap items-center gap-2">{!linkedRevision && item.candidates.length > 0 && <button className="btn-primary" disabled={!chosen || saving || loading} onClick={() => void resolve("match")}>Match existing</button>}{!linkedRevision && <button className="btn" disabled={saving || loading} onClick={() => void resolve("import")}>Add as new transaction</button>}<button className="btn text-mist" disabled={saving || loading} onClick={() => void resolve("ignore")}>{linkedRevision ? "Keep original ledger entry" : "Keep in cache only"}</button>{saving && <span role="status" className="text-xs text-mist">Saving decision…</span>}</div>
    {error && <ErrorBox error={error} />}
  </div>;
}

/** One comparison at a time keeps large review queues usable on small screens. */
export default function BankReviewQueue({ accountKey, items, total, offset, loading, onResolved }: {
  accountKey: string; items: BankReview[]; total: number; offset: number; loading: boolean; onResolved: (id: number) => void;
}) {
  const [selected, setSelected] = useWorkspaceState<number | null>(`banking:${accountKey}:review`, null);
  const [saving, setSaving] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const focusOnSelection = useRef(false);
  const item = items.find(value => value.id === selected) ?? items[0];
  const index = item ? items.indexOf(item) : 0;
  useEffect(() => {
    if (!focusOnSelection.current || !item) return;
    focusOnSelection.current = false;
    container.current?.querySelector<HTMLElement>("[data-review-heading]")?.focus({ preventScroll: true });
  }, [item?.id]);
  function select(id: number) { focusOnSelection.current = true; setSelected(id); }
  if (!item) return <Empty>No overlaps to review for these filters.</Empty>;
  return <div ref={container} className="rounded-xl border border-line p-3 sm:p-4" aria-label="Overlap review">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-line pb-3">
      <p className="text-xs font-medium">Review {offset + index + 1} of {total.toLocaleString()} <span className="font-normal text-mist">awaiting a decision</span></p>
      <div className="flex w-full min-w-0 max-w-full items-center justify-end gap-2 sm:w-auto"><label className="min-w-0 flex-1 sm:w-64"><span className="sr-only">Review item on this page</span><select className="input w-full" value={item.id} disabled={saving || loading} onChange={event => select(Number(event.target.value))}>{items.map((value, itemIndex) => <option key={value.id} value={value.id}>{offset + itemIndex + 1}. {value.merchant || value.description || "Bank transaction"}</option>)}</select></label><button className="btn h-11 w-11 shrink-0 !p-0" aria-label="Previous review on this page" title="Previous review" disabled={index === 0 || saving || loading} onClick={() => select(items[index - 1].id)}>←</button><button className="btn h-11 w-11 shrink-0 !p-0" aria-label="Next review on this page" title="Next review" disabled={index + 1 >= items.length || saving || loading} onClick={() => select(items[index + 1].id)}>→</button></div>
    </div>
    <ReviewComparison key={item.id} item={item} loading={loading} onSaving={setSaving} onResolved={id => { setSelected(items[index + 1]?.id ?? items[index - 1]?.id ?? null); onResolved(id); }} />
  </div>;
}
