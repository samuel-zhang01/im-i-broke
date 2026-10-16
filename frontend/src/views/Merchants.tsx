import { Heading } from "../components/Heading";
import { DataTable } from "../components/DataTable";
import { lazy, Suspense, useEffect, useId, useMemo, useRef, useState } from "react";
import { Disclosure } from "../components/Disclosure";
import {
  api,
  type MerchantRule,
  type MerchantInput,
  type MerchantAlias,
  type MerchantLookup,
  type MerchantDirectory,
} from "../api";
import {
  Async,
  Card,
  ErrorBox,
  Modal,
  DialogActions,
  Pill,
  SectionTitle,
  useAsync,
  PageControls,
  useCompactLayout,
} from "../components/ui";

import { useCategoryOptions, CategoryOptionsError } from "../components/CategoryOptions";
import { money, shortDate } from "../format";
import { readInboxPreference, writeInboxPreference } from "../inboxPreferences";
import MerchantInboxPanel from "../components/MerchantInboxPanel";
import SimilarNameSuggestions from "../components/SimilarNameSuggestions";
import MerchantMergeDialog from "../components/MerchantMergeDialog";
import MerchantMergeSuggestions from "../components/MerchantMergeSuggestions";
import MerchantSearchField from "../components/MerchantSearchField";
import { searchMerchantResult, type MerchantSearchMode } from "../merchantSearch";
import type { SimilarNameCandidate } from "../merchantInbox";

const MerchantProfile = lazy(() => import("../components/MerchantProfile"));

const suggestionKey = (s: {merchant:string;suggested_merchant_id:number}) => `${s.suggested_merchant_id}:${s.merchant.toLowerCase().trim()}`;
type Suggestion = MerchantDirectory["suggestions"][number];

const kinds = [
  { value: "merchant", label: "Business" },
  { value: "person", label: "Friend / person" },
  { value: "self", label: "Me" },
  { value: "financial", label: "Financial service" },
  { value: "unknown", label: "Unknown" },
] as const;
const blank: MerchantInput = {
  canonical_name: "",
  kind: "merchant",
  category: "Uncategorized",
  spending_treatment: "normal",
  source_url: null,
  notes: null,
  enabled: true,
  aliases: [],
};
export default function Merchants({ initialSearch }: { initialSearch?: string } = {}) {
  const [showInbox, setShowInbox] = useState<boolean>(() => initialSearch ? false : readInboxPreference("showInbox", true));
  useEffect(() => writeInboxPreference("showInbox", showInbox), [showInbox]);
  const directory = useAsync(signal => api.merchants(signal), [], { enabled: !showInbox });
  const [search, setSearch] = useState(initialSearch || "");
  const [searchMode, setSearchMode] = useState<MerchantSearchMode>("fuzzy");
  const [merging, setMerging] = useState<{ targetId: number; sourceIds: number[]; reason?: string } | null>(null);
  useEffect(() => { if (initialSearch !== undefined) { setSearch(initialSearch); setShowInbox(false); } }, [initialSearch]);
  const [profileId, setProfileId] = useState<number | null>(null);
  const [page, setPage] = useState(0);
  const recordsRef = useRef<HTMLDivElement>(null);
  const filtersRef = useRef<HTMLDivElement>(null);
  function changePage(next: number) {
    setPage(next);
    requestAnimationFrame(() => {
      recordsRef.current?.focus({ preventScroll: true });
      recordsRef.current?.scrollIntoView({ block: "start" });
    });
  }
  function clearFilters() {
    setSearch("");
    setKind("");
    setSearchMode("fuzzy");
    setPage(0);
    filtersRef.current?.querySelector<HTMLInputElement>('input[type="search"]')?.focus();
  }
  const compact = useCompactLayout();
  const [kind, setKind] = useState("");
  useEffect(() => setPage(0), [search, searchMode, kind]);
  const [editing, setEditing] = useState<MerchantRule | "new" | null>(null);
  const [rowState, setRowState] = useState<Record<string, {busy?:boolean; error?:string; resolved?:boolean}>>({});
  const [notices, setNotices] = useState<{key:string; text:string; dismissed?:{merchant:string; suggested_merchant_id:number}}[]>([]);
  const pending = useRef(new Set<string>());
  const [showDismissed, setShowDismissed] = useState(false);
  useEffect(() => {
    if (!directory.data) return;
    const stillPresent = new Set(directory.data.suggestions.map(suggestionKey));
    // Optimistic hiding lasts until the refreshed server list confirms removal.
    // A later rule edit can then legitimately reopen the same suggestion.
    setRowState(previous => Object.fromEntries(Object.entries(previous).filter(([key, value]) => !value.resolved || stillPresent.has(key))));
  }, [directory.data]);
  async function decide(s: Suggestion, action: "approve" | "dismiss") {
    const key = suggestionKey(s);
    if (pending.current.has(key)) return;
    pending.current.add(key);
    setRowState(previous => ({...previous, [key]:{busy:true}}));
    try {
      let message: string;
      if (action === "approve") {
        const result = await api.approveMerchantAlias(s.suggested_merchant_id, s.merchant, s.example_id);
        message = result.message;
      } else {
        await api.dismissMerchantSuggestion(s.suggested_merchant_id, s.merchant);
        message = `Dismissed ${s.merchant} → ${s.suggested_name}. Your transactions are unchanged.`;
      }
      setRowState(previous => ({...previous, [key]:{resolved:true}}));
      setNotices(previous => [{key, text:message, ...(action === "dismiss" ? {dismissed:s} : {})}, ...previous.filter(n => n.key !== key)].slice(0, 5));
      directory.refetch();
    } catch (error) {
      setRowState(previous => ({...previous, [key]:{error:String(error)}}));
    } finally {
      pending.current.delete(key);
    }
  }
  async function restore(s: {merchant:string; suggested_merchant_id:number}) {
    const key = suggestionKey(s);
    if (pending.current.has(key)) return;
    pending.current.add(key);
    setRowState(previous => ({...previous, [key]:{busy:true}}));
    try {
      await api.restoreMerchantSuggestion(s.suggested_merchant_id, s.merchant);
      setRowState(previous => ({...previous, [key]:{}}));
      setNotices(previous => previous.filter(n => n.key !== key));
      directory.refetch();
    } catch (error) {
      setRowState(previous => ({...previous, [key]:{error:String(error)}}));
    } finally { pending.current.delete(key); }
  }
  const searchResult = useMemo(() => searchMerchantResult((directory.data?.items ?? []).filter(rule => !kind || rule.kind === kind), search, searchMode), [directory.data?.items, kind, search, searchMode]);
  const visibleRules = searchResult.items;
  const currentPage = Math.min(page, Math.max(0, Math.ceil(visibleRules.length / 24) - 1));
  const displayedRules = visibleRules.slice(currentPage * 24, (currentPage + 1) * 24);
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-2xl">
          <Heading level={2} variant="intro" className="hidden sm:block">
            A little context goes a long way.
          </Heading>
          <p className="text-sm leading-relaxed text-mist sm:mt-2">
            Save names and categories for past and future payments.
          </p>
        </div>
        <button className="btn-primary" onClick={() => setEditing("new")}>
          + Add a name
        </button>
      </div>

      <Card>
        <div className="segmented-control" role="group" aria-label="Merchant workspace">
          <button className={showInbox ? "segment active" : "segment"} aria-label="Classification inbox" aria-pressed={showInbox} onClick={() => setShowInbox(true)}><span className="sm:hidden">Inbox</span><span className="hidden sm:inline">Classification inbox</span></button>
          <button className={!showInbox ? "segment active" : "segment"} aria-label="Saved merchants & aliases" aria-pressed={!showInbox} onClick={() => setShowInbox(false)}><span className="sm:hidden">Saved names</span><span className="hidden sm:inline">Saved merchants & aliases</span> ({directory.data?.items.length ?? "…"})</button>
        </div>
        <Disclosure title="How saved names work" variant="plain" className="mt-3"><div className="space-y-2 text-sm text-mist"><p>A saved name groups a person or business with a category. Aliases are the bank’s different labels for that name. Individual transaction reviews take priority.</p><p>To reuse a name, choose it when classifying a payment. Edit adds or changes aliases. Merge combines saved entries; preview the changes before confirming.</p></div></Disclosure>
      </Card>
      {showInbox && <MerchantInboxPanel onSaved={directory.refetch} />}
      {!showInbox && <div className="space-y-5" aria-label="Saved merchant directory">
      <Heading level={2} variant="section">Saved name directory</Heading>
      <Card ref={filtersRef} className="flex flex-wrap items-start gap-3">
        <MerchantSearchField className="min-w-0 flex-1 basis-80" label="Search people and merchants" placeholder="Search a name or alias…" value={search} mode={searchMode} onChange={setSearch} onModeChange={setSearchMode} error={searchResult.error} />
        <label className="field-label">Name type
        <select
          className="input"
          aria-label="Name type filter"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          <option value="">All types</option>
          {kinds.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
        </label>
      </Card>
      <Async state={directory}>
        {(d) => (
          <>
      {notices.length > 0 && <div aria-live="polite" className="space-y-2">{notices.map(n => <div key={n.key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-kelp/25 bg-kelp/5 px-4 py-3 text-sm"><p>{n.text}</p>{n.dismissed && <button className="btn text-xs" disabled={rowState[n.key]?.busy} onClick={() => void restore(n.dismissed!)}>Undo dismissal</button>}{rowState[n.key]?.error && <p role="alert" className="w-full text-xs text-coral">{rowState[n.key].error}</p>}</div>)}</div>}
            <MerchantMergeSuggestions rules={d.items} visibleIds={search || kind ? visibleRules.map(rule => rule.id) : undefined} onChoose={suggestion => setMerging({ targetId: suggestion.target_id, sourceIds: [suggestion.source_id], reason: suggestion.reason })} />
            <p className="text-xs text-mist">{visibleRules.length} of {d.items.length} saved names</p>
            {!visibleRules.length && <Card><p className="text-sm text-mist">No saved names match these filters.</p><button className="btn mt-3" onClick={clearFilters}>Clear directory filters</button></Card>}
            <div ref={recordsRef} role="region" aria-label="Saved merchant records" tabIndex={-1} className="data-table-region">
            {compact && <div className="space-y-3">{displayedRules.map(r => <Card key={r.id}><div className="flex items-start justify-between gap-3"><div className="min-w-0"><Heading level={3} variant="subsection" className="break-words"><button className="text-left text-teal underline decoration-teal/25 underline-offset-4" aria-label={`View ${r.canonical_name} activity`} onClick={() => setProfileId(r.id)}>{r.canonical_name}</button></Heading><p className="mt-1 text-xs text-mist">{r.category ?? "Use existing category"} · {r.n_transactions} payments</p></div><div className="flex shrink-0 flex-col gap-2"><button className="btn" aria-label={`Edit ${r.canonical_name}`} onClick={() => setEditing(r)}>Edit</button><button className="btn" aria-label={`Merge ${r.canonical_name}`} onClick={() => setMerging({ targetId: r.id, sourceIds: [] })}>Merge</button></div></div><p className="mt-3 text-xs text-mist">{kinds.find(k => k.value === r.kind)?.label} · {r.spending_treatment === "self_transfer" ? "Own transfer" : r.spending_treatment === "reimbursement" ? "Shared-cost refund" : "Normal treatment"}</p><details className="mt-3 text-xs"><summary className="cursor-pointer font-medium">{r.aliases.length} statement aliases</summary><ul className="mt-2 space-y-2 text-mist">{r.aliases.map(a => <li className="break-words" key={`${a.context}:${a.pattern}`}>{a.pattern}</li>)}</ul></details></Card>)}</div>}
            {!compact && <Card className="overflow-hidden p-0">
              <div className="overflow-x-auto">
                <DataTable className="w-full text-left text-sm">
                  <thead className="bg-surface2 text-xs text-mist">
                    <tr>
                      {[
                        "Name & aliases",
                        "Type",
                        "Category",
                        "Spending treatment",
                        "Matches",
                        "",
                      ].map((h, i) => (
                        <th className="px-4 py-3 font-medium" key={i}>
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {displayedRules
                      .map((r) => (
                        <tr key={r.id} className="border-t border-line/70">
                          <td className="min-w-[210px] px-4 py-4">
                            <button className="text-left font-semibold text-teal underline decoration-teal/25 underline-offset-4" aria-label={`View ${r.canonical_name} activity`} onClick={() => setProfileId(r.id)}>{r.canonical_name}</button>
                            <p className="mt-1 max-w-sm text-xs text-mist">
                              {r.aliases.map((a) => a.pattern).join(" · ")}
                            </p>
                            {r.source_url && (
                              <a
                                className="mt-1 inline-block text-xs text-teal underline"
                                href={r.source_url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                Source ↗
                              </a>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            {kinds.find((k) => k.value === r.kind)?.label}
                          </td>
                          <td className="px-4 py-3">
                            {r.category ?? "Use existing"}
                          </td>
                          <td className="px-4 py-3">
                            <Pill
                              tone={
                                r.spending_treatment === "self_transfer"
                                  ? "blue"
                                  : "mute"
                              }
                            >
                              {r.spending_treatment === "self_transfer"
                                ? "Own transfer"
                                : r.spending_treatment === "reimbursement"
                                  ? "Shared-cost refund"
                                  : [
                                        "Transfers & payments",
                                        "Investments",
                                      ].includes(r.category ?? "")
                                    ? "Excluded by category"
                                    : "Included"}
                            </Pill>
                          </td>
                          <td className="px-4 py-3 tabular-nums">
                            {r.n_transactions}
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex gap-2"><button className="btn" aria-label={`Edit ${r.canonical_name}`} onClick={() => setEditing(r)}>Edit</button><button className="btn" aria-label={`Merge ${r.canonical_name}`} onClick={() => setMerging({ targetId: r.id, sourceIds: [] })}>Merge</button></div>
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </DataTable>
              </div>
            </Card>}
            </div>
            <PageControls total={visibleRules.length} page={currentPage} size={24} onPage={changePage} label="Saved merchant pages" />
            <Card>
              <SectionTitle hint="Changes save immediately; no page reload needed">Similar names to review</SectionTitle>
              <p className="mb-4 text-xs leading-relaxed text-mist">A spelling match needs context. Check the original descriptions below, then approve the exact label or dismiss a wrong match. Saved transaction reviews take priority.</p>
              {d.suggestions.filter(s => !rowState[suggestionKey(s)]?.resolved).length === 0 ? <p className="text-sm text-mist">No close matches need review.</p> : <div className="divide-y divide-line">
                {d.suggestions.filter(s => !rowState[suggestionKey(s)]?.resolved).slice(0,20).map(s => {
                  const key = suggestionKey(s), status = rowState[key];
                  return <div key={key} className="space-y-3 py-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0"><p className="break-words text-sm font-medium">{s.merchant} <span className="text-mist">→</span> {s.suggested_name}</p><p className="mt-1 text-xs text-mist">{s.n_transactions} matching transaction{s.n_transactions === 1 ? "" : "s"} · category {s.category || "unchanged"}</p></div>
                      <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row"><button className="btn" disabled={status?.busy} onClick={() => void decide(s,"dismiss")} aria-label={`Dismiss ${s.merchant} to ${s.suggested_name}`}>Not this business</button><button className="btn-primary" disabled={status?.busy} onClick={() => void decide(s,"approve")} aria-label={`Approve ${s.merchant} as ${s.suggested_name}`}>{status?.busy ? "Saving…" : "Approve alias"}</button></div>
                    </div>
                    {s.match_warning && <p className="rounded-md border border-amber/25 bg-amber/5 px-3 py-2 text-xs leading-relaxed text-mist">{s.match_warning}</p>}
                    {s.examples && s.examples.length > 0 && <div className="space-y-1 rounded-md bg-surface2 px-3 py-2">{s.examples.map(example => <div key={example.id} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-xs"><span className="min-w-0 break-words">{example.description}</span><span className="text-mist">{shortDate(example.txn_date)} · {example.account} · {money(example.amount)}</span></div>)}</div>}
                    {status?.error && <div role="alert" className="rounded-md border border-coral/30 bg-coral/5 p-3 text-xs text-coral"><p>{status.error}</p><button className="mt-2 underline" onClick={() => directory.refetch()}>Refresh available matches</button></div>}
                  </div>;
                })}
              </div>}
              {(d.dismissed_suggestions?.length ?? 0) > 0 && <div className="mt-4 border-t border-line pt-3"><button className="text-xs font-medium underline underline-offset-4" aria-expanded={showDismissed} onClick={() => setShowDismissed(value => !value)}>Dismissed suggestions ({d.dismissed_suggestions!.length})</button>{showDismissed && <div className="mt-3 space-y-2">{d.dismissed_suggestions!.map(s => <div key={suggestionKey(s)} className="flex flex-wrap items-center justify-between gap-2 text-xs"><span>{s.merchant} → {s.suggested_name}</span><button className="btn text-xs" disabled={rowState[suggestionKey(s)]?.busy} onClick={() => void restore(s)}>Restore suggestion</button>{rowState[suggestionKey(s)]?.error && <p role="alert" className="w-full text-coral">{rowState[suggestionKey(s)].error}</p>}</div>)}</div>}</div>}
            </Card>
            <p className="text-xs leading-relaxed text-mist">
              {d.privacy_note}
            </p>
          </>
        )}
      </Async>
      </div>}
      {profileId !== null && <Suspense fallback={<p role="status" className="text-sm text-mist">Opening saved name…</p>}><MerchantProfile id={profileId} onClose={() => setProfileId(null)} onEdit={() => { setEditing(directory.data?.items.find(item => item.id === profileId) || null); setProfileId(null); }} /></Suspense>}
      {merging && directory.data && <MerchantMergeDialog rules={directory.data.items} initialTargetId={merging.targetId} initialSourceIds={merging.sourceIds} reason={merging.reason} onClose={() => setMerging(null)} onRefresh={directory.refetch} onSaved={result => {
        setMerging(null);
        setNotices(previous => [{ key: `merge:${result.target_id}:${Date.now()}`, text: result.message }, ...previous].slice(0, 5));
        directory.refetch();
      }} />}
      {editing && (
        <MerchantEditor
          rule={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            directory.refetch();
          }}
        />
      )}
    </div>
  );
}

function MerchantEditor({
  rule,
  onClose,
  onSaved,
}: {
  rule: MerchantRule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const formId = useId();
  const [form, setForm] = useState<MerchantInput>(
    rule
      ? { ...rule, aliases: rule.aliases.map((a) => ({ ...a })) }
      : {
          ...blank,
          aliases: [{ pattern: "", match_mode: "exact", context: "any" }],
        },
  );
  const categories = useCategoryOptions(form.category);
  const saving = useRef(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [lookup, setLookup] = useState<MerchantLookup | null>(null),
    [looking, setLooking] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [customCategory, setCustomCategory] = useState(false);
  const [selectedSimilar, setSelectedSimilar] = useState<SimilarNameCandidate[]>([]);
  const aliasKey = (alias: Pick<MerchantAlias, "pattern" | "context">) => `${alias.context}:${alias.pattern.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()}`;
  const selectedKeys = new Set(selectedSimilar.map(item => aliasKey({ pattern: item.label, context: item.context })));
  const withoutSuggested = (aliases: MerchantAlias[]) => aliases.filter(alias => !selectedKeys.has(aliasKey(alias)));
  function toggleSimilar(item: SimilarNameCandidate, checked: boolean) {
    const nextAlias: MerchantAlias = { pattern: item.label, match_mode: "exact", context: item.context };
    setSelectedSimilar(current => checked ? [...current.filter(row => row.example_id !== item.example_id), item] : current.filter(row => row.example_id !== item.example_id));
    setForm(current => ({ ...current, aliases: checked
      ? [...current.aliases.filter(alias => alias.pattern.trim() && aliasKey(alias) !== aliasKey(nextAlias)), nextAlias]
      : current.aliases.filter(alias => aliasKey(alias) !== aliasKey(nextAlias)) }));
  }
  function field<K extends keyof MerchantInput>(
    key: K,
    value: MerchantInput[K],
  ) {
    const identityChanged = key === "canonical_name" || key === "kind" || key === "spending_treatment";
    setForm((f) => ({ ...f, ...(identityChanged ? { aliases: withoutSuggested(f.aliases) } : {}), [key]: value }));
    if (identityChanged) setSelectedSimilar([]);
  }
  function alias(index: number, key: keyof MerchantAlias, value: string) {
    setForm((f) => ({
      ...f,
      aliases: f.aliases.map((a, i) =>
        i === index ? { ...a, [key]: value } : a,
      ),
    }));
  }
  async function save() {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const data = {
        ...form,
        expected_revision: rule?.revision,
        canonical_name: form.canonical_name.trim(),
        aliases: form.aliases
          .filter((a) => a.pattern.trim())
          .map((a) => ({ ...a, pattern: a.pattern.trim() })),
      };
      if (!data.canonical_name || !data.aliases.length)
        throw new Error("Add a name and at least one matching alias.");
      if (rule) await api.updateMerchant(rule.id, data);
      else await api.createMerchant(data);
      onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  async function lookupName() {
    if (!rule) return;
    setLooking(true);
    setError("");
    try {
      setLookup(await api.lookupMerchant(rule.canonical_name));
    } catch (e) {
      setError(`Business lookup could not finish. ${String(e)}`);
    } finally {
      setLooking(false);
    }
  }
  async function remove() {
    if (!rule || saving.current) return;
    saving.current = true;
    setBusy(true);
    try {
      await api.disableMerchant(rule.id);
      onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <Modal
      title={rule ? `Edit ${rule.canonical_name}` : "Add a person or business"}
      onClose={() => !saving.current && onClose()}
      footer={<DialogActions formId={formId} onCancel={onClose} busy={busy} disabled={looking} label={busy ? "Applying…" : "Save rule"}>
        {rule && <button type="button" className="btn btn-quiet text-coral" disabled={busy} onClick={() => confirmRemove ? remove() : setConfirmRemove(true)}>{confirmRemove ? "Confirm remove rule" : "Remove rule"}</button>}
      </DialogActions>}
    >
      <form
        id={formId}
        aria-busy={busy}
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <fieldset disabled={busy} className="space-y-4">
        <CategoryOptionsError state={categories} />
        {error && <ErrorBox error={error} />}
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="field-label">
            Display name
            <input
              required
              className="input mt-1 w-full"
              value={form.canonical_name}
              onChange={(e) => field("canonical_name", e.target.value)}
            />
          </label>
          <label className="field-label">
            Who is this?
            <select
              className="input mt-1 w-full"
              value={form.kind}
              onChange={(e) => {
                const next = e.target.value as MerchantInput["kind"];
                setSelectedSimilar([]);
                setForm((f) => ({
                  ...f,
                  kind: next,
                  category:
                    next === "person"
                      ? "Friends & shared costs"
                      : next === "self"
                        ? "Transfers & payments"
                        : f.category === "Transfers & payments"
                          ? "Uncategorized"
                          : f.category,
                  spending_treatment:
                    next === "self" ? "self_transfer" : "normal",
                  aliases: withoutSuggested(f.aliases).map((a) => ({
                    ...a,
                    context:
                      next === "self" || next === "person" ? "transfer" : "any",
                  })),
                }));
              }}
            >
              {kinds.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field-label">
            Category
            <select
              className="input mt-1 w-full"
              value={customCategory ? "__custom" : form.category ?? ""}
              onChange={(e) => { setCustomCategory(e.target.value === "__custom"); if (e.target.value !== "__custom") field("category", e.target.value || null); }}
            >
              <option value="">Keep existing category</option>
              {categories.options.map((c) => (
                <option key={c}>{c}</option>
              ))}
              <option value="__custom">Custom category…</option>
            </select>
            {customCategory && <input aria-label="Custom merchant category" className="input mt-2 w-full" maxLength={120} required value={form.category ?? ""} onChange={e => field("category", e.target.value)} />}
          </label>
          <label className="field-label">
            How it affects spending
            <select
              className="input mt-1 w-full"
              value={form.spending_treatment}
              onChange={(e) => {
                const treatment = e.target
                  .value as MerchantInput["spending_treatment"];
                setForm((f) => ({
                  ...f,
                  spending_treatment: treatment,
                  aliases: withoutSuggested(f.aliases),
                  category:
                    treatment === "normal" &&
                    f.category === "Transfers & payments"
                      ? "Uncategorized"
                      : f.category,
                }));
                setSelectedSimilar([]);
              }}
            >
              <option disabled={form.kind === "self"} value="normal">
                Include outgoing payments
              </option>
              <option value="self_transfer">
                Own transfer · exclude in and out
              </option>
              <option disabled={form.kind === "self"} value="reimbursement">
                Shared costs · receipts reduce spending
              </option>
            </select>
          </label>
        </div>
        <SimilarNameSuggestions
          query={{ canonical_name: form.canonical_name, kind: form.kind, spending_treatment: form.spending_treatment, merchant_id: rule?.id, patterns: withoutSuggested(form.aliases).map(alias => alias.pattern).filter(Boolean).slice(0, 30) }}
          selected={selectedSimilar.filter(item => form.aliases.some(alias => aliasKey(alias) === aliasKey({ pattern: item.label, context: item.context })))}
          onToggle={toggleSimilar} disabled={busy}
        />
        <div>
          <SectionTitle hint="Exact matching is the safest starting point">
            Matching aliases
          </SectionTitle>
          <div className="space-y-2">
            {form.aliases.map((a, i) => (
              <div key={i} className="flex flex-wrap gap-2">
                <input
                  className="input w-full sm:w-auto sm:flex-1"
                  aria-label={`Alias ${i + 1}`}
                  value={a.pattern}
                  onChange={(e) => alias(i, "pattern", e.target.value)}
                  placeholder="Name as it appears on a statement"
                />
                <select
                  className="input"
                  aria-label={`Match mode ${i + 1}`}
                  value={a.match_mode}
                  onChange={(e) => alias(i, "match_mode", e.target.value)}
                >
                  <option value="exact">Exact label</option>
                  <option value="prefix">Starts with</option>
                  <option value="contains">Contains words</option>
                </select>
                <select
                  className="input"
                  aria-label={`Context ${i + 1}`}
                  value={a.context}
                  onChange={(e) => alias(i, "context", e.target.value)}
                >
                  <option value="any">Any payment</option>
                  <option value="transfer">Transfers only</option>
                  <option value="card">Card purchases</option>
                  <option value="funding">Account funding</option>
                  <option value="spending">Non-investment payments</option>
                  <option value="spending_card">Non-investment card purchases</option>
                  <option value="spending_transfer">Non-investment transfers</option>
                  <option value="spending_bank">Full bank descriptions · no card purchases</option>
                </select>
                <button
                  type="button"
                  className="btn"
                  aria-label={`Remove alias ${i + 1}`}
                  onClick={() =>
                    field(
                      "aliases",
                      form.aliases.filter((_, n) => n !== i),
                    )
                  }
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          <button
            type="button"
            className="mt-2 text-xs font-semibold text-teal"
            onClick={() =>
              field("aliases", [
                ...form.aliases,
                {
                  pattern: "",
                  match_mode: "exact",
                  context:
                    form.kind === "self" || form.kind === "person"
                      ? "transfer"
                      : "any",
                },
              ])
            }
          >
            + Another spelling
          </button>
        </div>
        <label className="field-label block">
          Notes
          <textarea
            className="input mt-1 min-h-16 w-full"
            value={form.notes ?? ""}
            onChange={(e) => field("notes", e.target.value || null)}
            placeholder="Why this rule is useful…"
          />
        </label>
        <label className="field-label block">
          Source link (optional)
          <input
            className="input mt-1 w-full"
            type="url"
            value={form.source_url ?? ""}
            onChange={(e) => field("source_url", e.target.value || null)}
          />
        </label>
        {rule?.kind === "merchant" && (
          <div className="rounded-lg border border-line bg-deep p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-mist">
                Free business lookup · sends the saved business name to
                Wikidata.
              </p>
              <button
                type="button"
                className="btn"
                disabled={looking || busy}
                onClick={lookupName}
              >
                {looking ? "Looking up…" : "Look up business"}
              </button>
            </div>
            {lookup && (
              <div className="mt-3 space-y-3">
                <p className="text-xs text-mist">
                  {lookup.message}
                  {lookup.stale
                    ? " Showing older cached suggestions."
                    : lookup.cached
                      ? " Cached result."
                      : ""}
                </p>
                {lookup.search_url?.startsWith("https://www.wikidata.org/") && (
                  <a
                    className="inline-block text-xs font-semibold text-teal underline"
                    href={lookup.search_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open public business search ↗
                  </a>
                )}
                {lookup.matches.map((m) => (
                  <div
                    key={m.id}
                    className="rounded-md border border-line bg-white p-3"
                  >
                    <a
                      className="text-sm font-semibold text-teal underline"
                      href={m.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {m.label} ↗
                    </a>
                    <p className="mt-1 text-xs text-mist">{m.description}</p>
                    {m.suggested_category && (
                      <button
                        type="button"
                        className="btn mt-2"
                        onClick={() =>
                          setForm((f) => ({
                            ...f,
                            category: m.suggested_category!,
                            source_url: m.source_url,
                          }))
                        }
                      >
                        Use {m.suggested_category}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {!rule && (
          <p className="text-xs text-mist">
            Save a business first to look it up online. Personal names stay
            local.
          </p>
        )}
        <p className="text-xs leading-relaxed text-mist">
          This rule updates matching transactions and future imports. Individual
          transaction corrections take priority. Removing a rule restores
          classification from the original statement.
        </p>
        </fieldset>
      </form>
    </Modal>
  );
}
