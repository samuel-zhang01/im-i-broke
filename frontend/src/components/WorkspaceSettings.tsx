import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { fetchWorkspacePreferences, productRequest, type CategoryTreatment, type WorkspaceCategory, type WorkspacePreferences } from "../productApi";
import { ErrorBox, Spinner, useAsync } from "./ui";

export const settingsInput = "settings-input mt-1 min-h-11 w-full min-w-0 rounded-lg border border-line bg-white px-3 py-2 text-sm transition-colors focus:border-teal focus:outline-none focus:ring-2 focus:ring-teal/15 disabled:bg-surface2 disabled:text-mist";
const currencies = [
  ["GBP", "British pound"], ["EUR", "Euro"], ["USD", "US dollar"], ["SGD", "Singapore dollar"],
  ["AUD", "Australian dollar"], ["CAD", "Canadian dollar"], ["CHF", "Swiss franc"],
  ["HKD", "Hong Kong dollar"], ["JPY", "Japanese yen"], ["NZD", "New Zealand dollar"],
  ["KWD", "Kuwaiti dinar"], ["INR", "Indian rupee"], ["AED", "UAE dirham"], ["CNY", "Chinese yuan"],
] as const;
const locales = [
  ["en-GB", "English (UK)"], ["en-US", "English (US)"], ["de-DE", "German (Germany)"],
  ["fr-FR", "French (France)"], ["en-SG", "English (Singapore)"], ["ja-JP", "Japanese (Japan)"],
  ["en-IN", "English (India)"], ["ar-KW", "Arabic (Kuwait)"],
] as const;
const commonTimezones = ["UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Asia/Singapore", "Asia/Tokyo", "Asia/Kolkata", "Asia/Kuwait", "Australia/Sydney", "Pacific/Auckland"];
function timezoneSuggestions() {
  // Keep common choices available on browsers without supportedValuesOf.
  const supported = Intl as typeof Intl & { supportedValuesOf?: (key: "timeZone") => string[] };
  try { return [...new Set([...commonTimezones, ...(supported.supportedValuesOf?.("timeZone") || [])])].sort(); }
  catch { return commonTimezones; }
}
const timezones = timezoneSuggestions();
function deviceTimezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; }
  catch { return null; }
}
function numberFormatExample(locale: string) {
  try { return new Intl.NumberFormat(locale).format(1234567.89); }
  catch { return "Choose a valid locale to see its number format."; }
}
export function CurrencyInput({ value, onChange, disabled = false }: { value: string; onChange: (value: string) => void; disabled?: boolean }) {
  const id = useId();
  return <><input className={settingsInput} required pattern="[A-Z]{3}" maxLength={3} list={id} disabled={disabled} value={value} onChange={event => onChange(event.target.value.toUpperCase())} /><datalist id={id}>{currencies.map(([currency, name]) => <option key={currency} value={currency} label={name} />)}</datalist></>;
}

function PreferenceForm({ initial }: { initial: WorkspacePreferences }) {
  const [values, setValues] = useState(initial);
  const localeList = useId(); const timezoneList = useId();
  const [localTimezone] = useState(deviceTimezone);
  const edited = useRef(new Set<keyof WorkspacePreferences>());
  const savedPreferences = useRef<WorkspacePreferences | null>(null);
  useEffect(() => {
    // Rebase refreshed values only where the person has not expressed an edit.
    // Returning an edited field to its original value remains intentional.
    setValues(current => ({ ...initial, ...Object.fromEntries([...edited.current].map(key => [key, current[key]])) }));
    if (savedPreferences.current && (Object.keys(initial) as (keyof WorkspacePreferences)[]).some(key => initial[key] !== savedPreferences.current![key])) setSaved(false);
  }, [initial.reporting_currency, initial.locale, initial.timezone]);
  function change<K extends keyof WorkspacePreferences>(key: K, value: WorkspacePreferences[K]) {
    edited.current.add(key); setValues(previous => ({ ...previous, [key]: value })); setSaved(false);
  }
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [saved, setSaved] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setSaved(false);
    try {
      try { Intl.getCanonicalLocales(values.locale); }
      catch { throw new Error("Enter a valid locale, such as en-GB or de-DE."); }
      const patch = Object.fromEntries([...edited.current].map(key => [key, values[key]]));
      const confirmed = await productRequest<WorkspacePreferences>("workspace/preferences", "PATCH", patch);
      setValues(confirmed); edited.current.clear(); savedPreferences.current = confirmed; setSaved(true);
    }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  return <form onSubmit={submit} className="space-y-4">
    <p className="text-sm text-mist">Choose the default currency for Net assets, investments, workspace reports and new manual accounts. Historical statement and planning views keep their labelled GBP amounts.</p>
    <p className="text-xs text-mist">Your time zone sets calendar dates for manual balances, history coverage, Net assets, planning and reports. Provider timestamps stay stored in UTC. Currency choices already made within a view remain in place.</p>
    <fieldset disabled={busy} className="grid min-w-0 gap-4 sm:grid-cols-3">
      <div><label className="text-sm">Reporting currency<CurrencyInput value={values.reporting_currency} onChange={reporting_currency => change("reporting_currency", reporting_currency)} /></label><p className="mt-1 text-xs leading-relaxed text-mist">Original transaction currencies stay unchanged.</p></div>
      <div><label className="text-sm">Locale<input className={settingsInput} required maxLength={35} list={localeList} value={values.locale} placeholder="en-GB" onChange={event => change("locale", event.target.value)} /></label><datalist id={localeList}>{locales.map(([locale, name]) => <option key={locale} value={locale}>{name} · {numberFormatExample(locale)}</option>)}</datalist><p className="mt-1 text-xs leading-relaxed text-mist">Choose a number-format suggestion or enter a language tag, such as en-GB. This formats amounts and exchange rates in workspace reports. The interface stays in English.</p><p className="mt-1 text-xs text-mist">Example: {numberFormatExample(values.locale)}</p></div>
      <div><label className="text-sm">Time zone<input className={settingsInput} required maxLength={100} list={timezoneList} value={values.timezone} placeholder="Europe/London" onChange={event => change("timezone", event.target.value)} /></label><datalist id={timezoneList}>{timezones.map(timezone => <option key={timezone} value={timezone}>{timezone === "UTC" ? "Coordinated Universal Time" : timezone.split("/").reverse().join(" · ").replace(/_/g, " ")}</option>)}</datalist><p className="mt-1 text-xs leading-relaxed text-mist">Sets the workspace calendar date. Start typing your city to find a suggestion, or enter a time zone such as Europe/London.</p><button type="button" className="mt-1 min-h-11 text-left text-xs text-teal underline underline-offset-2 disabled:text-mist" disabled={!localTimezone} onClick={() => { if (localTimezone) change("timezone", localTimezone); }}>Use this device’s time zone{localTimezone ? ` (${localTimezone})` : ""}</button>{!localTimezone && <p className="text-xs text-mist">Your device’s time zone is unavailable. Choose a suggestion or enter your time zone above.</p>}</div>
    </fieldset>
    {error && <ErrorBox error={error} />}<div className="flex items-center gap-3"><button className="btn" disabled={busy}>{busy ? "Saving…" : "Save workspace preferences"}</button>{saved && <p role="status" className="text-sm text-teal">Preferences saved.</p>}</div>
  </form>;
}
export function WorkspacePreferencesSettings() {
  const state = useAsync(signal => fetchWorkspacePreferences(signal));
  if (state.error && !state.data) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading workspace preferences…" />;
  return <div className="space-y-4">
    {state.error && <><ErrorBox error={state.error} retry={state.refetch} /><p role="status" className="text-sm text-mist">Preferences could not be refreshed. Your unsaved changes are still here.</p></>}
    <PreferenceForm initial={state.data} />
  </div>;
}

const treatments: { value: CategoryTreatment; label: string }[] = [{ value: "normal", label: "Spending" }, { value: "income", label: "Income" }, { value: "transfer", label: "Transfer" }, { value: "investment", label: "Investment" }, { value: "refund", label: "Refund" }];
function CategoryForm({ category, onDone, onBusyChange }: { category?: WorkspaceCategory; onDone: () => void; onBusyChange: (busy: boolean) => void }) {
  const [name, setName] = useState(category?.name || ""); const [treatment, setTreatment] = useState<CategoryTreatment>(category?.treatment || "normal");
  const [order, setOrder] = useState(category?.sort_order || 0); const [archived, setArchived] = useState(category?.archived || false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); onBusyChange(true); setError("");
    try { await productRequest(`workspace/categories${category ? `/${encodeURIComponent(category.id)}` : ""}`, category ? "PATCH" : "POST", { name, treatment, sort_order: order, archived }); onDone(); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); onBusyChange(false); }
  }
  return <form onSubmit={submit} className="space-y-3 rounded-xl border border-line p-4">
    <fieldset disabled={busy} className="min-w-0 space-y-3">
    <div className="grid gap-3 sm:grid-cols-3">
      <label className="text-sm">Category name<input className={settingsInput} required maxLength={100} value={name} onChange={event => setName(event.target.value)} /></label>
      <label className="text-sm">Treatment<select className={settingsInput} value={treatment} onChange={event => setTreatment(event.target.value as CategoryTreatment)}>{treatments.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      <label className="text-sm">Sort order<input className={settingsInput} type="number" min={0} max={100000} step={1} value={order} onChange={event => setOrder(Number(event.target.value))} /></label>
    </div>
    {category && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={archived} onChange={event => setArchived(event.target.checked)} />Archive category</label>}
    </fieldset>
    <p className="text-xs text-mist">Treatment changes how transactions contribute to reports. Archived categories stay on historical transactions.</p>
    {error && <ErrorBox error={error} />}<div className="flex gap-2"><button className="btn" disabled={busy}>{busy ? "Saving…" : category ? "Save category" : "Create category"}</button><button type="button" className="btn" disabled={busy} onClick={onDone}>Cancel</button></div>
  </form>;
}
export function CategorySettings() {
  const state = useAsync(signal => productRequest<{ categories: WorkspaceCategory[] }>("workspace/categories", "GET", undefined, signal));
  const [editing, setEditing] = useState<WorkspaceCategory | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  if (state.error && !state.data) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading categories…" />;
  return <div className="space-y-3">
    <p className="text-sm text-mist">Manage the categories available when classifying your transactions.</p>
    {state.error && <><ErrorBox error={state.error} retry={state.refetch} /><p role="status" className="text-sm text-mist">The category list could not be refreshed. Your unsaved changes are still here.</p></>}
    {!editing && <button className="btn" onClick={() => setEditing("new")}>Add category</button>}
    {editing && <CategoryForm key={editing === "new" ? "new" : editing.id} onBusyChange={setBusy} category={editing === "new" ? undefined : editing} onDone={() => { setEditing(null); state.refetch(); }} />}
    <ul className="max-h-96 space-y-2 overflow-y-auto">{state.data.categories.map(category => <li key={category.id} className="flex items-center justify-between gap-3 rounded-lg border border-line px-3 py-2">
      <div className="min-w-0"><p className="break-words text-sm font-medium">{category.name}{category.archived && <span className="ml-2 font-normal text-mist">Archived</span>}</p><p className="text-xs text-mist">{treatments.find(item => item.value === category.treatment)?.label || category.treatment}</p></div>
      <button className="btn shrink-0" disabled={busy} aria-label={`Edit category ${category.name}`} onClick={() => setEditing(category)}>Edit</button>
    </li>)}</ul>
  </div>;
}
