import { useWorkspaceState } from "../workspaceState";
import { Heading } from "../components/Heading";
import { Disclosure } from "../components/Disclosure";
import MobileDisclosure from "../components/MobileDisclosure";
import { DataTable, TableMeta } from "../components/DataTable";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, type RecurringConfig, type RecurringSeries } from "../api";
import { Card, Pill, SectionTitle, Spinner, RefreshStatus, useAsync, PageControls, useCompactLayout } from "../components/ui";
import TransactionReview from "../components/TransactionReview";
import { money, shortDate } from "../format";
import { currentRecurringTotals, isForecastEligible, recurringDateWindow, recurringForPayment, recurringMonthlyAmount, recurringWaitingForPayment, upcomingRecurring } from "../recurring";

type SortKey =
  | "merchant" | "cadence_days" | "category" | "typical_amount"
  | "monthly_cost" | "first_seen" | "last_seen" | "n_occurrences";
const STRING_KEYS: SortKey[] = ["merchant", "category", "first_seen", "last_seen"];

const SORT_OPTIONS: { key: SortKey; label: string; defaultDir: "asc" | "desc" }[] = [
  { key: "monthly_cost", label: "Monthly cost", defaultDir: "desc" },
  { key: "first_seen", label: "First payment", defaultDir: "asc" },
  { key: "last_seen", label: "Last payment", defaultDir: "desc" },
  { key: "typical_amount", label: "Amount", defaultDir: "desc" },
  { key: "cadence_days", label: "Frequency", defaultDir: "asc" },
  { key: "n_occurrences", label: "Occurrences", defaultDir: "desc" },
  { key: "merchant", label: "Name", defaultDir: "asc" },
];

const CADENCE_TONE: Record<string, "teal" | "cyan" | "blue" | "kelp" | "amber" | "mute"> = {
  weekly: "cyan", fortnightly: "blue", monthly: "teal",
  "bi-monthly": "kelp", quarterly: "amber", annual: "mute",
};

const DEFAULTS: RecurringConfig = {
  min_occurrences: 3, amount_tol: 0.10, regularity: 0.5,
  tol_weekly: 2, tol_fortnightly: 2, tol_monthly: 5,
  tol_bimonthly: 3, tol_quarterly: 5, tol_annual: 15,
};

const CADENCE_FIELDS: { key: keyof RecurringConfig; label: string; max: number }[] = [
  { key: "tol_weekly", label: "Weekly", max: 6 },
  { key: "tol_fortnightly", label: "Fortnightly", max: 7 },
  { key: "tol_monthly", label: "Monthly", max: 14 },
  { key: "tol_bimonthly", label: "Bi-monthly", max: 20 },
  { key: "tol_quarterly", label: "Quarterly", max: 30 },
  { key: "tol_annual", label: "Annual", max: 60 },
];

function expectedTiming(series: RecurringSeries) {
  const window = recurringDateWindow(series);
  if (!window) return "Unknown";
  return window.earliest === window.latest ? `Around ${shortDate(series.next_expected)}` : `${shortDate(window.earliest)} – ${shortDate(window.latest)}`;
}

function EstimateEvidence({ series, today }: { series: RecurringSeries; today: string }) {
  const hasRange = Number.isFinite(series.amount_min) && Number.isFinite(series.amount_max);
  return <div className="space-y-2 rounded-lg bg-surface2 p-3 text-xs leading-relaxed text-mist">
    {isForecastEligible(series) && series.next_expected && <p>
      <span className="font-medium text-foam">{recurringWaitingForPayment(series, today) ? "Checking for payment" : "Estimated payment window"}: {expectedTiming(series)}.</span>
      {" Based on recorded timing, not a bank-supplied due date."}
      {series.schedule_basis && ` ${series.schedule_basis}`}
    </p>}
    {series.schedule_evidence_count != null && series.schedule_evidence_count < series.n_occurrences && <p>Timing is supported by {series.schedule_evidence_count} of {series.n_occurrences} payment dates{series.schedule_start ? `, from ${shortDate(series.schedule_start)}` : ""}. Other recorded dates remain in the payment history.</p>}
    {series.amount_basis && <p>Amount estimate: {series.amount_basis}</p>}
    {hasRange && <p>Observed amounts supporting this estimate: {money(series.amount_min!)} – {money(series.amount_max!)}. This is the recorded range, not a guaranteed range for the next payment.</p>}
    {!!series.price_change_evidence && <p>{series.price_change_confirmed ? "Confirmed amount change" : "Amount comparison"}{series.price_change_confirmed && series.price_change_percent != null ? ` (${series.price_change_percent > 0 ? "+" : ""}${series.price_change_percent.toFixed(1)}%)` : ""}: supported by {series.price_change_evidence} recent {series.price_change_evidence === 1 ? "payment" : "payments"}.</p>}
    {!!series.missed_cycles && <p>{series.missed_cycles} expected {series.missed_cycles === 1 ? "cycle has" : "cycles have"} no matching payment in the recorded activity. This does not confirm cancellation.</p>}
    {series.merchant_labels && series.merchant_labels.length > 1 && <p>Statement labels grouped here: {series.merchant_labels.join(" · ")}. Original labels remain in the payment history.</p>}
    {series.forecast_reason && <p>{series.forecast_reason}</p>}
  </div>;
}

function NumberField({ label, value, min, max, onChange }: {
  label: string; value: number; min: number; max: number; onChange: (v: number) => void;
}) {
  return (
    <div role="group" aria-label={`${label} tolerance`} className="flex flex-col gap-1">
      <span className="text-xs uppercase tracking-wider text-mist">{label}</span>
      <div className="flex items-center overflow-hidden rounded-lg border border-line/70 bg-deep/70">
        <button aria-label={`Decrease ${label.toLowerCase()} tolerance`} disabled={value <= min} className="min-h-11 w-9 shrink-0 text-mist hover:text-foam"
          onClick={() => onChange(Math.max(min, value - 1))}>−</button>
        <span className="min-w-[2.2rem] flex-1 text-center text-sm tabular-nums text-foam">±{value}</span>
        <button aria-label={`Increase ${label.toLowerCase()} tolerance`} disabled={value >= max} className="min-h-11 w-9 shrink-0 text-mist hover:text-foam"
          onClick={() => onChange(Math.min(max, value + 1))}>+</button>
      </div>
    </div>
  );
}

function Settings({ cfg, set, reset }: {
  cfg: RecurringConfig; set: (patch: Partial<RecurringConfig>) => void; reset: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-mist">Preview cadence windows &amp; price sensitivity</p>
        <button className="btn" onClick={reset}>Reset</button>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wider text-mist">Min occurrences</span>
          <input type="number" min={2} max={24} className="input min-h-11 py-1" value={cfg.min_occurrences}
            onChange={(e) => set({ min_occurrences: Math.max(2, Math.min(24, +e.target.value || 2)) })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wider text-mist">Regularity</span>
          <select className="input min-h-11 py-1" value={cfg.regularity}
            onChange={(e) => set({ regularity: +e.target.value })}>
            <option value={0.35}>Loose (35%)</option>
            <option value={0.5}>Normal (50%)</option>
            <option value={0.7}>Strict (70%)</option>
            <option value={0.85}>Very strict (85%)</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wider text-mist">Price-change threshold</span>
          <select className="input min-h-11 py-1" value={cfg.amount_tol}
            onChange={(e) => set({ amount_tol: +e.target.value })}>
            <option value={0.05}>±5%</option>
            <option value={0.10}>±10%</option>
            <option value={0.20}>±20%</option>
            <option value={0.30}>±30%</option>
            <option value={0.50}>±50%</option>
          </select>
        </label>
      </div>

      <div>
        <div className="mb-2 text-xs uppercase tracking-wider text-mist">Cadence tolerance (± days)</div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {CADENCE_FIELDS.map((f) => (
            <NumberField key={f.key} label={f.label} value={cfg[f.key] as number} min={0} max={f.max}
              onChange={(v) => set({ [f.key]: v } as Partial<RecurringConfig>)} />
          ))}
        </div>
      </div>
    </div>
  );
}

export default function Recurring({ transactionId = null, initialSearch }: { transactionId?: number | null; initialSearch?: string }) {
  const sortId = useId();
  const [cfg, setCfg] = useWorkspaceState<RecurringConfig>("recurring:config", DEFAULTS);
  const [applied, setApplied] = useState<RecurringConfig>(cfg);
  const state = useAsync(signal => api.recurringOverview(applied, signal), [applied]);
  const { data, error } = state;
  const items = data?.patterns;
  const compact = useCompactLayout("(max-width: 1023px)");
  const [search, setSearch] = useWorkspaceState("recurring:search", initialSearch || "");
  useEffect(() => { if (initialSearch !== undefined) { setSearch(initialSearch); setFilter("all"); setPatternType("all"); setPage(0); } }, [initialSearch]);
  const [filter, setFilter] = useWorkspaceState("recurring:filter", "all");
  const [patternType, setPatternType] = useWorkspaceState("recurring:pattern-type", "regular_payment");
  const [page, setPage] = useWorkspaceState("recurring:page", 0);
  const recordsRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [candidateLimit, setCandidateLimit] = useWorkspaceState("recurring:candidate-limit", 12);
  const filterKey = JSON.stringify([search, filter, patternType]);
  const previousFilters = useRef(filterKey);
  useEffect(() => {
    if (previousFilters.current !== filterKey) { previousFilters.current = filterKey; setPage(0); }
  }, [filterKey, setPage]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [reviewId, setReviewId] = useState<number | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const historyTriggerRef = useRef<HTMLElement | null>(null);
  const selection = data && [...data.patterns, ...data.candidates].find(r => r.key === selectedKey);
  const linked = data && transactionId !== null ? recurringForPayment(data, transactionId) : undefined;
  useEffect(() => {
    if (linked) setSelectedKey(linked.key);
  }, [transactionId, linked?.key]);
  function showPayments(key: string) {
    historyTriggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelectedKey(key);
    requestAnimationFrame(() => {
      detailRef.current?.focus({ preventScroll: true });
      detailRef.current?.scrollIntoView({ behavior: "auto", block: "start" });
    });
  }
  function closePayments() {
    setSelectedKey(null);
    requestAnimationFrame(() => {
      if (historyTriggerRef.current?.isConnected) historyTriggerRef.current.focus();
    });
  }
  const loading = state.loading || cfg !== applied;

  // debounce config changes so stepper clicks don't spam the backend
  useEffect(() => {
    const id = setTimeout(() => setApplied(cfg), 250);
    return () => clearTimeout(id);
  }, [cfg]);

  const set = (patch: Partial<RecurringConfig>) => setCfg((c) => ({ ...c, ...patch }));
  const totals = currentRecurringTotals(items ?? []);

  const [sort, setSort] = useWorkspaceState<{ key: SortKey; dir: "asc" | "desc" }>("recurring:sort", {
    key: "monthly_cost", dir: "desc",
  });
  const previousSort = useRef(sort);
  useEffect(() => { if (previousSort.current !== sort) { previousSort.current = sort; setPage(0); } }, [sort, setPage]);
  function changePage(next: number) {
    setPage(next);
    requestAnimationFrame(() => {
      recordsRef.current?.focus({ preventScroll: true });
      recordsRef.current?.scrollIntoView({ block: "start" });
    });
  }
  function clearFilters() {
    setSearch("");
    setFilter("all");
    setPage(0);
    searchRef.current?.focus();
  }
  const normalizedSearch = search.trim().toLowerCase();
  const hasFilters = !!normalizedSearch || filter !== "all";
  const sortBy = (key: SortKey) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === "desc" ? "asc" : "desc" }));
  const sorted = useMemo(() => {
    const arr = (items ?? []).filter(series => {
      if (patternType !== "all" && (series.pattern_type || "regular_payment") !== patternType) return false;
      if (normalizedSearch && !`${series.merchant} ${(series.merchant_labels ?? []).join(" ")} ${series.account} ${series.category}`.toLowerCase().includes(normalizedSearch)) return false;
      if (filter === "current") return !series.data_stale && series.status !== "ended";
      if (filter === "changes") return series.status === "price_change" && !series.data_stale;
      if (filter === "stale") return series.data_stale;
      if (filter === "ended") return series.status === "ended";
      return true;
    });
    arr.sort((a, b) => {
      const cmp = STRING_KEYS.includes(sort.key)
        ? String(a[sort.key] ?? "").localeCompare(String(b[sort.key] ?? ""))
        : sort.key === "monthly_cost" ? recurringMonthlyAmount(a) - recurringMonthlyAmount(b)
        : (a[sort.key] as number) - (b[sort.key] as number);
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [items, sort, normalizedSearch, filter, patternType]);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(sorted.length / 18) - 1));
  const displayed = sorted.slice(currentPage * 18, (currentPage + 1) * 18);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const upcoming = upcomingRecurring(items ?? [], today);
  const statusText = (series: NonNullable<typeof items>[number]) => series.status === "ended" ? "No recent match" : series.data_stale ? "Older evidence" : series.status === "price_change" ? "Price change" : "Current pattern";

  return (
    <div className="space-y-4">
      {reviewId !== null && <TransactionReview id={reviewId} onClose={() => setReviewId(null)} onSaved={state.refetch} />}
      <div><Heading level={2} variant="intro" >Your regular payments</Heading><p className="mt-1.5 text-sm text-mist">See changing amounts and the next dates supported by your saved activity.</p></div>
      <dl className="grid grid-cols-2 gap-4 rounded-xl border border-line bg-white p-4" aria-busy={loading}><div><dt className="text-xs text-mist">Current regular payments</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{items ? totals.current : "…"}</dd><p className="mt-1 text-xs text-mist">{totals.priceChanges} with an amount change</p></div><div><dt className="text-xs text-mist">Estimated monthly cost</dt><dd className="mt-1 text-2xl font-semibold tabular-nums text-coral">{money(items ? totals.monthly : null)}</dd><p className="mt-1 text-xs text-mist">Regular, recent payment evidence only</p></div></dl>
      <div className="flex flex-wrap gap-1 rounded-xl border border-line bg-white p-1" role="group" aria-label="Pattern types">{[{id:"regular_payment",label:"Regular payments"},{id:"variable_cost",label:"Variable costs"},{id:"spending_habit",label:"Spending habits"},{id:"all",label:"All history"}].map(type => <button key={type.id} className={`min-h-11 flex-1 rounded-lg px-2 py-2 text-xs font-medium ${patternType === type.id ? "bg-surface2 text-foam" : "text-mist hover:text-teal"}`} aria-pressed={patternType === type.id} onClick={() => setPatternType(type.id)}>{type.label}<span className="ml-1 tabular-nums text-mist">{(items || []).filter(series => type.id === "all" || (series.pattern_type || "regular_payment") === type.id).length}</span></button>)}</div>
      <p className="text-xs leading-relaxed text-mist">{patternType === "spending_habit" ? "Repeated shopping or spending is historical behavior. It does not establish a future bill." : patternType === "variable_cost" ? "Variable fees and costs remain inspectable but do not count toward expected payment dates or monthly commitments." : "Open a payment to inspect its dates and amounts. Older records, variable costs and spending habits are excluded from upcoming payment estimates."}</p>
      {upcoming.length > 0 && <Card className="p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><Heading level={3} variant="subsection">Expected in the next 30 days</Heading><p className="mt-1 text-xs text-mist">Pattern estimates from recent activity, including payment windows still open today. Banks have not supplied bill due dates.</p></div><Pill tone="mute">{upcoming.length} patterns</Pill></div><div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{upcoming.slice(0, compact ? 3 : 6).map(series => <button key={series.key} onClick={() => showPayments(series.key)} className="rounded-xl border border-line bg-deep/50 p-3 text-left transition hover:border-teal/40 hover:bg-white" aria-label={`View upcoming ${series.merchant} payment`}><span className="flex items-center justify-between gap-3 text-xs"><span className="font-medium text-teal">{expectedTiming(series)}</span><span className="font-semibold tabular-nums text-foam">{money(series.current_amount)}</span></span>{recurringWaitingForPayment(series, today) && <span className="mt-1 block text-xs text-amber">Checking for payment</span>}<span className="mt-2 block truncate text-sm font-medium">{series.merchant}</span><span className="mt-1 block truncate text-xs text-mist">{series.account}</span></button>)}</div>{upcoming.length > (compact ? 3 : 6) && <p className="mt-3 text-xs text-mist">Showing the nearest expected dates. Search the payment history below for more.</p>}</Card>}
      <RefreshStatus loading={!!items && loading} error={error} retry={state.refetch} retaining={!!items} />
      {data && transactionId !== null && !linked && !loading && <Card>
        <p className="text-sm text-mist">This payment is no longer part of a detected pattern or a subscription awaiting evidence. Its classification may have changed.</p>
        <button className="btn mt-2" onClick={() => setReviewId(transactionId)}>Review payment</button>
      </Card>}
      {selectedKey && <div ref={detailRef} role="region" aria-label="Payment history" tabIndex={-1} className="scroll-mt-24">
        <Card className="space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <Heading level={2} variant="section">{selection?.merchant ?? "Payment history"}</Heading>
              {selection && <p className="text-sm text-mist">{selection.account} · {selection.category}</p>}
            </div>
            <button className="btn" onClick={closePayments}>Close history</button>
          </div>
          {!selection ? <p className="text-sm text-mist">This series no longer matches the current settings or classifications.</p> : <>
            {"reason" in selection ? <p className="text-sm text-mist">{selection.reason} No frequency or monthly cost has been assumed.</p> :
              <p className="text-sm text-mist">{selection.cadence} · Typical {money(selection.typical_amount)} · Recent {money(selection.current_amount)}.
                {selection.next_expected && isForecastEligible(selection) && ` Estimated timing: ${expectedTiming(selection)}.`}
                {selection.data_stale && " Recent activity is unknown; refresh the bank connection to check."}
                {selection.status === "ended" && " No expected payment found in later account records; this does not confirm cancellation."}
              </p>}
            {!("reason" in selection) && <EstimateEvidence series={selection} today={today} />}
            <p className="text-xs text-mist">{selection.payments.length} payments on {selection.n_occurrences} dates. Separate charges on the same day count as one observation. Refunds and excluded transfers do not establish a payment schedule.</p>
            <div className="overflow-x-auto">
              <DataTable className="w-full text-sm">
                <thead><tr className="text-left text-xs text-mist"><th className="p-2">Date</th><th className="p-2">Original statement label</th><th className="p-2 text-right">Recorded amount</th><th className="p-2">Action</th></tr></thead>
                <tbody>{selection.payments.map(payment => <tr key={payment.id} className={`border-t border-line ${payment.id === transactionId ? "bg-teal/10" : ""}`}>
                  <td className="whitespace-nowrap p-2">{shortDate(payment.txn_date)}</td>
                  <td className="min-w-40 p-2">{payment.description}<span className="block text-xs text-mist">{payment.category}</span>
                    {payment.analysis_amount != null && <span className="mt-1 block text-xs text-mist">Included in this pattern: {money(Math.abs(payment.analysis_amount))} · {payment.analysis_category}{payment.analysis_component ? ` · ${payment.analysis_component}` : ""}.</span>}
                  </td>
                  <td className="whitespace-nowrap p-2 text-right tabular-nums">{money(payment.amount)}</td>
                  <td className="p-2"><button className="btn" onClick={() => setReviewId(payment.id)} aria-label={`Review payment on ${payment.txn_date}`}>Review</button></td>
                </tr>)}</tbody>
              </DataTable>
            </div>
          </>}
        </Card>
      </div>}
      {!items ? (!error && <Spinner />) : (
        <Card className="overflow-hidden p-0">
          <div className="grid gap-3 border-b border-line p-4 sm:grid-cols-2 xl:grid-cols-[minmax(220px,1fr)_auto_auto]"><label className="field-label">Find a payment<input ref={searchRef} className="input w-full" type="search" placeholder="Merchant, account or category" value={search} onChange={event => setSearch(event.target.value)} /></label><MobileDisclosure className="sm:contents" label="Payment filters & sort" summary={filter === "all" ? "All patterns" : filter} contentClassName="grid gap-3 sm:contents"><label className="field-label">Evidence<select className="input" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All patterns</option><option value="current">Current patterns</option><option value="changes">Price changes</option><option value="stale">Older evidence</option><option value="ended">No recent match</option></select></label><div className="field-label"><label htmlFor={sortId}>Sort by</label><div className="flex gap-2"><select id={sortId} className="input min-w-0 flex-1" value={sort.key} onChange={event => { const option = SORT_OPTIONS.find(item => item.key === event.target.value)!; setSort({ key: option.key, dir: option.defaultDir }); setPage(0); }}>{SORT_OPTIONS.map(option => <option key={option.key} value={option.key}>{option.label}</option>)}</select><button className="btn" aria-label="Reverse recurring sort order" onClick={() => setSort(previous => ({ ...previous, dir: previous.dir === "asc" ? "desc" : "asc" }))}>{sort.dir === "asc" ? "↑" : "↓"}</button></div></div></MobileDisclosure></div>
          <div ref={recordsRef} role="region" aria-label="Recurring payment records" tabIndex={-1} className="data-table-region">
          {compact ? <div className="divide-y divide-line">{displayed.map(series => <button key={series.key} className="block w-full px-4 py-3 text-left hover:bg-deep/50" onClick={() => showPayments(series.key)} aria-label={`View payments for ${series.merchant} in ${series.account}`}><span className="flex items-start justify-between gap-3"><span className="min-w-0 flex-1"><span className="block break-words text-sm font-semibold text-teal">{series.merchant}</span><span className="mt-1 block text-xs text-mist">{series.account}</span></span><span className="shrink-0 text-right"><span className="block text-sm font-semibold tabular-nums">{money(recurringMonthlyAmount(series))}</span><span className="text-xs text-mist">{isForecastEligible(series) ? "estimated / month" : "observed rate / month"}</span></span></span><span className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-mist"><Pill tone={CADENCE_TONE[series.cadence] ?? "mute"}>{series.cadence}</Pill><span>{money(series.typical_amount)} typical</span><span>{statusText(series)}</span></span><span className="mt-1 block text-xs text-mist">{series.next_expected && isForecastEligible(series) ? `${recurringWaitingForPayment(series, today) ? "Checking for payment" : "Expected"}: ${expectedTiming(series)}` : `Last recorded ${shortDate(series.last_seen)}`} · {series.n_occurrences} payment dates</span></button>)}</div> : <div className="overflow-x-auto"><DataTable className="w-full text-sm"><thead className="bg-surface2/80 text-left text-xs text-mist"><tr><Th label="Merchant & account" k="merchant" sort={sort} onSort={sortBy} /><th className="px-3 py-2.5 font-medium">Frequency</th><Th label="Typical payment" k="typical_amount" sort={sort} onSort={sortBy} right /><Th label={patternType === "regular_payment" ? "Monthly estimate" : "Observed monthly rate"} k="monthly_cost" sort={sort} onSort={sortBy} right /><th className="px-3 py-2.5 font-medium">Next expected</th><th className="px-3 py-2.5 font-medium">Evidence</th></tr></thead><tbody>{displayed.map(series => <tr key={series.key} className="border-t border-line/60 hover:bg-deep/50"><td className="max-w-64 px-3 py-3"><button className="break-words text-left font-medium text-teal underline decoration-teal/25 underline-offset-4" onClick={() => showPayments(series.key)} aria-label={`View payments for ${series.merchant} in ${series.account}`}>{series.merchant}</button><TableMeta>{series.account} · {series.category}</TableMeta></td><td className="px-3 py-3"><Pill tone={CADENCE_TONE[series.cadence] ?? "mute"}>{series.cadence}</Pill><TableMeta>{series.n_occurrences} dates</TableMeta></td><td className="whitespace-nowrap px-3 py-3 text-right tabular-nums">{money(series.typical_amount)}</td><td className="whitespace-nowrap px-3 py-3 text-right font-medium tabular-nums">{money(recurringMonthlyAmount(series))}<TableMeta>{isForecastEligible(series) ? "estimated" : "observed rate"}</TableMeta></td><td className="whitespace-nowrap px-3 py-3 text-xs text-mist">{series.next_expected && isForecastEligible(series) ? <>{expectedTiming(series)}{recurringWaitingForPayment(series, today) && <TableMeta tone="warning">Checking for payment</TableMeta>}</> : "—"}</td><td className="px-3 py-3 text-xs"><Pill tone={series.status === "price_change" || series.data_stale ? "amber" : series.status === "ended" ? "mute" : "kelp"}>{statusText(series)}</Pill><TableMeta>Last {shortDate(series.last_seen)}</TableMeta></td></tr>)}</tbody></DataTable></div>}
          {!sorted.length && <div className="px-4 py-6 text-center text-sm text-mist">
            <p>{hasFilters ? "No patterns match these filters." : "There are not enough regular payments to establish a pattern at these settings."}</p>
            {hasFilters && <button className="btn mt-3" onClick={clearFilters}>Clear payment filters</button>}
          </div>}
          </div>
          <PageControls total={sorted.length} page={currentPage} size={18} onPage={changePage} label="Recurring pattern pages" />
        </Card>
      )}
      <Disclosure title="Detection settings">
        <Settings cfg={cfg} set={set} reset={() => setCfg(DEFAULTS)} />
        <p className="mt-2 text-xs text-mist">Settings preview patterns in this view. Transaction badges and planning use the default settings.</p>
      </Disclosure>
      {data && <Card className="space-y-3">
        <SectionTitle hint={`${data.candidates.length} to check`}>Subscriptions awaiting timing evidence</SectionTitle>
        <p className="text-sm text-mist">These payments have a subscription category, but too few or irregular dates to establish a schedule. They are not included in the monthly estimate.</p>
        {data.candidates.length === 0 ? <p className="text-sm text-mist">No unmatched subscription labels at these settings.</p> :
          <ul className="divide-y divide-line">{data.candidates.slice(0, candidateLimit).map(candidate => <li key={candidate.key} className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div><span className="font-medium">{candidate.merchant}</span><p className="text-xs text-mist">{candidate.account} · {candidate.payments.length} payments · last {shortDate(candidate.last_seen)}</p></div>
            <button className="btn" onClick={() => showPayments(candidate.key)} aria-label={`View payments for ${candidate.merchant} in ${candidate.account}`}>View payments</button>
          </li>)}</ul>}
        {data.candidates.length > candidateLimit && <button className="btn" onClick={() => setCandidateLimit(value => value + 12)}>Show more candidates ({data.candidates.length - candidateLimit} remaining)</button>}
      </Card>}
    </div>
  );
}

function Th({ label, k, sort, onSort, right }: {
  label: string; k: SortKey;
  sort: { key: SortKey; dir: "asc" | "desc" }; onSort: (k: SortKey) => void; right?: boolean;
}) {
  const active = sort.key === k;
  return (
    <th aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={`cursor-pointer select-none px-3 py-2.5 font-medium hover:text-foam ${right ? "text-right" : ""}`}
    >
      <button onClick={() => onSort(k)}>
      {label}
      {active && <span className="ml-1 text-teal">{sort.dir === "desc" ? "▾" : "▴"}</span>}
      </button>
    </th>
  );
}
