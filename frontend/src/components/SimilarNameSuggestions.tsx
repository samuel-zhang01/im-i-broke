import { Heading } from "./Heading";
import { useEffect, useState } from "react";
import { api, type SimilarNamesQuery } from "../api";
import type { SimilarNameCandidate } from "../merchantInbox";
import { money, shortDate } from "../format";

/** Read-only suggestions; the parent owns every explicit selection and save. */
export default function SimilarNameSuggestions({ query, selected = [], onToggle, onReview, disabled = false }: {
  query: SimilarNamesQuery;
  selected?: SimilarNameCandidate[];
  onToggle?: (item: SimilarNameCandidate, checked: boolean) => void;
  onReview?: (item: SimilarNameCandidate) => void;
  disabled?: boolean;
}) {
  const [items, setItems] = useState<SimilarNameCandidate[]>([]);
  const [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [visibleCount, setVisibleCount] = useState(5);
  const payload = JSON.stringify(query);
  const ready = query.canonical_name.trim().length >= 3 || (query.patterns ?? []).some(value => value.trim().length >= 3);
  useEffect(() => {
    let current = true;
    setItems([]); setError(""); setVisibleCount(5);
    if (!ready) { setLoading(false); return; }
    setLoading(true);
    const timer = setTimeout(() => {
      api.similarMerchantNames(JSON.parse(payload)).then(result => {
        if (current) setItems(result.items);
      }).catch(e => current && setError(String(e))).finally(() => current && setLoading(false));
    }, 350);
    return () => { current = false; clearTimeout(timer); };
  }, [payload, ready, retry]);
  if (!ready) return null;
  const available = items.filter(item => !selected.some(picked => picked.example_id === item.example_id));
  const shown = [...selected, ...available.slice(0, visibleCount)];
  const remaining = Math.max(0, available.length - visibleCount);
  return <section className="min-w-0 space-y-3 rounded-lg border border-line bg-surface2/50 p-3" aria-label="Similar statement names">
    <Heading level={3} variant="subsection">Similar statement names</Heading>
    <p className="text-xs leading-relaxed text-mist">{onToggle ? "Select other spellings that belong to this name. They are added as exact aliases when you save this rule." : "These labels resemble your current name choice. Open a match to preview applying the same name and category."} Matching runs locally; similarity is a clue to review.</p>
    {loading && <p role="status" className="text-xs text-mist">Finding similar names…</p>}
    {error && <p role="alert" className="text-xs text-coral">{error} <button type="button" className="underline" onClick={() => setRetry(n => n + 1)}>Retry suggestions</button></p>}
    {!loading && !error && !shown.length && <p className="text-xs text-mist">No additional similar labels found in your imported payments.</p>}
    <div className="space-y-3">{shown.map(item => <div key={item.example_id} className="min-w-0 border-t border-line pt-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <label className="flex min-w-0 flex-1 items-start gap-2 text-sm font-medium">
          {onToggle && <input type="checkbox" className="mt-1 shrink-0" checked={selected.some(picked => picked.example_id === item.example_id)} disabled={disabled || loading || (selected.length >= 30 && !selected.some(picked => picked.example_id === item.example_id))} onChange={e => onToggle(item, e.target.checked)} />}
          <span className="min-w-0 break-words">{item.label}</span>
        </label>
        {onReview && <button type="button" className="btn text-xs" disabled={disabled || loading} onClick={() => onReview(item)}>Preview this match</button>}
      </div>
      <p className="mt-1 text-xs text-mist">{item.n_transactions} {item.n_transactions === 1 ? "transaction" : "transactions"} · {money(item.outgoing_total)} outgoing · {item.context.endsWith("transfer") ? "bank transfers" : item.context.endsWith("card") ? "card payments" : "payments"}</p>
      <p className="mt-1 text-xs text-mist">{item.reason}</p>
      <details className="mt-2 text-xs"><summary className="cursor-pointer underline underline-offset-4">Original examples</summary><div className="mt-2 space-y-2">{item.examples.map(example => <div key={example.id}><p className="break-words">{example.description}</p><p className="text-mist">{shortDate(example.txn_date)} · {example.account} · {money(example.amount)}</p></div>)}</div></details>
    </div>)}</div>
    {!loading && !error && <div className="flex flex-wrap items-center gap-2">
      {remaining > 0 && <button type="button" className="btn text-xs" onClick={() => setVisibleCount(count => count + 5)}>Show {Math.min(5, remaining)} more matches ({remaining} remaining)</button>}
      {visibleCount > 5 && available.length > 5 && <button type="button" className="btn text-xs" onClick={() => setVisibleCount(5)}>Show fewer matches</button>}
      {selected.length > 0 && <span className="text-xs text-mist">{selected.length} selected · all selected labels stay visible</span>}
    </div>}
  </section>;
}
