import { useState, type FormEvent } from "react";
import { productRequest } from "../productApi";
import { settingsInput } from "./WorkspaceSettings";
import { ErrorBox, Spinner, useAsync } from "./ui";

type Period = { id: string | number; account_id: string; period_start: string; period_end: string; complete: boolean; source: string; evidence: string };
export default function HistoryCoverageSettings({ accountId, accountName }: { accountId: string; accountName: string }) {
  const path = `workspace/accounts/${encodeURIComponent(accountId)}/coverage`;
  const state = useAsync(signal => productRequest<{ periods: Period[] }>(path, "GET", undefined, signal), [accountId]);
  const [start, setStart] = useState(""); const [end, setEnd] = useState(""); const [complete, setComplete] = useState(false); const [evidence, setEvidence] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [removing, setRemoving] = useState<Period | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await productRequest(path, "POST", { period_start: start, period_end: end, complete, evidence }); setEvidence(""); state.refetch(); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  async function remove(period: Period) {
    setBusy(true); setError("");
    try { await productRequest(`${path}/${encodeURIComponent(period.id)}`, "DELETE"); setRemoving(null); state.refetch(); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  return <section aria-label={`History coverage for ${accountName}`} className="space-y-3 rounded-xl border border-line p-4">
    <p className="text-sm font-medium">History coverage · {accountName}</p><p className="text-xs text-mist">Confirm a period only after checking that all transactions are present. A successful bank refresh does not prove complete history. You can also record a known gap.</p>
    {state.error && <ErrorBox error={state.error} retry={state.refetch} />}{!state.data && state.loading && <Spinner label="Loading history coverage…" />}
    {state.data && <ul className="space-y-2">{state.data.periods.map(period => <li key={period.id} className="rounded-lg border border-line p-3"><p className="text-sm">{period.period_start} – {period.period_end} · {period.complete ? "Confirmed complete" : "Known gap"}</p><p className="break-words text-xs text-mist">{period.evidence}</p><button className="btn mt-2" disabled={busy} onClick={() => setRemoving(period)}>Remove coverage note</button></li>)}</ul>}
    {removing && <div className="space-y-2"><p className="text-sm">Remove the coverage note for {removing.period_start} – {removing.period_end}? Saved transactions remain unchanged.</p><div className="flex gap-2"><button className="btn" disabled={busy} onClick={() => void remove(removing)}>Confirm removal</button><button className="btn" disabled={busy} onClick={() => setRemoving(null)}>Keep note</button></div></div>}
    <form className="space-y-3" onSubmit={submit}><fieldset disabled={busy} className="min-w-0 space-y-3"><div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">Coverage start<input className={settingsInput} required type="date" max={end || undefined} value={start} onChange={event => setStart(event.target.value)} /></label><label className="text-sm">Coverage end<input className={settingsInput} required type="date" min={start || undefined} value={end} onChange={event => setEnd(event.target.value)} /></label></div>
      <label className="block text-sm">Coverage status<select className={settingsInput} value={complete ? "complete" : "gap"} onChange={event => setComplete(event.target.value === "complete")}><option value="gap">Known gap or incomplete history</option><option value="complete">I checked that this period is complete</option></select></label>
      <label className="block text-sm">Evidence or reason<textarea className={settingsInput} required minLength={8} maxLength={1000} rows={2} placeholder="For example, checked against all monthly statements for this period" value={evidence} onChange={event => setEvidence(event.target.value)} /></label><button className="btn">{busy ? "Saving…" : "Save history coverage"}</button></fieldset></form>
    {error && <ErrorBox error={error} />}
  </section>;
}
