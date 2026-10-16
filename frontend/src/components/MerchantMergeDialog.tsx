import { Disclosure } from "./Disclosure";
import { Heading } from "./Heading";
import { useEffect, useMemo, useRef, useState } from "react";
import type { MerchantAlias, MerchantRule } from "../api";
import { previewMerchantMerge, saveMerchantMerge, type MerchantMergePreview, type MerchantMergeResult } from "../merchantMerge";
import { searchMerchantResult, type MerchantSearchMode } from "../merchantSearch";
import { ErrorBox, Modal, Pill } from "./ui";
import MerchantSearchField from "./MerchantSearchField";

const contexts: Record<MerchantAlias["context"], string> = {
  any: "All payments", transfer: "Transfers", card: "Card payments", funding: "Account funding",
  spending: "Spending", spending_card: "Card spending", spending_transfer: "Transfer spending", spending_bank: "Bank spending",
};
const modes: Record<MerchantAlias["match_mode"], string> = { exact: "Exact label", prefix: "Starts with", contains: "Contains" };
const treatments: Record<MerchantRule["spending_treatment"], string> = {
  normal: "Normal spending", self_transfer: "Own transfer", reimbursement: "Shared-cost refund",
};
const kinds: Record<MerchantRule["kind"], string> = {
  merchant: "Business", person: "Friend / person", self: "Own account", financial: "Financial service", unknown: "Unknown",
};

export default function MerchantMergeDialog({ rules, initialTargetId, initialSourceIds = [], reason, onClose, onSaved, onRefresh }: {
  rules: MerchantRule[];
  initialTargetId: number;
  initialSourceIds?: number[];
  reason?: string;
  onClose: () => void;
  onSaved: (result: MerchantMergeResult) => void;
  onRefresh: () => void;
}) {
  const [selectedIds, setSelectedIds] = useState(() => [...new Set([initialTargetId, ...initialSourceIds])]);
  const [targetId, setTargetId] = useState(initialTargetId);
  const [query, setQuery] = useState("");
  const [addNamesOpen, setAddNamesOpen] = useState(() => initialSourceIds.length === 0);
  const [searchMode, setSearchMode] = useState<MerchantSearchMode>("fuzzy");
  const [preview, setPreview] = useState<MerchantMergePreview | null>(null);
  const [previewKey, setPreviewKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [refresh, setRefresh] = useState(0);
  const selected = useMemo(() => selectedIds.map(id => rules.find(rule => rule.id === id)).filter((rule): rule is MerchantRule => !!rule), [rules, selectedIds]);
  const target = selected.find(rule => rule.id === targetId);
  const sourceIds = selectedIds.filter(id => id !== targetId).sort((a, b) => a - b);
  const inputKey = `${targetId}:${sourceIds.join(",")}`;
  const ruleRevisions = selected.map(rule => `${rule.id}:${rule.revision ?? ""}:${rule.n_transactions}`).join("|");
  const missing = selected.length !== selectedIds.length;
  const available = rules.filter(rule => !selectedIds.includes(rule.id) && (!target || (rule.kind === target.kind && rule.spending_treatment === target.spending_treatment)));
  const matches = searchMerchantResult(available, query, searchMode);
  // Start with the closest names without making a choice on the user's behalf.
  const candidates = query.trim() ? matches.items : searchMerchantResult(available, target?.canonical_name ?? "", "fuzzy").items;
  const visibleCandidates = candidates.slice(0, 12);
  const ready = !!preview && previewKey === inputKey && !loading && !missing;

  useEffect(() => {
    const controller = new AbortController();
    setConfirmed(false);
    setPreview(null);
    setPreviewKey("");
    setError("");
    if (!sourceIds.length || missing) { setLoading(false); return () => controller.abort(); }
    setLoading(true);
    void previewMerchantMerge({ target_id: targetId, source_ids: sourceIds }, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        setPreview(value);
        setPreviewKey(inputKey);
      })
      .catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // The stable key includes every selected ID and the destination.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputKey, ruleRevisions, refresh, missing]);

  function changeSelection(ids: number[], destination = targetId) {
    setConfirmed(false);
    setPreview(null);
    setSelectedIds(ids);
    setTargetId(destination);
  }

  function refreshPreview() {
    setConfirmed(false);
    setPreview(null);
    setRefresh(value => value + 1);
  }

  async function save() {
    if (!ready || !confirmed || !preview || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await saveMerchantMerge({ target_id: targetId, source_ids: sourceIds, preview_token: preview.preview_token });
      onSaved(result);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setConfirmed(false);
      setPreview(null);
      setPreviewKey("");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return <Modal title="Merge saved names" onClose={() => { if (!saving.current) onClose(); }}>
    <div className="space-y-5">
      <p className="text-sm text-mist">Choose the names that belong together and the name to keep. Their saved matching rules will be combined for past and future payments.</p>
      {reason && <p className="rounded-lg bg-surface2 p-3 text-sm"><strong>Suggested match:</strong> {reason}</p>}

      <fieldset disabled={busy} className="space-y-3">
        <legend className="mb-3 text-sm font-semibold">1. Choose the name to keep</legend>
        <p className="text-xs text-mist">Its category and spending treatment will apply to the combined names.</p>
        {selected.map(rule => <div key={rule.id} className={`rounded-lg border p-3 ${targetId === rule.id ? "border-teal/50 bg-teal/5" : "border-line"}`}>
          <div className="flex items-start justify-between gap-3">
            <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3">
              <input className="mt-1 h-4 w-4 shrink-0 accent-teal" type="radio" name="merge-target" checked={targetId === rule.id} onChange={() => changeSelection(selectedIds, rule.id)} aria-label={`Keep ${rule.canonical_name}`} />
              <span className="min-w-0"><span className="block break-words text-sm font-semibold">{rule.canonical_name}</span><span className="mt-1 block text-xs text-mist">{kinds[rule.kind]} · {rule.category ?? "Existing payment category"} · {treatments[rule.spending_treatment]}</span><span className="mt-1 block text-xs text-mist">{rule.aliases.length} aliases · {rule.n_transactions} payments{!rule.enabled ? " · Disabled rule" : ""}</span></span>
            </label>
            {targetId === rule.id ? <Pill>Keep</Pill> : <button className="btn shrink-0 text-xs" aria-label={`Remove ${rule.canonical_name} from merge`} onClick={() => changeSelection(selectedIds.filter(id => id !== rule.id))}>Remove</button>}
          </div>
          <p className="mt-2 break-words text-xs text-mist">{rule.aliases.slice(0, 3).map(alias => alias.pattern).join(" · ")}{rule.aliases.length > 3 ? ` · +${rule.aliases.length - 3} more` : ""}</p>
        </div>)}
      </fieldset>

      {missing && <ErrorBox error="One of these names is no longer available. Close this preview and choose from the refreshed directory." retry={onRefresh} />}
      <Disclosure title={`2. Add more names (${selectedIds.length} selected)`} open={addNamesOpen} onToggle={event => setAddNamesOpen(event.currentTarget.open)}>
        <fieldset disabled={busy} className="mt-4 space-y-3" aria-label="Add names to combine">
        <MerchantSearchField label="Find saved names to merge" value={query} mode={searchMode} onChange={setQuery} onModeChange={setSearchMode} error={matches.error} placeholder="Search names and aliases…" />
        <p className="text-xs text-mist">Only names with the same type and spending treatment can be combined. Add up to 20 other names.</p>
        {visibleCandidates.length > 0 && !matches.error ? <div className="max-h-60 overflow-y-auto rounded-lg border border-line divide-y divide-line">
          {visibleCandidates.map(rule => <label key={rule.id} className="flex cursor-pointer items-start gap-3 p-3 hover:bg-surface2">
            <input className="mt-1 h-4 w-4 shrink-0 accent-teal" type="checkbox" checked={false} disabled={selectedIds.length >= 21} aria-label={`Add ${rule.canonical_name} to merge`} onChange={() => changeSelection([...selectedIds, rule.id])} />
            <span className="min-w-0"><span className="block break-words text-sm font-medium">{rule.canonical_name}</span><span className="mt-1 block text-xs text-mist">{rule.category ?? "Existing payment category"} · {rule.n_transactions} payments</span><span className="mt-1 block break-words text-xs text-mist">{rule.aliases.slice(0, 2).map(alias => alias.pattern).join(" · ")}</span></span>
          </label>)}
        </div> : !matches.error && <p className="rounded-lg bg-surface2 p-3 text-xs text-mist">{query.trim() ? "No compatible names match this search. Try a shorter name or a statement alias." : "No other close names found. Search to find another saved name."}</p>}
        {candidates.length > 12 && <p className="text-xs text-mist">Showing the first 12 of {candidates.length} matches. Refine the search to find another name.</p>}
        </fieldset>
      </Disclosure>

      {error && <div className="space-y-2"><ErrorBox error={error} /><div className="flex flex-wrap gap-2"><button className="btn" disabled={busy || loading || !sourceIds.length || missing} onClick={refreshPreview}>Refresh preview</button><button className="btn" disabled={busy} onClick={onRefresh}>Refresh saved names</button></div></div>}
      {loading && <p role="status" className="text-sm text-mist">Checking matching rules and affected payments…</p>}
      {!sourceIds.length && <p className="rounded-lg bg-surface2 p-3 text-sm text-mist">Add another saved name to see the merge preview.</p>}
      {ready && preview && <section className="space-y-4" aria-label="Merge preview">
        <Heading level={3} variant="subsection">3. Review the suggested merge rule</Heading>
        <div className="rounded-lg bg-surface2 p-4 text-sm leading-relaxed"><p>Keep <strong>{preview.target.canonical_name}</strong> and combine {preview.sources.length + 1} saved names into one entry.</p><p className="mt-2">Future payments matching any of the <strong>{preview.aliases.length} saved aliases</strong> will use <strong>{preview.target.category ?? "their existing category"}</strong> and <strong>{treatments[preview.target.spending_treatment].toLowerCase()}</strong>.</p><p className="mt-2 text-xs text-mist">Each alias keeps its current match type and payment context. Individual transaction reviews keep priority.</p></div>
        <dl className="grid grid-cols-3 gap-2 rounded-lg border border-line p-3 text-center"><div><dt className="text-xs text-mist">Matching payments</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{preview.matched_transactions}</dd></div><div><dt className="text-xs text-mist">Payments updated</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{preview.changed_transactions}</dd></div><div><dt className="text-xs text-mist">Reviews preserved</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{preview.preserved_reviews}</dd></div></dl>
        {preview.warnings.length > 0 && <ul className="list-disc space-y-2 rounded-lg border border-amber/40 bg-amber/10 py-3 pl-7 pr-3 text-sm">{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
        <Disclosure title={`Review all ${preview.aliases.length} matching rules`}><ul className="mt-3 divide-y divide-line">{preview.aliases.map((alias, index) => <li key={`${alias.context}:${alias.match_mode}:${alias.pattern}:${index}`} className="py-2"><p className="break-words text-sm">{alias.pattern}</p><p className="mt-1 text-xs text-mist">{modes[alias.match_mode]} · {contexts[alias.context]}</p></li>)}</ul></Disclosure>
        <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-line p-3 text-sm"><input className="mt-1 h-4 w-4 shrink-0 accent-teal" type="checkbox" aria-label="I confirm these names belong to the same person or business" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} /><span>I confirm these names belong to the same person or business<span className="mt-1 block text-xs text-mist">Combine their saved aliases and use the name, category and treatment shown above.</span></span></label>
      </section>}
      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-line pt-4"><button className="btn" disabled={busy} onClick={onClose}>Cancel</button><button className="btn-primary" disabled={!ready || !confirmed || busy} onClick={() => void save()}>{busy ? "Merging names…" : "Merge names"}</button></div>
    </div>
  </Modal>;
}
