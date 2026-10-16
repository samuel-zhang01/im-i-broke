import { useState } from "react";
import { Disclosure } from "./Disclosure";
import { DataTable } from "./DataTable";
import { productRequest, fetchWorkspacePreferences } from "../productApi";
import { ErrorBox, Spinner, useAsync } from "./ui";
import TransactionReview from "./TransactionReview";
import NativeTransactionReview, { type NativeReportEvent } from "./NativeTransactionReview";
import { openFinancePage } from "../navigation";
import { nativeCurrencyPrecision } from "../banking";

type Report = {
  currency: string; totals: { income: number; spending: number; net: number };
  monthly: { month: string; income: number; spending: number; net: number }[];
  categories: { category: string; spending: number }[];
  partial: boolean; missing_fx_count: number; possible_overlap_count: number; stale_review_count: number; source_review_count: number;
  event_count: number; report_revision: string; offset: number; limit: number; next_offset: number | null; method: string;
  events: NativeReportEvent[];
  coverage: Record<string, { complete: boolean; missing_accounts: string[] }>;
};

export default function CurrencyReport() {
  const preferences = useAsync(fetchWorkspacePreferences);
  const [offset, setOffset] = useState(0); const [revision, setRevision] = useState<string | null>(null);
  const [from, setFrom] = useState(""); const [to, setTo] = useState("");
  const [notice, setNotice] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [fxCurrency, setFxCurrency] = useState("EUR"); const [fxDate, setFxDate] = useState("");
  const [rate, setRate] = useState(""); const [evidence, setEvidence] = useState("");
  const [review, setReview] = useState<NativeReportEvent | null>(null);
  const params = new URLSearchParams({ offset: String(offset) }); if (from) params.set("date_from", from); if (to) params.set("date_to", to);
  if (offset && revision) params.set("revision", revision);
  const report = useAsync(signal => productRequest<Report>(`workspace/reports/cashflow?${params}`, "GET", undefined, signal), [from, to, offset, revision, preferences.data?.reporting_currency]);
  const data = report.data;
  function restartEvidence() { setOffset(0); setRevision(null); report.refetch(); }
  function reviewSaved() {
    setReview(null); restartEvidence();
    setNotice("Review saved. Evidence starts again at the first page with your updated report.");
  }
  let locale = preferences.data?.locale || "en-GB";
  let localeFallback = false;
  try { Intl.getCanonicalLocales(locale); }
  catch { locale = "en-GB"; localeFallback = true; }
  const money = (value: number | null, code = data?.currency || "GBP") => value === null ? "Unavailable" : new Intl.NumberFormat(locale, {
    style: "currency", currency: code,
    minimumFractionDigits: nativeCurrencyPrecision(code), maximumFractionDigits: nativeCurrencyPrecision(code),
  }).format(value);
  const rateFormat = new Intl.NumberFormat(locale, { maximumSignificantDigits: 6 });
  async function refresh() {
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await productRequest<{ saved: number; missing: number }>("workspace/exchange-rates/refresh", "POST");
      setNotice(`${result.saved} dated references saved. ${result.missing} remain unavailable.`); restartEvidence();
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  return <div className="space-y-4">
    <p className="text-sm text-mist">View supported activity in your reporting currency. Original amounts stay intact. Planning and historical statement views keep their labelled GBP amounts.</p>
    {localeFallback && <p role="status" className="text-sm text-amber">The saved locale could not be used. Numbers use en-GB until you update Workspace preferences.</p>}
    <div className="flex flex-wrap items-end gap-3">
      <label className="text-sm">From<input className="input mt-1 block" type="date" value={from} onChange={event => { setFrom(event.target.value); setOffset(0); setRevision(null); }} /></label>
      <label className="text-sm">To<input className="input mt-1 block" type="date" value={to} onChange={event => { setTo(event.target.value); setOffset(0); setRevision(null); }} /></label>
      <button type="button" className="btn" disabled={busy} onClick={() => void refresh()}>{busy ? "Refreshing…" : "Refresh dated exchange rates"}</button>
    </div>
    {notice && <p role="status" className="text-sm text-kelp">{notice}</p>}
    {error && <ErrorBox error={error} />}
    {preferences.error && <ErrorBox error={preferences.error} retry={preferences.refetch} />}
    {report.error && <ErrorBox error={report.error} retry={offset ? restartEvidence : report.refetch} />}
    {report.error && offset > 0 && <button type="button" className="btn" onClick={restartEvidence}>Return to first evidence page</button>}
    {!data && report.loading && <Spinner label="Loading currency report…" />}
    {data && report.loading && <p role="status" className="text-sm text-mist">Updating report… Previous results remain visible until the new report is ready.</p>}
    {data && report.error && <p role="status" className="text-sm text-mist">The report could not be updated. Results below are from the previous successful request and may not match your current filters.</p>}
    {data && <>
      {data.partial && <p role="status" className="rounded-lg border border-line p-3 text-sm">Partial totals: {data.missing_fx_count} {data.missing_fx_count === 1 ? "transaction needs" : "transactions need"} dated exchange rates; {data.possible_overlap_count} possible source {data.possible_overlap_count === 1 ? "overlap needs" : "overlaps need"} review.{data.stale_review_count ? ` ${data.stale_review_count} ${data.stale_review_count === 1 ? "review needs" : "reviews need"} checking after bank evidence changed.` : ""}{data.source_review_count ? ` ${data.source_review_count} ${data.source_review_count === 1 ? "transaction needs" : "transactions need"} bank source evidence checked.` : ""} These amounts are excluded from the totals below.</p>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">{[["Income", data.totals.income], ["Spending", data.totals.spending], ["Net", data.totals.net]].map(([label, value]) => <div className="rounded-xl border border-line p-4" key={label}><p className="text-xs text-mist">{label} · {data.currency}</p><p className="mt-1 text-xl font-semibold tabular-nums">{money(Number(value))}</p></div>)}</div>
      <div className="overflow-x-auto"><DataTable className="w-full text-sm"><thead><tr className="border-b border-line text-left"><th className="p-2">Month</th><th className="p-2 text-right">Income</th><th className="p-2 text-right">Spending</th><th className="p-2 text-right">Net</th><th className="p-2">History</th></tr></thead><tbody>{data.monthly.map(month => <tr className="border-b border-line" key={month.month}><td className="p-2">{month.month}</td><td className="p-2 text-right tabular-nums">{money(month.income)}</td><td className="p-2 text-right tabular-nums">{money(month.spending)}</td><td className="p-2 text-right tabular-nums">{money(month.net)}</td><td className="p-2 text-xs text-mist">{data.coverage[month.month]?.complete ? "Covered" : "Incomplete"}</td></tr>)}</tbody></DataTable></div>
      {!data.event_count && <p className="text-sm text-mist">No booked activity in this period. Connect a bank or import a CSV in Settings.</p>}
      <Disclosure title={`Amounts and conversion evidence (${data.event_count} ${data.event_count === 1 ? "transaction" : "transactions"})`}><div role="region" aria-label="Conversion evidence records" tabIndex={0} className="max-h-96 overflow-auto"><DataTable className="w-full text-xs"><thead><tr className="text-left"><th className="p-2">Date / account</th><th className="p-2">Description</th><th className="p-2 text-right">Original</th><th className="p-2 text-right">Report</th><th className="p-2">Conversion / treatment</th><th className="p-2">Review</th></tr></thead><tbody>{data.events.map(event => <tr key={event.event_id} className="border-t border-line"><td className="p-2">{event.txn_date}<br />{event.account_name}</td><td className="max-w-64 break-words p-2">{event.description}<br /><span className="text-mist">{event.category}</span></td><td className="whitespace-nowrap p-2 text-right">{money(event.native_amount, event.currency)}</td><td className="whitespace-nowrap p-2 text-right">{money(event.reporting_amount)}</td><td className="p-2">{event.omission_reason || (event.source_review ? event.review_reason || "Bank source evidence needs review." : event.fx ? `${event.fx.source} · ${event.fx.date} · ${rateFormat.format(event.fx.rate)}` : "Missing rate")}{event.analysis_component && <p className="mt-1 text-mist">{event.analysis_component}</p>}</td><td className="p-2"><button className="btn text-xs" type="button" disabled={!!report.error || report.loading} onClick={() => event.source_review ? openFinancePage("banking", event.account_id) : setReview(event)}>{event.source_review ? "Review bank evidence" : "Review"}</button></td></tr>)}</tbody></DataTable></div>{data.event_count > data.events.length && <nav aria-label="Report evidence pages" className="mt-3 flex flex-wrap items-center gap-3">
        <p role="status" className="text-xs text-mist">{report.loading ? "Loading evidence…" : `Showing ${data.events.length ? (data.offset || 0) + 1 : 0}–${(data.offset || 0) + data.events.length} of ${data.event_count} transactions`}</p>
        <button type="button" className="btn text-xs" disabled={report.loading || !!report.error || !(data.offset > 0)} onClick={() => { setRevision(data.report_revision); setOffset(Math.max(0, data.offset - data.limit)); }}>Previous evidence</button>
        <button type="button" className="btn text-xs" disabled={report.loading || !!report.error || data.next_offset == null} onClick={() => { setRevision(data.report_revision); setOffset(data.next_offset!); }}>Next evidence</button>
      </nav>}</Disclosure>
      <p className="text-xs text-mist">{data.method}</p>
    </>}
    <Disclosure title="Supply a dated exchange reference"><form className="space-y-3" onSubmit={event => { event.preventDefault(); setBusy(true); setError(""); void productRequest("workspace/exchange-rates", "POST", { currency: fxCurrency, observed_date: fxDate, rate_to_gbp: Number(rate), evidence }).then(() => { setNotice("Exchange reference saved."); restartEvidence(); }).catch(cause => setError(cause.message)).finally(() => setBusy(false)); }}>
      <p className="text-xs text-mist">Enter pounds per one unit of the original currency. For example, if €1 equals £0.85, enter 0.85. Include the date and source. This changes reporting estimates only.</p>
      <div className="grid gap-3 sm:grid-cols-3"><label className="text-sm">Currency<input required pattern="[A-Z]{3}" maxLength={3} className="input mt-1 block w-full" value={fxCurrency} onChange={event => setFxCurrency(event.target.value.toUpperCase())} /></label><label className="text-sm">Observed date<input required type="date" className="input mt-1 block w-full" value={fxDate} onChange={event => setFxDate(event.target.value)} /></label><label className="text-sm">GBP per unit<input required type="number" step="any" min="0.000000001" className="input mt-1 block w-full" value={rate} onChange={event => setRate(event.target.value)} /></label></div>
      <label className="block text-sm">Source or evidence<input required minLength={8} maxLength={1000} className="input mt-1 block w-full" value={evidence} onChange={event => setEvidence(event.target.value)} /></label><button className="btn" disabled={busy}>Save reference</button>
    </form></Disclosure>
    {review?.event_id.startsWith("ledger:") && <TransactionReview id={Number(review.event_id.slice(7))} onClose={() => setReview(null)} onSaved={reviewSaved} />}
    {review?.event_id.startsWith("bank:") && !review.source_review && <NativeTransactionReview event={review} onClose={() => setReview(null)} onSaved={reviewSaved} />}
  </div>;
}
