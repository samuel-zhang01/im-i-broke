import { Heading } from "./Heading";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, type MerchantKind, type MerchantRule, type SpendingTreatment } from "../api";
import type { MerchantExample, MerchantQuickInput, MerchantQuickPreview } from "../merchantInbox";
import { useCategoryOptions, CategoryOptionsError } from "./CategoryOptions";
import { money, shortDate } from "../format";
import { DialogActions, ErrorBox, Modal, Spinner } from "./ui";
import MerchantSearchField from "./MerchantSearchField";
import { searchMerchantResult, type MerchantSearchMode } from "../merchantSearch";

function contextLabel(context: string) {
  return context === "spending_bank" ? "full bank descriptions, excluding card purchases" : context === "spending" ? "non-investment payments" : context.endsWith("card") ? "card purchases" : "transfer descriptions";
}

function OriginalExamples({ examples }: { examples: MerchantExample[] }) {
  return <details><summary className="cursor-pointer text-xs font-medium underline underline-offset-4">Inspect original examples</summary><div className="mt-2 space-y-2">{examples.map(example => <div key={example.id} className="border-t border-line pt-2 text-xs"><p className="break-words">{example.description}</p><p className="mt-1 break-words text-mist">{shortDate(example.txn_date)} · {example.account} · {money(example.amount)} · {example.category}</p></div>)}</div></details>;
}

export default function MerchantQuickClassify({ id, initialValues, unsavedReviewDetails = false, onClose, onSaved }: {
  id: number; initialValues?: Partial<MerchantQuickInput>; unsavedReviewDetails?: boolean; onClose: () => void; onSaved: (message: string) => void;
}) {
  const formId = useId();
  const [form, setForm] = useState<MerchantQuickInput | null>(null);
  const categories = useCategoryOptions(form?.category);
  const [names, setNames] = useState<MerchantRule[]>([]);
  const [preview, setPreview] = useState<MerchantQuickPreview | null>(null);
  const [previewIdentity, setPreviewIdentity] = useState("");
  const [pending, setPending] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null), [nonce, setNonce] = useState(0);
  const [loadNonce, setLoadNonce] = useState(0);
  const [custom, setCustom] = useState(false);
  const [discardDetails, setDiscardDetails] = useState(false);
  const [showNameSearch, setShowNameSearch] = useState(false);
  const [search, setSearch] = useState("");
  const [searchMode, setSearchMode] = useState<MerchantSearchMode>("fuzzy");
  const [nameLimit, setNameLimit] = useState(6);
  const [matchLimit, setMatchLimit] = useState(5);
  const acceptedPayload = useRef("");
  const initialValuesRef = useRef(initialValues);
  initialValuesRef.current = initialValues;
  const saving = useRef(false);
  const payload = form ? JSON.stringify(form) : "";
  const identity = form ? JSON.stringify([form.example_id, form.merchant_id ?? null, form.canonical_name, form.kind, form.spending_treatment, form.update_category]) : "";
  const currentPreview = previewIdentity === identity ? preview : null;
  const previewIsCurrent = !!payload && acceptedPayload.current === payload;
  const needsTransferConfirmation = !!currentPreview?.own_transfer_confirmation_required && !form?.confirm_own_transfer;
  const selectedIds = new Set(form?.additional_example_ids || []);
  const selectedCount = selectedIds.size;
  const candidates = currentPreview?.similar_names || [];
  const visibleCandidates = candidates.filter((candidate, index) => index < matchLimit || selectedIds.has(candidate.example_id));
  const hiddenCount = candidates.length - visibleCandidates.length;

  useEffect(() => { setMatchLimit(5); }, [identity]);
  useEffect(() => { setNameLimit(6); }, [search, searchMode]);
  const nameSearch = useMemo(() => searchMerchantResult(names, search, searchMode), [names, search, searchMode]);

  useEffect(() => {
    let active = true;
    acceptedPayload.current = "";
    setError(null); setForm(null); setPreview(null); setPreviewIdentity("");
    Promise.all([api.review(id), api.merchants()]).then(([data, directory]) => {
      if (!active) return;
      const t = data.transaction;
      const assigned = t.classification?.merchant_id;
      const current = directory.items.find(rule => rule.id === assigned);
      setNames(directory.items);
      const draft = initialValuesRef.current;
      const category = draft?.category ?? t.category ?? "Uncategorized";
      setCustom(false);
      setForm({ example_id: id, merchant_id: draft?.merchant_id !== undefined ? draft.merchant_id : current?.id || null,
        canonical_name: draft?.canonical_name ?? (t.merchant || t.source_merchant || ""), category,
        kind: draft?.kind ?? (t.merchant_kind === "unknown" ? "merchant" : t.merchant_kind || "merchant"),
        spending_treatment: draft?.spending_treatment ?? t.spending_treatment ?? "normal",
        additional_example_ids: draft?.additional_example_ids || [], update_category: draft?.update_category ?? false, confirm_own_transfer: false });
    }).catch(e => active && setError(String(e)));
    return () => { active = false; };
  }, [id, loadNonce]);

  useEffect(() => {
    if (!payload) return;
    let active = true;
    acceptedPayload.current = "";
    setPending(true); setError(null);
    const timer = setTimeout(() => {
      api.previewMerchantClassification(JSON.parse(payload)).then(result => {
        if (!active) return;
        setPreview(result); setPreviewIdentity(identity); acceptedPayload.current = payload;
      }).catch(e => active && setError(String(e))).finally(() => active && setPending(false));
    }, 350);
    return () => { active = false; clearTimeout(timer); };
  }, [payload, identity, nonce]);

  function change<K extends keyof MerchantQuickInput>(field: K, value: MerchantQuickInput[K]) {
    // The payload comparison invalidates actual edits. Clearing the accepted
    // payload here would strand a no-op edit: the preview effect would not rerun.
    if (field === "merchant_id") {
      const selected = names.find(name => name.id === value);
      setCustom(false);
      setForm(previous => previous ? { ...previous, merchant_id: value as number | null,
        category: selected?.category || previous.category, update_category: false,
        additional_example_ids: [], confirm_own_transfer: false } : previous);
      return;
    }
    const changesIdentity = ["example_id", "merchant_id", "canonical_name", "kind", "spending_treatment"].includes(field);
    setForm(previous => previous ? { ...previous, [field]: value,
      ...(changesIdentity && previous[field] !== value ? { additional_example_ids: [], confirm_own_transfer: false } : {}) } : previous);
  }
  function toggleSimilar(exampleId: number, checked: boolean) {
    setForm(previous => {
      if (!previous) return previous;
      const selected = new Set(previous.additional_example_ids || []);
      if (checked && selected.size < 30) selected.add(exampleId);
      else if (!checked) selected.delete(exampleId);
      return { ...previous, additional_example_ids: [...selected].sort((a, b) => a - b) };
    });
  }
  function showMoreMatches() {
    const next = candidates.filter((candidate, index) => index >= matchLimit && !selectedIds.has(candidate.example_id)).slice(0, 5);
    if (next.length) setMatchLimit(candidates.indexOf(next[next.length - 1]) + 1);
  }
  async function save() {
    if ((unsavedReviewDetails && !discardDetails) || !form || !currentPreview || pending || acceptedPayload.current !== payload || saving.current || currentPreview.conflict || needsTransferConfirmation) return;
    saving.current = true; setBusy(true); setError(null);
    try {
      const result = await api.classifyMerchant({ ...form, preview_token: currentPreview.preview_token });
      onSaved(result.message); onClose();
    } catch (e) {
      setError(String(e)); acceptedPayload.current = "";
    } finally { saving.current = false; setBusy(false); }
  }
  const matchingNames = nameSearch.items;
  const existing = names.find(name => name.id === form?.merchant_id);
  const selectableNames = existing && !matchingNames.some(name => name.id === existing.id) ? [existing, ...matchingNames] : matchingNames;
  return <Modal title="Teach Am I Broke? this name" onClose={() => !saving.current && onClose()} footer={form && <DialogActions formId={formId} onCancel={onClose} busy={busy} disabled={(unsavedReviewDetails && !discardDetails) || pending || !currentPreview || !!currentPreview.conflict || needsTransferConfirmation || acceptedPayload.current !== payload} label={busy ? "Saving…" : selectedCount ? `Save ${selectedCount + 1} exact aliases` : "Save exact rule"} />}>
    <p className="mb-4 text-sm leading-relaxed text-mist">Name and categorise matching past and future payments. Individual reviews take priority.</p>
    <CategoryOptionsError state={categories} />
    {!form && !error && <Spinner label="Loading statement context…" />}
    {error && <div className="mb-4"><ErrorBox error={error} retry={form ? () => setNonce(n => n + 1) : () => setLoadNonce(n => n + 1)} /></div>}
    {form && <form id={formId} onSubmit={event => { event.preventDefault(); void save(); }} aria-busy={busy}>
      <fieldset disabled={busy} className="min-w-0 space-y-4 [&_input]:min-w-0 [&_select]:min-w-0">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-x-3"><label htmlFor={`${formId}-name`} className="text-sm font-medium">Saved name</label><button type="button" className="min-h-11 text-sm text-teal underline decoration-line underline-offset-4" aria-expanded={showNameSearch} aria-controls={`${formId}-search`} onClick={() => { setShowNameSearch(value => !value); setSearch(""); }}>Search saved names <span aria-hidden="true">{showNameSearch ? "▴" : "▾"}</span></button></div>
          <div id={`${formId}-search`} hidden={!showNameSearch}>
          {showNameSearch && <MerchantSearchField label="Find a saved name" value={search} mode={searchMode} onChange={setSearch} onModeChange={setSearchMode} error={nameSearch.error} />}
          </div>
          <select id={`${formId}-name`} className="input w-full" aria-label="Saved name" value={form.merchant_id || ""} onChange={e => change("merchant_id", Number(e.target.value) || null)}>
            <option value="">Create a new name and category</option>
            {selectableNames.map(name => <option key={name.id} value={name.id}>{name.canonical_name} · {name.category || "Keep category"} · {name.aliases.length} aliases</option>)}
          </select>
        </div>
        {search.trim() && !nameSearch.error && <div className="space-y-2 rounded-lg border border-line p-3" aria-label="Saved name search results">
          <p className="text-xs text-mist">{matchingNames.length} saved {matchingNames.length === 1 ? "name matches" : "names match"}. Choose a name to reuse its rule.</p>
          <div className="max-h-64 space-y-2 overflow-y-auto pr-1">{matchingNames.slice(0, nameLimit).map(name => <button type="button" key={name.id} className={`btn block w-full text-left ${name.id === form.merchant_id ? "border-teal bg-surface2" : ""}`} aria-pressed={name.id === form.merchant_id} onClick={() => change("merchant_id", name.id)}><span className="block">{name.canonical_name}</span><span className="mt-1 block text-xs font-normal text-mist">{name.category || "Keep category"} · {name.n_transactions} payments · {name.aliases.length} aliases</span></button>)}</div>
          {matchingNames.length > nameLimit && <button type="button" className="btn text-xs" onClick={() => setNameLimit(limit => limit + 6)}>Show more saved names ({matchingNames.length - nameLimit} remaining)</button>}
          {!matchingNames.length && <p className="text-xs text-mist">No saved merchant matches. An automatic category or a single-transaction review does not create a saved merchant. Clear the search to browse all saved names, or create a new name below.</p>}
        </div>}
        {existing ? <div className="space-y-3 rounded-lg bg-surface2 p-3 text-xs leading-relaxed">
          <p>This adds the selected exact aliases to <strong>{existing.canonical_name}</strong>. Its saved category is {existing.category || "keep existing"}.</p>
          {(!existing.category || existing.category === "Uncategorized") && !form.update_category && <p>This saved name has no spending category. Choose a category below to categorise its payments.</p>}
          <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={!!form.update_category} onChange={e => change("update_category", e.target.checked)} /><span>Change the saved category for this name</span></label>
          {form.update_category && <>
            <label className="block font-medium">Category<select className="input mt-1 w-full" value={custom ? "__custom" : form.category} onChange={e => { setCustom(e.target.value === "__custom"); if (e.target.value !== "__custom") change("category", e.target.value); }}>
              {categories.options.map(category => <option key={category}>{category}</option>)}<option value="__custom">Custom category…</option>
            </select></label>
            {custom && <input required aria-label="Custom category" className="input w-full" maxLength={120} value={form.category} onChange={e => change("category", e.target.value)} />}
            <p>This changes the category for all past and future payments matching this name’s saved aliases. Individual reviews keep priority. The preview includes those payments.</p>
          </>}
        </div> : <div className="grid gap-4 sm:grid-cols-2 [&>label]:min-w-0">
          <label className="field-label text-foam">Display name<input required maxLength={120} className="input w-full" value={form.canonical_name} onChange={e => change("canonical_name", e.target.value)} /></label>
          <label className="field-label text-foam">Who is this?<select className="input w-full" value={form.kind} onChange={e => {
            const kind = e.target.value as MerchantKind;
            setForm(previous => previous ? { ...previous, kind, category: kind === "person" ? "Friends & shared costs" : kind === "self" ? "Transfers & payments" : previous.category, spending_treatment: kind === "self" ? "self_transfer" : "normal", additional_example_ids: [], confirm_own_transfer: false } : previous);
            if (kind === "person" || kind === "self") setCustom(false);
          }}><option value="merchant">Business / merchant</option><option value="person">Friend / person</option><option value="self">Me / my own account</option><option value="financial">Financial provider</option><option value="unknown">Still unknown</option></select></label>
          <label className="field-label text-foam">Category<select className="input w-full" value={custom ? "__custom" : form.category} onChange={e => {
            setCustom(e.target.value === "__custom");
            if (e.target.value !== "__custom") change("category", e.target.value);
          }}>{categories.options.map(category => <option key={category}>{category}</option>)}<option value="__custom">Custom category…</option></select>
            {custom && <input required aria-label="Custom category" className="input mt-2 w-full" maxLength={120} value={form.category} onChange={e => change("category", e.target.value)} />}
          </label>
          <label className="field-label text-foam">How should payments count?<select className="input w-full" value={form.spending_treatment} onChange={e => change("spending_treatment", e.target.value as SpendingTreatment)}><option disabled={form.kind === "self"} value="normal">Outgoing payments count in spending</option><option disabled={form.kind === "self"} value="reimbursement">Shared costs · receipts offset spending</option><option value="self_transfer">Own transfer · exclude in and out</option></select></label>
        </div>}
        {form.kind === "person" && !existing && <p className="text-xs text-mist">Friends default to Friends & shared costs. Their outgoing payments remain expenses until you choose a different treatment.</p>}
        {currentPreview?.own_transfer_confirmation_required && <section aria-label="Confirm own transfer" className="space-y-3 rounded-lg border border-amber/40 bg-surface2 p-4">
          <Heading level={3} variant="subsection">Confirm this moves money between your own accounts</Heading>
          <p className="text-xs leading-relaxed text-mist">This statement does not identify the payment as a transfer. Check the original description before teaching Am I Broke? to exclude it from spending.</p>
          <p className="break-words text-sm">“{currentPreview.pattern}”</p>
          <p className="text-xs text-mist">{currentPreview.matched_transactions} matching transactions · {money(currentPreview.outgoing_total)} recorded outgoing. Your saved transaction reviews keep priority.</p>
          <OriginalExamples examples={currentPreview.examples} />
          <label className="flex cursor-pointer items-start gap-3 text-sm leading-relaxed">
            <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 accent-teal" checked={!!form.confirm_own_transfer} onChange={event => change("confirm_own_transfer", event.target.checked)} />
            <span>I confirm this exact bank description is a transfer between my own accounts. Exclude matching past and future payments from spending.</span>
          </label>
        </section>}
        {!currentPreview?.own_transfer_confirmation_required && (!currentPreview || candidates.length > 0) && <section aria-label="Similar statement names" className="rounded-lg border border-line p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Heading level={3} variant="subsection">Similar statement names</Heading>
            {selectedCount > 0 && <div className="flex flex-wrap items-center gap-2"><span className="pill bg-surface2">{selectedCount} selected</span><button type="button" className="btn text-xs" onClick={() => change("additional_example_ids", [])}>Clear selected matches</button></div>}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-mist">Select only labels for the same business or person. Your choices are included in this rule; nothing is selected automatically.</p>
          {!currentPreview && <p role="status" className="mt-3 text-xs text-mist">{pending ? "Finding similar names in your statements…" : error ? "Fix the details above to check similar names." : "Checking statement names…"}</p>}
          {currentPreview && (candidates.length ? <div className="mt-3 space-y-3">
            <p className="text-xs text-mist">Showing {visibleCandidates.length} of {candidates.length} suggestions.{selectedCount > 0 ? " Selected matches stay visible." : ""}</p>
            {visibleCandidates.map(candidate => <div key={candidate.example_id} className={`rounded-lg border p-3 ${selectedIds.has(candidate.example_id) ? "border-teal bg-surface2" : "border-line"}`}>
              <label className="flex cursor-pointer items-start gap-3">
                <input type="checkbox" className="mt-1 h-4 w-4 shrink-0 accent-teal" checked={selectedIds.has(candidate.example_id)} disabled={!selectedIds.has(candidate.example_id) && selectedCount >= 30} onChange={event => toggleSimilar(candidate.example_id, event.target.checked)} />
                <span className="min-w-0 flex-1">
                  <span className="block break-words text-sm font-medium">{candidate.label}</span>
                  <span className="mt-1 block text-xs text-mist">{candidate.n_transactions} {candidate.n_transactions === 1 ? "transaction" : "transactions"} · {money(candidate.outgoing_total)} outgoing · {contextLabel(candidate.context)}</span>
                </span>
              </label>
              <p className="mt-2 text-xs leading-relaxed text-mist">{Math.round(candidate.score * 100)}% spelling similarity · {candidate.reason}</p>
              <div className="mt-2"><OriginalExamples examples={candidate.examples} /></div>
            </div>)}
            {(hiddenCount > 0 || matchLimit > 5) && <div className="flex flex-wrap gap-2">
              {hiddenCount > 0 && <button type="button" className="btn text-xs" onClick={showMoreMatches}>Show more matches ({hiddenCount} remaining)</button>}
              {matchLimit > 5 && <button type="button" className="btn text-xs" onClick={() => setMatchLimit(5)}>Show fewer matches</button>}
            </div>}
          </div> : <p className="mt-3 text-xs text-mist">No other unassigned statement names are similar enough to suggest.</p>)}
          {selectedCount >= 30 && <p className="mt-2 text-xs text-mist">You can add up to 30 similar labels at once.</p>}
          {currentPreview && (pending || !previewIsCurrent) && <p role="status" className="mt-3 text-xs text-mist">{error ? "Your selections are kept. Resolve the preview error before saving." : "Updating the preview. Your selections are kept."}</p>}
        </section>}
        <div aria-live="polite" aria-busy={pending} className="rounded-lg border border-line p-4">
          {pending || (!previewIsCurrent && !error) ? <p className="text-sm text-mist">Checking totals for {selectedCount + 1} exact {selectedCount ? "aliases" : "alias"}…</p> : error ? <p className="text-xs text-mist">A fresh preview is needed before these rules can be saved.</p> : currentPreview && <div className="space-y-3">
            <p className="text-sm"><strong>{currentPreview.canonical_name}</strong> · {currentPreview.category || "Keep each transaction’s existing category"}</p>
            <p className="text-xs text-mist">{existing ? "Saved rule treatment" : "Preview treatment (not saved yet)"}: {currentPreview.spending_treatment === "self_transfer" ? "own transfer; exclude money in and out" : currentPreview.spending_treatment === "reimbursement" ? "shared costs; receipts offset spending" : "outgoing payments count in spending"}. {existing ? (form.update_category ? "The saved category will be updated." : "This uses the existing saved merchant rule.") : "Save the rule below to remember this name and its aliases."}</p>
            <p className="text-sm"><strong>{currentPreview.matched_transactions} matching transactions</strong> · {money(currentPreview.outgoing_total)} recorded outgoing</p>
            <p className="break-words text-xs text-mist">Always included: “{currentPreview.pattern}” · {contextLabel(currentPreview.context)}. {currentPreview.preserved_reviews} saved individual reviews kept. Outgoing is before refunds and analytics exclusions.</p>
            {currentPreview.additional_aliases.length > 0 && <p className="break-words text-xs text-mist">Also selected: {currentPreview.additional_aliases.map(candidate => `“${candidate.label}”`).join(", ")}. All {currentPreview.alias_count} exact aliases will be saved together.</p>}
            {currentPreview.learned_suggestion && !existing && <div className="rounded-md bg-surface2 p-3 text-xs"><p>{currentPreview.learned_suggestion.explanation}</p><button className="btn mt-2 text-xs" type="button" onClick={() => { change("category", currentPreview.learned_suggestion!.category); setCustom(false); }}>Use {currentPreview.learned_suggestion.category}</button></div>}
            {currentPreview.conflict && <div className="rounded-md border border-amber/40 p-3 text-xs"><p>This exact label already belongs to {currentPreview.conflict.canonical_name}.</p><button className="btn mt-2 text-xs" type="button" onClick={() => change("merchant_id", currentPreview.conflict!.merchant_id)}>Use this saved name</button></div>}
            <OriginalExamples examples={currentPreview.examples} />
          </div>}
        </div>
        {unsavedReviewDetails && <label className="flex items-start gap-3 rounded-lg border border-amber/40 p-3 text-xs leading-relaxed">
          <input type="checkbox" className="mt-1 shrink-0" checked={discardDetails} onChange={event => setDiscardDetails(event.target.checked)} />
          <span>Continue without my unsaved note, exclusion or review outcome. This saves the matching rule only. Cancel to return and save those transaction details.</span>
        </label>}
      </fieldset>
    </form>}
  </Modal>;
}
