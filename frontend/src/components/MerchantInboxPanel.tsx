import { Heading } from "./Heading";
import MobileDisclosure from "./MobileDisclosure";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { money, shortDate } from "../format";
import { Async, Card, Pill, useAsync } from "./ui";
import { readInboxPreference, writeInboxPreference } from "../inboxPreferences";
import MerchantQuickClassify from "./MerchantQuickClassify";

const SORTS: Record<string, string> = {
  outgoing: "Largest outgoing", smallest: "Smallest outgoing", frequent: "Most transactions",
  recent: "Most recent", oldest: "Oldest activity", name: "Name A–Z",
};

function useInboxPreference<T extends string | boolean | number>(key: string, fallback: T) {
  const [value, setValue] = useState<T>(() => readInboxPreference(key, fallback));
  useEffect(() => writeInboxPreference(key, value), [key, value]);
  return [value, setValue] as const;
}

export default function MerchantInboxPanel({ onSaved }: { onSaved: () => void }) {
  const inbox = useAsync(signal => api.merchantInbox(signal));
  const [filter, setFilter] = useInboxPreference<string>("filter", "uncategorized"), [search, setSearch] = useInboxPreference<string>("search", "");
  const [limit, setLimit] = useInboxPreference<number>("limit", 8), [editing, setEditing] = useState<number | null>(null);
  const [notice, setNotice] = useState("");
  const [category, setCategory] = useInboxPreference<string>("category", ""), [context, setContext] = useInboxPreference<string>("context", "");
  const [sort, setSort] = useInboxPreference<string>("sort", "outgoing");
  const [repeated, setRepeated] = useInboxPreference<boolean>("repeated", false), [suggested, setSuggested] = useInboxPreference<boolean>("suggested", false);
  const categories = useMemo(() => [...new Set((inbox.data?.items || []).flatMap(item => item.categories))].sort(), [inbox.data]);
  function reset(all = false) {
    setSearch(""); setFilter(all ? "all" : "uncategorized"); setCategory(""); setContext("");
    setRepeated(false); setSuggested(false); setSort("outgoing"); setLimit(8);
  }
  const changed = search || filter !== "uncategorized" || category || context || repeated || suggested || sort !== "outgoing";
  const rows = useMemo(() => (inbox.data?.items || []).filter(item =>
    (filter === "all" || (filter === "uncategorized" ? item.uncategorized_count > 0 : item.needs_identity_count > 0)) &&
    (!category || item.categories.includes(category)) && (!context || item.context === context) &&
    (!repeated || item.n_transactions > 1) && (!suggested || !!item.learned_suggestion) &&
    (!search.trim() || `${item.label} ${item.categories.join(" ")} ${item.examples.map(example => example.description).join(" ")}`.toLowerCase().includes(search.trim().toLowerCase()))
  ).sort((a, b) => {
    const difference = sort === "outgoing" ? b.outgoing_total - a.outgoing_total
      : sort === "smallest" ? a.outgoing_total - b.outgoing_total
      : sort === "frequent" ? b.n_transactions - a.n_transactions
      : sort === "recent" ? b.last_date.localeCompare(a.last_date)
      : sort === "oldest" ? a.last_date.localeCompare(b.last_date) : 0;
    return difference || a.label.localeCompare(b.label) || a.key.localeCompare(b.key);
  }), [inbox.data, filter, search, category, context, repeated, suggested, sort]);
  return <Card>
    <div className="flex flex-wrap items-baseline justify-between gap-2"><Heading level={2} variant="section">Classification inbox</Heading><Pill tone="mute">Local learning · you approve changes</Pill></div>
    <p className="mt-2 text-sm leading-relaxed text-mist">Review unclear names and choose a category for matching payments.</p>
    {notice && <p role="status" className="mt-3 rounded-lg border border-kelp/30 bg-kelp/5 p-3 text-sm">{notice}</p>}
    <div className="my-4 flex flex-wrap gap-2">
      <input aria-label="Search classification inbox" className="input min-w-0 basis-full sm:flex-1 sm:basis-60" placeholder="Search original name, description or category…" value={search} onChange={e => { setSearch(e.target.value); setLimit(8); }} />
      <MobileDisclosure className="w-full sm:contents" label="Inbox filters & sort" summary={`${filter === "uncategorized" ? "Uncategorized" : filter === "identity" ? "Names to define" : "All labels"}${category ? ` · ${category}` : ""}${context ? ` · ${context}` : ""}${repeated ? " · repeated" : ""}${suggested ? " · suggested" : ""}`} contentClassName="flex flex-wrap gap-2 sm:contents">      <select aria-label="Classification inbox filter" className="input min-w-0 max-w-full basis-full sm:basis-auto sm:flex-none" value={filter} onChange={e => { setFilter(e.target.value); setLimit(8); }}><option value="uncategorized">Uncategorized only</option><option value="identity">Names to define</option><option value="all">All payment labels</option></select>
      <select aria-label="Inbox category" className="input min-w-0 max-w-full basis-full sm:flex-1 sm:basis-44" value={category} onChange={e => { setCategory(e.target.value); setLimit(8); }}><option value="">All categories</option>{categories.map(value => <option key={value}>{value}</option>)}</select>
      <select aria-label="Inbox payment type" className="input min-w-0 max-w-full basis-full sm:flex-1 sm:basis-44" value={context} onChange={e => { setContext(e.target.value); setLimit(8); }}><option value="">All payment types</option><option value="card">Card purchases</option><option value="transfer">Bank transfers</option><option value="spending">Other payments</option></select>
      <label className="flex min-w-0 basis-full items-center gap-2 text-xs text-mist sm:basis-auto">Sort by<select aria-label="Sort classification inbox" className="input min-w-0 flex-1" value={sort} onChange={e => { setSort(e.target.value); setLimit(8); }}>{Object.entries(SORTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <div className="flex basis-full flex-wrap items-center gap-2" role="group" aria-label="Quick inbox filters">
      <span className="text-xs text-mist">Quick filters</span>
      <button className={repeated ? "btn-primary" : "btn"} aria-pressed={repeated} onClick={() => { setRepeated(value => !value); setLimit(8); }}>Repeated labels</button>
      <button className={suggested ? "btn-primary" : "btn"} aria-pressed={suggested} onClick={() => { setSuggested(value => !value); setLimit(8); }}>Category suggested</button>
      {changed && <button className="btn" onClick={() => reset()}>Clear inbox filters</button>}
    </div>
    </MobileDisclosure></div>
    <Async state={inbox}>{data => <>
      <p className="mb-3 text-xs text-mist">Showing {Math.min(limit, rows.length)} of {rows.length} labels in this filtered view · sorted by {SORTS[sort].toLowerCase()}</p>
      <p className="mb-3 text-xs text-mist">Across all payments: {data.uncategorized_labels} uncategorized labels. Each spelling/payment context is a label; several may belong to one merchant.</p>
      {rows.length === 0 ? <div className="rounded-lg bg-surface2 p-5 text-sm"><p>No payment labels match these filters.</p><button className="btn mt-3" onClick={() => reset(true)}>Browse all payment labels</button></div> : <div className="divide-y divide-line">{rows.slice(0, limit).map(item => <div key={item.key} className="py-4">
        <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0 flex-1 basis-64 [overflow-wrap:anywhere]"><Heading level={3} variant="subsection" className="break-words">{item.label}</Heading><p className="mt-1 text-xs text-mist">{item.n_transactions} transactions · {money(item.outgoing_total)} recorded outgoing · latest {shortDate(item.last_date)}</p><p className="mt-1 text-xs text-mist">{item.categories.join(" · ")}</p></div><button className="btn w-full sm:w-auto sm:shrink-0" onClick={() => setEditing(item.example_id)} aria-label={`Classify ${item.label}`}>Choose name & category</button></div>
        {item.learned_suggestion && <p className="mt-2 text-xs text-mist">Your reviews suggest <strong>{item.learned_suggestion.category}</strong> ({item.learned_suggestion.support}/{item.learned_suggestion.reviewed} matching reviewed payments{item.learned_suggestion.conflicting ? "; mixed categories" : ""}). Check before applying.</p>}
        <details className="mt-3 text-xs [overflow-wrap:anywhere]"><summary className="cursor-pointer text-mist underline underline-offset-4">Statement examples</summary><div className="mt-2 space-y-2 rounded-lg bg-surface2 p-3">{item.examples.map(example => <div key={example.id}><p className="break-words">{example.description}</p><p className="mt-1 text-mist">{shortDate(example.txn_date)} · {example.account} · {money(example.amount)}</p></div>)}</div></details>
      </div>)}</div>}
      {rows.length > limit && <button className="btn mt-3" onClick={() => setLimit(previous => previous + 25)}>Show {Math.min(25, rows.length - limit)} more labels ({rows.length - limit} not yet shown)</button>}
      <p className="mt-4 border-t border-line pt-3 text-xs leading-relaxed text-mist">{data.method}</p>
    </>}</Async>
    {editing !== null && <MerchantQuickClassify id={editing} onClose={() => setEditing(null)} onSaved={message => { setNotice(message); inbox.refetch(); onSaved(); }} />}
  </Card>;
}
