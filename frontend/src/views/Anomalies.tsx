import { Heading } from "../components/Heading";
import MobileDisclosure from "../components/MobileDisclosure";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Pill, Spinner, useAsync, PageControls, useCompactLayout } from "../components/ui";
import TransactionReview from "../components/TransactionReview";
import { money, shortDate } from "../format";

type ReasonKind = "all" | "outlier" | "duplicate";
type SortKey = "priority" | "date" | "amount" | "z";

export default function Anomalies() {
  const shortMobile = useCompactLayout("(max-width: 639px) and (max-height: 700px)");
  const [status, setStatus] = useState<"open" | "resolved" | "learned">("open");
  const anomalies = useAsync(signal => api.anomalies(status, signal), [status]);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  async function resolve(id: number, resolution: "expected" | "pending") {
    setBusyId(id); setActionError(null);
    try { await api.saveReview(id, {resolution}); anomalies.refetch(); setNotice(resolution === "expected" ? "Marked expected. Similar flags now learn from this payment. Spending is unchanged; check Learned as normal for inferred matches." : "Returned to review and protected from automatic normal matching."); }
    catch(e) { setActionError(String(e)); } finally { setBusyId(null); }
  }
  const [review, setReview] = useState<{ id: number; reason: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [account, setAccount] = useState("");
  const [category, setCategory] = useState("");
  const [kind, setKind] = useState<ReasonKind>("all");
  const [minAmount, setMinAmount] = useState("");
  const [sort, setSort] = useState<SortKey>("priority");
  const [page, setPage] = useState(0);
  const [priority, setPriority] = useState("all");
  const resultsRef = useRef<HTMLSpanElement>(null);
  function changePage(next: number) {
    const pagingFocus = document.activeElement;
    setPage(next);
    requestAnimationFrame(() => {
      // A user may start filtering before the next paint. Keep their new focus.
      // Disabling a paging button can return focus to body, which still needs the results.
      if (document.activeElement !== pagingFocus && document.activeElement !== document.body) return;
      resultsRef.current?.focus({ preventScroll: true });
      resultsRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
    });
  }
  useEffect(() => setPage(0), [status, q, account, category, kind, minAmount, sort, priority]);

  const all = useMemo(() => (anomalies.data ?? []).filter(a => status === "learned" ? a.outcome === "learned" : status === "resolved" ? !!a.outcome && a.outcome !== "learned" : !a.outcome), [anomalies.data, status]);
  const accounts = useMemo(() => [...new Set(all.map((a) => a.account))].sort(), [all]);
  const cats = useMemo(
    () => Array.from(new Set(all.map((a) => a.category).filter(Boolean))) as string[],
    [all]
  );
  const filtered = useMemo(() => {
    const min = parseFloat(minAmount);
    const items = all.filter((a) => {
      if (priority !== "all" && (a.priority || "medium") !== priority) return false;
      if (account && a.account !== account) return false;
      if (category && a.category !== category) return false;
      if (kind === "outlier" && !(a.kind === "large_amount" || a.related_kinds?.includes("large_amount") || (!a.kind && a.z != null))) return false;
      if (kind === "duplicate" && !(a.kind === "possible_duplicate" || a.related_kinds?.includes("possible_duplicate") || a.duplicate_transaction_ids?.length || (!a.kind && a.z == null))) return false;
      if (!Number.isNaN(min) && Math.abs(a.amount) < min) return false;
      if (q) {
        const hay = `${a.description} ${a.merchant ?? ""} ${a.reason}`.toLowerCase();
        if (!hay.includes(q.toLowerCase())) return false;
      }
      return true;
    });
    return [...items].sort((x, y) => {
      if (sort === "priority") { const rank = { high: 0, medium: 1, low: 2 }; const difference = rank[x.priority || "medium"] - rank[y.priority || "medium"]; if (difference) return difference; }
      if (sort === "amount") return Math.abs(y.amount) - Math.abs(x.amount);
      if (sort === "z") return (y.z ?? 0) - (x.z ?? 0);
      return (y.txn_date || "").localeCompare(x.txn_date || "");
    });
  }, [all, q, account, category, kind, minAmount, sort, priority]);

  const currentPage = Math.min(page, Math.max(0, Math.ceil(filtered.length / 12) - 1));
  const displayed = filtered.slice(currentPage * 12, (currentPage + 1) * 12);

  if (anomalies.loading && anomalies.data === null) return <Spinner />;
  if (anomalies.error && anomalies.data === null) return <ErrorBox error={anomalies.error} retry={anomalies.refetch} />;

  return (
    <div className={shortMobile ? "space-y-2" : "space-y-4"}>
      {anomalies.loading && <p role="status" className="text-xs text-mist">Updating review flags… Your filters and current review stay open.</p>}
      {anomalies.error && <ErrorBox error={anomalies.error} retry={anomalies.refetch} />}
      {actionError && <ErrorBox error={actionError} />}
      {notice && <p role="status" className="text-sm text-kelp">{notice}</p>}
      {review && <TransactionReview id={review.id} reason={review.reason} onClose={() => setReview(null)} onSaved={message => { anomalies.refetch(); setNotice(message || "Review saved. Resolved payments are in Resolved history."); }} />}
      <div>{!shortMobile && <Heading level={2} variant="intro">A closer look</Heading>}<p className={shortMobile ? "max-w-3xl text-xs leading-4 text-mist" : "mt-2 max-w-3xl text-sm leading-relaxed text-mist"}>{shortMobile ? "Compare earlier payments. Flags can be ordinary spending." : "Compare unusual charges with earlier payments. Flags can be ordinary spending."}</p></div>
      <div className="segmented-control" role="group" aria-label="Review status">
        <button className={status === "open" ? "segment active" : "segment"} aria-pressed={status === "open"} onClick={() => setStatus("open")}>Open reviews</button>
        <button className={status === "learned" ? "segment active" : "segment"} aria-pressed={status === "learned"} onClick={() => setStatus("learned")}>Learned as normal</button>
        <button className={status === "resolved" ? "segment active" : "segment"} aria-pressed={status === "resolved"} onClick={() => setStatus("resolved")}>Resolved history</button>
      </div>
      <Card className={shortMobile ? "space-y-2 p-2" : "space-y-3 p-3 sm:p-4"}>
        <input aria-label="Search anomaly reason or merchant" className="input w-full" placeholder="Search reason / merchant…"
          value={q} onChange={(e) => setQ(e.target.value)} />
        <MobileDisclosure label="Review filters & sort" summary={`${[account, category, kind !== "all", minAmount, priority !== "all"].filter(Boolean).length} active · ${priority === "all" ? "All priorities" : priority}`}><div className="grid grid-cols-2 gap-2">
          <select aria-label="Review priority" className="input min-w-0 w-full" value={priority} onChange={e => setPriority(e.target.value)}><option value="all">All priorities</option><option value="high">Higher priority</option><option value="medium">Medium priority</option><option value="low">Lower priority</option></select>
          <select aria-label="Sort anomalies" className="input min-w-0 w-full" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
            <option value="priority">Sort: priority</option><option value="date">Sort: date</option>
            <option value="amount">Sort: amount</option><option value="z">Sort: severity</option>
          </select>
        </div>
        <details className="border-t border-line pt-1">
          <summary className="min-h-11 cursor-pointer py-2 text-xs font-medium text-mist">More filters{(account || category || kind !== "all" || minAmount) && <span className="ml-2 text-teal">{[account, category, kind !== "all", minAmount].filter(Boolean).length} active</span>}</summary>
          <div className="grid gap-3 pb-2 pt-1 sm:grid-cols-2">
            <label className="field-label">Account<select aria-label="Account" className="input min-w-0 w-full" value={account} onChange={(e) => setAccount(e.target.value)}>
              <option value="">All accounts</option>{accounts.map((a) => <option key={a} value={a}>{a}</option>)}
            </select></label>
            <label className="field-label">Category<select aria-label="Category" className="input min-w-0 w-full" value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">All categories</option>{cats.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></label>
            <div><p className="field-label mb-1">Flag type</p><div className="flex overflow-hidden rounded-lg border border-line/70" role="group" aria-label="Flag type">
              {(["all", "outlier", "duplicate"] as ReasonKind[]).map((k) => <button key={k} aria-pressed={kind === k} onClick={() => setKind(k)} className={`min-h-11 flex-1 px-3 py-1.5 text-xs capitalize transition-colors ${kind === k ? "bg-amber/20 text-amber" : "bg-surface2/40 text-mist hover:text-foam"}`}>{k}</button>)}
            </div></div>
            <label className="field-label">Minimum amount<input aria-label="Minimum amount in pounds" min={0} className="input w-full" type="number" placeholder="£0" value={minAmount} onChange={(e) => setMinAmount(e.target.value)} /></label>
          </div>
        </details>
        </MobileDisclosure>
        <div className="flex min-h-5 items-center justify-between gap-3 text-xs text-mist">
          <span ref={resultsRef} tabIndex={-1} className="scroll-mt-24 tabular-nums">{filtered.length} of {all.length} {status === "open" ? "open flags" : status === "learned" ? "learned matches" : "resolved payments"}{filtered.length > 0 && <span className="sr-only"> · Showing {currentPage * 12 + 1}–{Math.min((currentPage + 1) * 12, filtered.length)}</span>}</span>
          {(q || account || category || kind !== "all" || minAmount || priority !== "all") && <button className="min-h-11 text-teal underline underline-offset-4" onClick={() => { setQ(""); setAccount(""); setCategory(""); setKind("all"); setMinAmount(""); setPriority("all"); }}>Clear filters</button>}
        </div>
      </Card>

      {filtered.length === 0 ? (
        <Card><div className="py-8 text-center text-sm text-mist">No {status === "open" ? "open reviews" : status === "learned" ? "learned matches" : "resolved payments"} match these filters.</div></Card>
      ) : (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {displayed.map((a) => (
            <Card key={`${a.id}-${a.reason}`} className={`flex min-w-0 flex-col ${shortMobile ? "gap-3" : "gap-4"}`}>
              <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
                <div className="min-w-0">
                  <p className="break-words font-medium text-foam">{a.merchant || a.description}</p>
                  <div className="mt-2">{a.outcome ? <Pill tone="mute">{a.outcome === "learned" ? "Learned as normal" : a.outcome === "expected" ? "Resolved · expected" : "Resolved · issue confirmed"}</Pill> : a.z != null ? <Pill tone="amber">larger than usual</Pill> : <Pill tone="coral">possible duplicate</Pill>}</div>
                </div>
                <p className="whitespace-nowrap text-right font-semibold tabular-nums text-coral">{money(a.amount)}</p>
              </div>
              <div className="min-w-0 flex-1 space-y-2 text-xs text-mist">
                {a.priority && <p className="font-medium text-foam">{a.priority === "high" ? "Higher" : a.priority === "low" ? "Lower" : "Medium"} priority{a.priority_reason ? ` · ${a.priority_reason}` : ""}</p>}
                <p>{a.reason}</p>
                <p>{shortDate(a.txn_date)} · {a.account} · {a.category}</p>
                {a.learning_reason && <p className="rounded-lg bg-surface2 p-3 leading-relaxed text-foam">{a.learning_reason}</p>}
                {a.learning_source_id && <button className="underline underline-offset-4" onClick={() => setReview({id: a.learning_source_id!, reason: ""})}>View your expected example</button>}
                {(a.evidence_note || a.comparison_transaction_ids?.length || a.duplicate_transaction_ids?.length) && <details><summary className="min-h-11 cursor-pointer font-medium text-foam">View the earlier evidence</summary><div className="mt-2 space-y-2 leading-relaxed">{a.evidence_note && <p>{a.evidence_note}</p>}{a.history_count != null && <p>{a.history_count} earlier observations{a.baseline_from && a.baseline_to ? ` · ${shortDate(a.baseline_from)} to ${shortDate(a.baseline_to)}` : ""}</p>}{a.baseline_amount != null && <p>Typical prior amount {money(a.baseline_amount)}{a.threshold_amount != null ? ` · check threshold ${money(a.threshold_amount)}` : ""}</p>}<div className="flex flex-wrap gap-2">{Array.from(new Set([...(a.comparison_transaction_ids || []), ...(a.duplicate_transaction_ids || [])])).map((id, index) => <button key={id} className="btn text-xs" onClick={() => setReview({ id, reason: "" })}>Earlier payment {index + 1}</button>)}</div></div></details>}
                {a.explanation && <details><summary className="cursor-pointer">Why was this flagged?</summary><p className="mt-2 leading-relaxed">{a.explanation}</p></details>}
              </div>
              <div className="grid grid-cols-1 gap-2 border-t border-line pt-3 min-[400px]:grid-cols-2">
                <button disabled={busyId !== null || anomalies.loading} className="btn btn-primary min-h-11 min-w-0 whitespace-normal text-sm" aria-label={`${status === "open" ? "Resolve" : "Reopen"} ${a.merchant || a.description}`} onClick={() => void resolve(a.id, status === "open" ? "expected" : "pending")}>{busyId === a.id ? "Saving…" : status === "open" ? "Looks OK — resolve" : status === "learned" ? "Return to review" : "Reopen"}</button>
                <button disabled={busyId !== null || anomalies.loading} className="btn min-h-11 min-w-0 whitespace-normal text-sm" aria-label={`Review ${a.merchant || a.description}`} onClick={() => setReview({ id: a.id, reason: a.reason })}>Edit details…</button>
              </div>
            </Card>
          ))}
        </div>
      )}
      <PageControls total={filtered.length} page={currentPage} size={12} onPage={changePage} label="Review queue pages" />
      <Card><details><summary className="cursor-pointer font-medium">How flags and learning work</summary><div className="mt-3 space-y-2 text-sm text-mist">
        <p>Amount flags compare a payment with earlier merchant history, with earlier category history as a fallback. At least five prior observations are required. Repeated-payment flags link an earlier equal amount within two days; adjacent-day or small everyday purchases have lower priority. These are prompts to check, not conclusions that a charge is wrong.</p>
        <p>“Looks OK” teaches a local nearest-example model to reduce false alarms. It groups the same merchant, account, category, payment context and flag type, within 10% of the expected amount (minimum £1) and 180 days. Nearby confirmed issues prevent automatic normal matching.</p>
        <p>Your explicit outcomes teach the model immediately; inferred matches never teach further matches. Learned as normal shows what left the queue and why. “Return to review” overrides the model for that payment. Spending stays unchanged. This prioritises fewer false alarms and may hide a similar real issue.</p>
        <p>Flags help prioritise review; they have not been independently measured for missed issues. Check the original payment before acting on a flag or a learned match.</p>
      </div></details></Card>
    </div>
  );
}
