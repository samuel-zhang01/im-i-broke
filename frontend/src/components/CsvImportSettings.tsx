import { Disclosure } from "./Disclosure";
import { useId, useRef, useState, type FormEvent, type InvalidEvent } from "react";
import { csvColumns, productRequest, type CsvInput, type CsvMapping, type CsvPreview, type CsvTemplate, type WorkspaceAccount } from "../productApi";
import { openFinancePage } from "../navigation";
import { settingsInput } from "./WorkspaceSettings";
import { ErrorBox, Spinner, useAsync } from "./ui";

const defaultMapping: CsvMapping = { date_column: "", description_column: "", amount_column: "", date_format: "%Y-%m-%d", currency: "GBP", delimiter: ",", decimal_separator: ".", amount_multiplier: 1 };

function matchColumns(mapping: CsvMapping, headers: string[]): CsvMapping {
  const choose = (names: string[]) => headers.find(header => names.includes(header.toLowerCase().trim())) || "";
  const existing = (column?: string) => column && headers.includes(column) ? column : undefined;
  return { ...mapping,
    date_column: existing(mapping.date_column) || choose(["date", "transaction date", "booking date", "posted date"]),
    description_column: existing(mapping.description_column) || choose(["description", "details", "merchant", "narrative"]),
    amount_column: existing(mapping.amount_column) || choose(["amount", "value", "transaction amount"]),
    debit_column: existing(mapping.debit_column), credit_column: existing(mapping.credit_column),
    currency_column: existing(mapping.currency_column), id_column: existing(mapping.id_column),
  };
}

function previewError(item: CsvPreview["errors"][number]): string {
  if (typeof item === "string") return item;
  let message = item.message || item.error || "Invalid row";
  const date = /^time data ('.*') does not match format '.*'$/.exec(message);
  if (date) message = `Date ${date[1]} does not match the selected date format. Choose the format used in your file, then preview again.`;
  return `${item.row ? `Row ${item.row}: ` : ""}${message}`;
}

export default function CsvImportSettings({ onCreateAccount, onOpenReports }: { onCreateAccount?: () => void; onOpenReports?: () => void }) {
  const accounts = useAsync(signal => productRequest<{ accounts: WorkspaceAccount[] }>("workspace/accounts", "GET", undefined, signal));
  const templates = useAsync(signal => productRequest<{ templates: CsvTemplate[] }>("workspace/imports/templates", "GET", undefined, signal));
  const [accountId, setAccountId] = useState(""); const [csvText, setCsvText] = useState(""); const [sourceName, setSourceName] = useState("CSV import");
  const [sourceFilename, setSourceFilename] = useState("");
  const [mapping, setMapping] = useState<CsvMapping>(defaultMapping); const [splitAmounts, setSplitAmounts] = useState(false);
  const [previewPage, setPreviewPage] = useState(0); const [reviewOnly, setReviewOnly] = useState(false);
  const [preview, setPreview] = useState<CsvPreview | null>(null); const [confirmOverlaps, setConfirmOverlaps] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [templateName, setTemplateName] = useState("");
  const [completed, setCompleted] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const sourceErrorId = useId();
  const fileVersion = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const columns = csvColumns(csvText, mapping.delimiter); const account = accounts.data?.accounts.find(item => item.id === accountId);
  const effectiveMapping = { ...mapping, currency: account?.currency || mapping.currency,
    ...(splitAmounts ? { amount_column: undefined } : { debit_column: undefined, credit_column: undefined }) };
  const input: CsvInput = { account_id: accountId, csv_text: csvText, mapping: effectiveMapping, source_name: sourceName, source_filename: sourceFilename };
  function invalidate() { setPreviewPage(0); setReviewOnly(false); setPreview(null); setConfirmOverlaps(false); setNotice(""); setCompleted(false); setSourceError(""); }
  function changeMapping<K extends keyof CsvMapping>(key: K, value: CsvMapping[K]) { setMapping(previous => ({ ...previous, [key]: value })); invalidate(); }
  function changeDelimiter(delimiter: string) {
    setMapping(previous => matchColumns({ ...previous, delimiter }, csvColumns(csvText, delimiter)));
    invalidate();
  }
  function revealRequiredSource(event: InvalidEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    event.preventDefault();
    const options = input.closest("details");
    if (options) options.open = true;
    setSourceError("Enter an import source name, such as CSV import. Reuse this name for future exports from this account.");
    if (input.form?.querySelector("input:invalid, select:invalid, textarea:invalid") === input) {
      requestAnimationFrame(() => { if (input.isConnected && !input.validity.valid) input.focus(); });
    }
  }
  async function fileSelected(file?: File) {
    const version = ++fileVersion.current;
    setError(""); setCsvText(""); setSourceFilename(""); invalidate();
    if (!file) return;
    if (file.size > 512 * 1024) { setError("Choose a CSV smaller than 512 KB. Split larger exports into separate files."); return; }
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (fileVersion.current !== version) return;
      setCsvText(text); setSourceFilename(file.name.slice(0, 200));
      setMapping(previous => matchColumns(previous, csvColumns(text, previous.delimiter)));
    } catch { setError("This file could not be read. Choose a UTF-8 CSV export."); }
  }
  async function generatePreview(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setPreview(null); setConfirmOverlaps(false); setNotice("");
    try {
      if (new TextEncoder().encode(JSON.stringify(input)).length > 1024 * 1024 - 512) throw new Error("This CSV is too large to send with its column mapping. Split it into smaller exports.");
      const result = await productRequest<CsvPreview>("workspace/imports/preview", "POST", input);
      setPreview(result); setPreviewPage(0); setReviewOnly(result.review_count > 0);
    }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  async function commit() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      const result = await productRequest<{ imported_count: number; duplicate_count: number }>("workspace/imports/commit", "POST", { ...input, preview_token: preview.preview_token, confirm_overlaps: confirmOverlaps });
      setPreview(null); setCsvText(""); setSourceFilename(""); setConfirmOverlaps(false);
      if (fileInput.current) fileInput.current.value = "";
      const duplicates = `${result.duplicate_count} ${result.duplicate_count === 1 ? "transaction was" : "transactions were"} already imported into this account.`;
      setNotice(result.imported_count
        ? `Import complete. ${result.imported_count} ${result.imported_count === 1 ? "transaction" : "transactions"} added to ${account?.display_name || account?.institution || "your account"}.${result.duplicate_count ? ` ${duplicates}` : ""}`
        : `No transactions added. ${duplicates}`);
      setCompleted(true);
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  async function saveTemplate() {
    setBusy(true); setError("");
    try { await productRequest("workspace/imports/templates", "POST", { name: templateName, mapping: effectiveMapping }); setTemplateName(""); templates.refetch(); setNotice("Column mapping saved."); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  function columnSelector(key: keyof CsvMapping, label: string, required = true) {
    return <label key={key} className="text-sm">{label}<select className={settingsInput} required={required} value={String(mapping[key] || "")} onChange={event => changeMapping(key, event.target.value || undefined)}><option value="">{required ? "Choose column" : "Not included"}</option>{columns.map((column, index) => <option key={`${column}-${index}`} value={column}>{column}</option>)}</select></label>;
  }
  const reviewRows = preview?.review_rows || preview?.rows.filter(row => row.status === "review") || [];
  const completeReview = !!preview && reviewRows.length === preview.review_count;
  const previewRows = reviewOnly ? reviewRows : preview?.rows || [];
  const visibleRows = previewRows.slice(previewPage * 100, (previewPage + 1) * 100);
  if (accounts.error) return <ErrorBox error={accounts.error} retry={accounts.refetch} />;
  if (!accounts.data) return <Spinner label="Loading import accounts…" />;
  return <div className="space-y-4">
    <p className="text-sm text-mist">Import a bank’s CSV export into an existing account. Review the dates, amounts and possible duplicates before saving.</p>
    {error && <ErrorBox error={error} />}{notice && <p role="status" className="text-sm text-teal">{notice}</p>}
    {completed && <button type="button" className="btn" onClick={() => onOpenReports ? onOpenReports() : openFinancePage("settings", undefined, "reports")}>View imported income and spending</button>}
    {!accounts.data.accounts.filter(item => !item.archived).length ? <div className="space-y-3 rounded-xl border border-line bg-surface2/50 p-4"><p className="text-sm text-mist">First, add the account this CSV belongs to. Its currency and name keep your records organised.</p>{onCreateAccount && <button className="btn-primary" onClick={onCreateAccount}>Go to account settings</button>}</div> : <form className="space-y-4" onSubmit={generatePreview}>
      <fieldset disabled={busy} className="min-w-0 space-y-4">
        <label className="block text-sm">Import into account<select className={settingsInput} required value={accountId} onChange={event => { setAccountId(event.target.value); invalidate(); }}><option value="">Choose an account</option>{accounts.data.accounts.filter(item => !item.archived).map(item => <option key={item.id} value={item.id}>{item.display_name || item.institution} · {item.currency}</option>)}</select></label>
        <label className="block text-sm">CSV file<input ref={fileInput} className={settingsInput} type="file" accept=".csv,text/csv" onChange={event => void fileSelected(event.target.files?.[0])} /></label>
        <p className="text-xs text-mist">UTF-8 CSV, up to 512 KB. File contents are sent to your server only when you request a preview.</p>
        {csvText && <>
          <p className="text-sm text-mist">Match the columns and formats to your file. In the preview, spending should be negative and income positive.</p>
          {columns.length === 1 && <p className="text-sm text-amber">Only one column heading was found. Check the Separator below; your export may use semicolons or tabs.</p>}
          {columns.length > 1 && (!mapping.date_column || !mapping.description_column || (!splitAmounts && !mapping.amount_column)) && <p className="text-sm text-amber">Choose the columns your bank uses for dates, descriptions and amounts. Column names can differ between banks.</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm">Separator<select className={settingsInput} value={mapping.delimiter} onChange={event => changeDelimiter(event.target.value)}><option value=",">Comma</option><option value=";">Semicolon</option><option value={"\t"}>Tab</option></select></label>
            <label className="text-sm">Decimal separator<select className={settingsInput} value={mapping.decimal_separator} onChange={event => changeMapping("decimal_separator", event.target.value)}><option value=".">Point (12.34)</option><option value=",">Comma (12,34)</option></select></label>
            {columnSelector("date_column", "Date column")}{columnSelector("description_column", "Description column")}
            <label className="text-sm">Date format<select className={settingsInput} value={mapping.date_format} onChange={event => changeMapping("date_format", event.target.value)}><option value="%Y-%m-%d">Year-month-day (2026-09-21)</option><option value="%d/%m/%Y">Day/month/year (21/09/2026)</option><option value="%m/%d/%Y">Month/day/year (09/21/2026)</option><option value="%d-%m-%Y">Day-month-year (21-09-2026)</option><option value="%Y/%m/%d">Year/month/day (2026/09/21)</option></select></label>
            <label className="text-sm">Amount format<select className={settingsInput} value={splitAmounts ? "split" : "signed"} onChange={event => { setSplitAmounts(event.target.value === "split"); invalidate(); }}><option value="signed">One signed amount column</option><option value="split">Separate money out / money in</option></select></label>
            {splitAmounts ? <>{columnSelector("debit_column", "Money out column")}{columnSelector("credit_column", "Money in column")}</> : columnSelector("amount_column", "Amount column")}
            <label className="text-sm">Amount direction<select className={settingsInput} value={mapping.amount_multiplier} onChange={event => changeMapping("amount_multiplier", Number(event.target.value))}><option value={1}>Keep signs (spending is negative)</option><option value={-1}>Reverse signs (spending is positive)</option></select></label>
            {columnSelector("id_column", "Transaction ID column (optional)", false)}
          </div>
          <p className="text-xs text-mist">Use the date order and decimal separator shown in your export. Amounts must use {account?.currency || "the selected account’s currency"}, without currency symbols or thousands separators. A stable transaction ID improves duplicate detection.</p>
          <Disclosure title="Saved mappings and other options"><div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm">Saved column mapping<select className={settingsInput} value="" onChange={event => { const template = templates.data?.templates.find(item => item.id === event.target.value); if (template) { setMapping(template.mapping); setSplitAmounts(Boolean(template.mapping.debit_column || template.mapping.credit_column)); invalidate(); } }}><option value="">Choose a saved mapping</option>{templates.data?.templates.map(template => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>
            <div><label className="text-sm">Import source<input className={settingsInput} required maxLength={200} value={sourceName} aria-invalid={sourceError ? true : undefined} aria-describedby={sourceError ? sourceErrorId : undefined} onInvalid={revealRequiredSource} onChange={event => { setSourceName(event.target.value); invalidate(); }} /></label>{sourceError && <p id={sourceErrorId} role="alert" className="mt-2 text-xs text-coral">{sourceError}</p>}</div>
            {columnSelector("currency_column", "Currency column (optional)", false)}
          </div><p className="mt-3 text-xs text-mist">Reuse the same import source name for future exports from this account. A currency column must match the selected account’s currency.</p></Disclosure>
          {templates.error && <p className="text-xs text-coral">Saved mappings could not be loaded.</p>}
          <button className="btn" disabled={!accountId}>{busy ? "Checking…" : "Preview CSV import"}</button>
        </>}
      </fieldset>
    </form>}
    {preview && <section aria-label="CSV import preview" className="space-y-3 rounded-xl border border-line p-4">
      <p className="text-sm font-medium">{preview.row_count} rows · {preview.new_count} new · {preview.duplicate_count} already imported · {preview.review_count} need review</p>
      <p className="text-xs text-mist">Check the dates, descriptions and signs below before saving. Already imported rows will be skipped. Possible overlaps need your review.</p>
      {preview.errors.length > 0 && <ul role="alert" className="space-y-1 text-sm text-coral">{preview.errors.map((item, index) => <li key={index}>{previewError(item)}</li>)}</ul>}
      {preview.review_count > 0 && <div className="flex flex-wrap gap-2" role="group" aria-label="CSV evidence view">
        <button type="button" className="btn text-xs" aria-pressed={reviewOnly} onClick={() => { setReviewOnly(true); setPreviewPage(0); }}>Possible overlaps ({preview.review_count})</button>
        <button type="button" className="btn text-xs" aria-pressed={!reviewOnly} onClick={() => { setReviewOnly(false); setPreviewPage(0); }}>File sample ({preview.rows.length})</button>
      </div>}
      <div className="max-h-80 overflow-auto"><table role="table" className="block w-full text-left text-xs sm:table"><thead className="sr-only sm:not-sr-only sm:table-header-group"><tr><th scope="col" className="p-2">CSV row</th><th scope="col" className="p-2">Date</th><th scope="col" className="p-2">Description</th><th scope="col" className="p-2 text-right">Amount</th><th scope="col" className="p-2">Review</th></tr></thead><tbody role="rowgroup" className="block sm:table-row-group">{visibleRows.map((row, index) => <tr role="row" key={`${row.row}-${index}`} className="grid grid-cols-2 gap-2 border-t border-line py-3 sm:table-row sm:py-0">
        <td role="cell" className="min-w-0 sm:p-2"><span className="mb-1 block text-mist sm:hidden">CSV row</span>{row.row}</td>
        <td role="cell" className="min-w-0 sm:whitespace-nowrap sm:p-2"><span className="mb-1 block text-mist sm:hidden">Date</span>{row.date}</td>
        <td role="cell" className="col-span-2 min-w-0 break-words sm:min-w-32 sm:max-w-xs sm:p-2"><span className="mb-1 block text-mist sm:hidden">Description</span>{row.description}</td>
        <td role="cell" className="min-w-0 break-words tabular-nums sm:whitespace-nowrap sm:p-2 sm:text-right"><span className="mb-1 block text-mist sm:hidden">Amount</span>{row.amount.toLocaleString(undefined, { maximumFractionDigits: 8 })} {row.currency}</td>
        <td role="cell" className="min-w-0 break-words sm:p-2"><span className="mb-1 block text-mist sm:hidden">Review</span>{row.status === "new" ? "Ready to import" : row.status === "duplicate" ? "Already imported" : "Check overlap"}{row.reason && <p className="text-mist sm:min-w-32">{row.reason}</p>}</td>
      </tr>)}</tbody></table></div>
      {previewRows.length > 100 && <nav aria-label="CSV evidence pages" className="flex flex-wrap items-center gap-3">
        <p role="status" className="text-xs text-mist">Showing {previewPage * 100 + 1}–{previewPage * 100 + visibleRows.length} of {previewRows.length} {reviewOnly ? "possible overlaps" : "sample rows"}</p>
        <button type="button" className="btn text-xs" disabled={previewPage === 0} onClick={() => setPreviewPage(previous => previous - 1)}>Previous CSV rows</button>
        <button type="button" className="btn text-xs" disabled={(previewPage + 1) * 100 >= previewRows.length} onClick={() => setPreviewPage(previous => previous + 1)}>Next CSV rows</button>
      </nav>}
      {reviewOnly && <p className="text-xs text-mist">Every possible overlap from the full file is available in this view. CSV row numbers include the header.</p>}
      {!reviewOnly && preview.rows.length < preview.row_count && <p className="text-xs text-mist">Sample of the first {preview.rows.length} rows. Counts and the possible-overlaps view cover the full file.</p>}
      {!completeReview && <p role="alert" className="text-sm text-coral">Some overlap evidence is unavailable. Preview again before confirming this import.</p>}
      {preview.review_count > 0 && <label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" disabled={!completeReview} checked={confirmOverlaps} onChange={event => setConfirmOverlaps(event.target.checked)} />I reviewed the possible overlaps and confirm these are separate transactions to import.</label>}
      <button className="btn" disabled={busy || !completeReview || !preview.can_import || preview.errors.length > 0 || (preview.review_count > 0 && !confirmOverlaps)} onClick={() => void commit()}>{busy ? "Importing…" : "Confirm CSV import"}</button>
      <details className="border-t border-line pt-3"><summary className="cursor-pointer text-sm font-medium">Save this column mapping (optional)</summary><div className="mt-3 flex flex-wrap items-end gap-2"><label className="min-w-0 flex-1 text-sm">Save column mapping as<input className={settingsInput} maxLength={100} value={templateName} onChange={event => setTemplateName(event.target.value)} /></label><button className="btn" disabled={busy || !templateName.trim()} onClick={() => void saveTemplate()}>Save mapping</button></div></details>
    </section>}
  </div>;
}
