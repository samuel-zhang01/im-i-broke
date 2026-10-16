import { useWorkspaceState } from "../workspaceState";
import Icon from "../components/Icon";
import MobileDisclosure from "../components/MobileDisclosure";
import { DataTable } from "../components/DataTable";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Txn, type TxnQuery } from "../api";
import { Card, Pill, Spinner, ErrorBox, RefreshStatus, useAsync, useCompactLayout } from "../components/ui";
import TransactionReview from "../components/TransactionReview";
import { money, shortDate } from "../format";
import { useCategoryOptions, CategoryOptionsError } from "../components/CategoryOptions";
import { categoryOptions } from "../categories";
import { validPageOffset } from "../pagination";
import { openRecurring } from "../recurring";
import { openBankAccount } from "../banking";
import { openFinancePage } from "../navigation";

const PAGE = 50;

export default function Transactions({ initialSearch }: { initialSearch?: string } = {}) {
  const accounts = useAsync(signal => api.summary(signal));
  const compact = useCompactLayout();
  const pageSize = compact ? 20 : PAGE;
  const [reviewId, setReviewId] = useState<number | null>(null);
  const records = useRef<HTMLDivElement>(null);
  const [rangesOpen, setRangesOpen] = useWorkspaceState("transactions:ranges-open", false);
  const [notice, setNotice] = useState<string | null>(null);
  const [q, setQ] = useWorkspaceState("transactions:search", initialSearch || "");
  const [qDebounced, setQDebounced] = useState(q);
  const [amounts, setAmounts] = useWorkspaceState("transactions:amounts", { min_amount: "", max_amount: "" });
  const [filters, setFilters] = useWorkspaceState<TxnQuery>("transactions:filters", {
    sort: "date", order: "desc", limit: pageSize, offset: 0,
  });
  useEffect(() => setFilters(current => current.limit === pageSize ? current : ({ ...current, limit: pageSize, offset: 0 })), [pageSize]);
  const categories = useCategoryOptions(filters.category);
  useEffect(() => { if (initialSearch !== undefined) { setQ(initialSearch); setQDebounced(initialSearch); setFilters({ sort: "date", order: "desc", limit: pageSize, offset: 0 }); setAmounts({ min_amount: "", max_amount: "" }); } }, [initialSearch]);
  // Only a changed search resets pagination; mounting must not undo a quick Next click.
  useEffect(() => {
    if (q === qDebounced) return;
    const id = setTimeout(() => {
      setQDebounced(q);
      setFilters((f) => f.offset === 0 ? f : ({ ...f, offset: 0 }));
    }, 300);
    return () => clearTimeout(id);
  }, [q, qDebounced]);

  const query: TxnQuery = useMemo(
    () => ({ ...filters, q: qDebounced || undefined,
      min_amount: amounts.min_amount === "" ? undefined : Number(amounts.min_amount),
      max_amount: amounts.max_amount === "" ? undefined : Number(amounts.max_amount) }),
    [filters, qDebounced, amounts]
  );

  const invalidDates = Boolean(filters.date_from && filters.date_to && filters.date_from > filters.date_to);
  const invalidAmounts = amounts.min_amount !== "" && amounts.max_amount !== "" && Number(amounts.min_amount) > Number(amounts.max_amount);
  const invalidFilters = invalidDates || invalidAmounts;
  const page = useAsync(signal => api.transactions(query, signal), [JSON.stringify(query)], { enabled: !invalidFilters });
  const updating = !invalidFilters && (page.loading || q !== qDebounced);

  function set<K extends keyof TxnQuery>(key: K, value: TxnQuery[K]) {
    setFilters((f) => ({ ...f, [key]: value, offset: 0 }));
  }
  function toggle(key: "anomaly" | "recurring" | "transfer") {
    setFilters((f) => ({ ...f, [key]: f[key] ? undefined : true, offset: 0 }));
  }
  function setAmount(key: "min_amount" | "max_amount", value: string) {
    setAmounts((current) => ({ ...current, [key]: value }));
    setFilters((current) => ({ ...current, offset: 0 }));
  }
  function sortBy(col: string) {
    setFilters((f) => ({
      ...f, sort: col,
      order: f.sort === col ? (f.order === "desc" ? "asc" : "desc") : ["merchant", "description", "category"].includes(col) ? "asc" : "desc",
      offset: 0,
    }));
  }
  function changePage(nextOffset: number) {
    setFilters(current => ({ ...current, offset: nextOffset }));
    requestAnimationFrame(() => {
      records.current?.focus({ preventScroll: true });
      records.current?.scrollIntoView({ block: "start" });
    });
  }

  const rangeCount = [filters.date_from, filters.date_to, amounts.min_amount, amounts.max_amount].filter(Boolean).length;
  const hasFilters = Boolean(q || rangeCount || filters.account || filters.category || filters.direction || filters.anomaly || filters.recurring || filters.transfer);
  function clearFilters() {
    setQ(""); setQDebounced(""); setAmounts({ min_amount: "", max_amount: "" });
    setFilters(current => ({ sort: current.sort, order: current.order, limit: pageSize, offset: 0 }));
  }
  const selectedAccount = accounts.data?.accounts.find(account => account.id === filters.account);
  const data = invalidFilters ? null : page.data;
  const total = data?.total ?? 0;
  const offset = filters.offset ?? 0;
  useEffect(() => {
    if (!data || page.loading || data.offset !== offset) return;
    const next = validPageOffset(data.total, data.limit, offset);
    if (next !== offset) setFilters(current => ({ ...current, offset: next }));
  }, [data, page.loading, offset]);

  async function changeCategory(id: number, category: string) {
    await api.setCategory(id, category);
    // Requery the active filter: an edited row can leave the category filter,
    // change sorting or alter its intelligence flags and aggregate totals.
    setFilters((f) => ({ ...f, offset: 0 }));
    setNotice(`Category saved: ${category}. All affected totals update automatically.`);
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-mist">Saved activity in GBP. Find other currencies in Workspace reports and bank feeds in Banks &amp; accounts.</p>
      {selectedAccount?.currency && selectedAccount.currency !== "GBP" && selectedAccount.account_type !== "investment" && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-seablue/25 bg-seablue/5 p-3"><p className="text-xs leading-relaxed text-mist">This list only shows GBP. Find imported and connected {selectedAccount.currency} activity under “Amounts and conversion evidence” in Workspace reports. Original amounts remain available even when an exchange rate is missing.</p><div className="flex flex-wrap gap-2"><button className="btn" onClick={() => openFinancePage("settings", undefined, "reports")}>Open workspace reports →</button><button className="btn btn-quiet" onClick={() => openBankAccount(selectedAccount.id)}>Open account activity →</button></div></div>}
      {notice && <p role="status" className="text-sm text-kelp">{notice}</p>}
      {reviewId !== null && <TransactionReview id={reviewId} onClose={() => setReviewId(null)} onSaved={message => { setNotice(message || "Review saved. Totals updated."); }} />}
      <CategoryOptionsError state={categories} />
      {accounts.error && <ErrorBox error={accounts.error} retry={accounts.refetch} />}
      <Card className="transaction-toolbar space-y-3 p-3">
        <div className="relative min-w-0">
          <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-mist" />
          <input
            className="input w-full pl-9"
            placeholder="Search description or merchant…"
            aria-label="Search transactions"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
<MobileDisclosure className="transaction-filter-disclosure" contentClassName="transaction-filter-content space-y-3" label="Transaction filters" summary={invalidDates || invalidAmounts ? "Check date or amount filters" : hasFilters ? `${[filters.account, filters.category, filters.direction, filters.anomaly, filters.recurring, filters.transfer, ...[filters.date_from, filters.date_to, amounts.min_amount, amounts.max_amount]].filter(Boolean).length} active · ${selectedAccount?.display_name || "All accounts"}` : "All accounts · all activity"}>
        <div className="transaction-selectors grid grid-cols-2 gap-2 sm:grid-cols-3">
          <select aria-label="Account" className="input min-w-0" value={filters.account ?? ""}
            onChange={(e) => set("account", e.target.value || undefined)}>
            <option value="">All accounts</option>
            {(accounts.data?.accounts ?? []).map((a) => <option key={a.id} value={a.id}>{a.display_name}{a.currency && a.currency !== "GBP" ? ` · ${a.currency}` : ""}</option>)}
          </select>
          <select aria-label="Category" className="input min-w-0" value={filters.category ?? ""}
            onChange={(e) => set("category", e.target.value || undefined)}>
            <option value="">All categories</option>
            {categories.options.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select aria-label="Money direction" className="input col-span-2 min-w-0 sm:col-span-1" value={filters.direction ?? ""}
            onChange={(e) => set("direction", (e.target.value || undefined) as TxnQuery["direction"])}>
            <option value="">In &amp; out</option>
            <option value="in">Money in</option>
            <option value="out">Money out</option>
          </select>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FilterChip on={!!filters.anomaly} onClick={() => toggle("anomaly")} tone="amber">Anomalies</FilterChip>
          <FilterChip on={!!filters.recurring} onClick={() => toggle("recurring")} tone="cyan">Recurring</FilterChip>
          <FilterChip on={!!filters.transfer} onClick={() => toggle("transfer")} tone="blue">Transfers</FilterChip>
          <button type="button" className={`btn text-xs ${rangeCount ? "border-teal text-teal" : ""}`} aria-expanded={rangesOpen} aria-controls="transaction-ranges" onClick={() => setRangesOpen(open => !open)}>Date &amp; amount{rangeCount ? ` (${rangeCount})` : ""} <span aria-hidden="true">{rangesOpen ? "▴" : "▾"}</span></button>
          {hasFilters && <button type="button" className="transaction-clear min-h-11 px-2 text-xs text-mist underline underline-offset-4" onClick={clearFilters}>Clear filters</button>}
        </div>
        {rangeCount > 0 && !rangesOpen && <div className="flex flex-wrap gap-2" aria-label="Active date and amount filters">
          {filters.date_from && <button className="btn text-xs" aria-label="Remove start date filter" onClick={() => set("date_from", undefined)}>From {shortDate(filters.date_from)} ×</button>}
          {filters.date_to && <button className="btn text-xs" aria-label="Remove end date filter" onClick={() => set("date_to", undefined)}>To {shortDate(filters.date_to)} ×</button>}
          {amounts.min_amount && <button className="btn text-xs" aria-label="Remove minimum amount filter" onClick={() => setAmount("min_amount", "")}>Min {money(Number(amounts.min_amount))} ×</button>}
          {amounts.max_amount && <button className="btn text-xs" aria-label="Remove maximum amount filter" onClick={() => setAmount("max_amount", "")}>Max {money(Number(amounts.max_amount))} ×</button>}
        </div>}
        {rangesOpen && <div id="transaction-ranges" className="space-y-3 border-t border-line pt-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className="field-label">From date
            <input type="date" aria-label="From date" aria-invalid={invalidDates || undefined} aria-describedby={invalidDates ? "transaction-date-error" : undefined} max={filters.date_to} className="input w-full" value={filters.date_from ?? ""}
              onChange={(e) => set("date_from", e.target.value || undefined)} />
          </label>
          <label className="field-label">To date
            <input type="date" aria-label="To date" aria-invalid={invalidDates || undefined} aria-describedby={invalidDates ? "transaction-date-error" : undefined} min={filters.date_from} className="input w-full" value={filters.date_to ?? ""}
              onChange={(e) => set("date_to", e.target.value || undefined)} />
          </label>
          <label className="field-label">Min amount (£)
            <input type="number" step="0.01" aria-invalid={invalidAmounts || undefined} aria-describedby={invalidAmounts ? "transaction-amount-error" : undefined} className="input w-full" value={amounts.min_amount}
              onChange={(e) => setAmount("min_amount", e.target.value)} />
          </label>
          <label className="field-label">Max amount (£)
            <input type="number" step="0.01" aria-invalid={invalidAmounts || undefined} aria-describedby={invalidAmounts ? "transaction-amount-error" : undefined} className="input w-full" value={amounts.max_amount}
              onChange={(e) => setAmount("max_amount", e.target.value)} />
          </label>
        </div>
        <p className="text-xs text-mist">Use negative amounts for money out: −50 to −10 finds spending of £10–£50.</p>
        </div>}
        {invalidDates && <p id="transaction-date-error" role="alert" className="text-sm text-coral">Choose an end date on or after the start date.</p>}
        {invalidAmounts && <p id="transaction-amount-error" role="alert" className="text-sm text-coral">The maximum amount must be at least the minimum amount.</p>}

      </MobileDisclosure></Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div aria-busy={updating} className="text-sm text-mist tabular-nums">
          {data && <><span className="font-medium text-foam">{total.toLocaleString()} transactions</span><span className="mx-2" aria-hidden="true">·</span><span className="text-kelp">{money(data.sum_in)}</span> in · <span className="text-coral">{money(Math.abs(data.sum_out))}</span> out</>}
        </div>
        <label className="flex min-w-0 items-center gap-2 text-xs text-mist">Sort by
          <select aria-label="Sort transactions" className="input min-w-0" value={`${filters.sort}:${filters.order}`} onChange={event => {
            const [sort, order] = event.target.value.split(":");
            setFilters(current => ({ ...current, sort, order: order as "asc" | "desc", offset: 0 }));
          }}>
            <option value="date:desc">Newest first</option><option value="date:asc">Oldest first</option>
            <option value="amount:asc">Amount: low to high</option><option value="amount:desc">Amount: high to low</option>
            <option value="merchant:asc">Name: A–Z</option><option value="merchant:desc">Name: Z–A</option>
            <option value="description:asc">Bank description: A–Z</option><option value="description:desc">Bank description: Z–A</option>
            <option value="category:asc">Category: A–Z</option><option value="category:desc">Category: Z–A</option>
          </select>
        </label>
      </div>

      <Card ref={records} role="region" aria-label="Transaction records" tabIndex={0} className="data-table-region overflow-hidden !p-0">
        {!invalidFilters && <div className="px-4"><RefreshStatus loading={!!data && updating} error={page.error} retry={page.refetch} retaining={!!data} /></div>}
        {invalidFilters ? <p className="p-6 text-center text-sm text-mist">Correct the filters above to see matching transactions.</p> : !data ? (
          !page.error && <Spinner />
        ) : (
          compact ? <div aria-busy={updating} className="content-enter">

            <div className="divide-y divide-line">{data.items.map(t => <article key={t.id} className="px-3 py-3"><button className="flex w-full flex-wrap items-start justify-between gap-3 text-left" aria-label={`Review ${t.merchant || t.description}`} onClick={() => setReviewId(t.id)}><span className="min-w-0 flex-1 basis-36"><span className="block break-words text-sm font-medium">{t.merchant || t.description}</span><span className="mt-1 block text-xs text-mist">{shortDate(t.txn_date)} · {t.account}</span></span><span className={`ml-auto shrink-0 text-sm font-semibold tabular-nums ${t.amount < 0 ? "text-coral" : "text-kelp"}`}>{money(t.amount)}</span></button><div className="mt-2 flex flex-wrap items-center gap-2 text-xs"><Pill tone="mute">{t.category || "Uncategorized"}</Pill>{(t.transfer_group || t.spending_treatment === "self_transfer") && <Pill tone="blue">Own transfer</Pill>}{t.is_anomaly ? <Pill tone="amber">Check</Pill> : null}{(t.recurring_id || (t.category === "Subscriptions" && t.amount < 0 && !t.exclude_from_analysis && t.spending_treatment !== "self_transfer")) && <button className="min-h-11 rounded px-1 text-teal underline" aria-label={`View ${t.merchant || t.description} in Recurring`} onClick={() => openRecurring(t.id)}>Payment pattern ↗</button>}</div></article>)}</div>{!data.items.length && <p className="p-8 text-center text-sm text-mist">No matching transactions.</p>}
          </div> : <div className="overflow-x-auto" aria-busy={updating}>
            <DataTable className="w-full text-sm">
              <thead className="sticky top-0 bg-surface2/90 text-left text-xs uppercase tracking-wider text-mist backdrop-blur">
                <tr>
                  <Th label="Date" col="date" filters={filters} onSort={sortBy} />
                  <th className="px-3 py-2.5 font-medium">Account</th>
                  <Th label="Name" col="merchant" filters={filters} onSort={sortBy} />
                  <Th label="Category" col="category" filters={filters} onSort={sortBy} />
                  <Th label="Amount" col="amount" filters={filters} onSort={sortBy} right />
                  <th className="px-3 py-2.5 font-medium">Review</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((t) => (
                  <Row key={t.id} t={t} categories={categories.options} onCategory={changeCategory} onReview={() => setReviewId(t.id)} />
                ))}
                {data.items.length === 0 && (
                  <tr><td colSpan={6} className="py-10 text-center text-mist">No matching transactions.</td></tr>
                )}
              </tbody>
            </DataTable>
          </div>
        )}
        {data && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line/60 px-4 py-2.5 text-sm text-mist">
            <span>
              {total === 0 ? 0 : data.offset + 1}–{Math.min(data.offset + data.limit, total)} of {total.toLocaleString()}
            </span>
            <div className="flex gap-2">
              <button className="btn disabled:opacity-40" disabled={updating || offset === 0}
                onClick={() => changePage(Math.max(0, offset - pageSize))}>
                ← Prev
              </button>
              <button className="btn disabled:opacity-40" disabled={updating || offset + pageSize >= total}
                onClick={() => changePage(offset + pageSize)}>
                Next →
              </button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}

function Th({ label, col, filters, onSort, right }: {
  label: string; col: string; filters: TxnQuery; onSort: (c: string) => void; right?: boolean;
}) {
  const active = filters.sort === col;
  return (
    <th aria-sort={active ? (filters.order === "asc" ? "ascending" : "descending") : "none"}
      className={`cursor-pointer select-none px-3 py-2.5 font-medium hover:text-foam ${right ? "text-right" : ""}`}
    >
      <button onClick={() => onSort(col)}>
      {label}
      {active && <span className="ml-1 text-teal">{filters.order === "desc" ? "▾" : "▴"}</span>}
      </button>
    </th>
  );
}

function Row({ t, categories, onCategory, onReview }: { t: Txn; categories: string[]; onCategory: (id: number, c: string) => Promise<void>; onReview: () => void }) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save(category: string) {
    setSaving(true);
    setError(null);
    try { await onCategory(t.id, category); setEditing(false); }
    catch (e) { setError(String(e)); }
    finally { setSaving(false); }
  }
  return (
    <tr className="border-t border-line/40 hover:bg-surface2/40">
      <td className="whitespace-nowrap px-3 py-2 text-mist tabular-nums">{shortDate(t.txn_date)}</td>
      <td className="whitespace-nowrap px-3 py-2 text-xs text-mist">{t.account}</td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-foam">{t.merchant || t.description}</span>
          {t.is_anomaly ? <Pill tone="amber">!</Pill> : null}
          {t.recurring_id || (t.category === "Subscriptions" && t.amount < 0 && !t.exclude_from_analysis && t.spending_treatment !== "self_transfer") ?
            <button className="rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal" onClick={() => openRecurring(t.id)}
              aria-label={`View ${t.merchant || t.description} in Recurring`}>
              <Pill tone="cyan">{t.recurring_id ? "↻ Recurring" : "Check recurring"}</Pill>
            </button> : null}
          {t.transfer_group || t.spending_treatment === "self_transfer" ? <Pill tone="blue">own transfer</Pill> : null}
          {t.merchant_kind === "person" && <Pill tone="mute">person</Pill>}
          {t.exclude_from_analysis ? <Pill tone="amber">excluded</Pill> : null}
        </div>
        {t.merchant && t.merchant !== t.description && (
          <div className="truncate text-xs text-mist">{t.description}</div>
        )}
      </td>
      <td className="px-3 py-2">
        {editing ? (
          <select
            autoFocus aria-label={`Category for ${t.merchant || t.description}`} disabled={saving} className="input py-1" defaultValue={t.category ?? "Uncategorized"}
            onBlur={() => setEditing(false)}
            onChange={(e) => void save(e.target.value)}
          >
            {categoryOptions(categories, [t.category]).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <button
            className="group inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs text-mist hover:bg-surface2 hover:text-foam"
            disabled={saving} onClick={() => setEditing(true)}
            title="Click to recategorize"
          >
            {t.category ?? "Uncategorized"}
            {t.category_source === "user" && <span className="text-teal">•</span>}
            <span className="opacity-0 group-hover:opacity-100">✎</span>
          </button>
        )}
        {error && <div role="alert" className="mt-1 max-w-56 text-xs text-coral">{error}</div>}
      </td>
      <td className={`whitespace-nowrap px-3 py-2 text-right font-medium tabular-nums ${t.amount < 0 ? "text-coral" : "text-kelp"}`}>
        {money(t.amount)}
      </td>
      <td className="px-3 py-2"><button className="btn btn-quiet text-xs" onClick={onReview} aria-label={`Review ${t.merchant || t.description}`}>Review</button></td>
    </tr>
  );
}

function FilterChip({ on, onClick, tone, children }: {
  on: boolean; onClick: () => void; tone: "amber" | "cyan" | "blue"; children: React.ReactNode;
}) {
  const tones = {
    amber: on ? "bg-amber/20 text-amber border-amber/40" : "",
    cyan: on ? "bg-cyan/20 text-cyan border-cyan/40" : "",
    blue: on ? "bg-seablue/20 text-seablue border-seablue/40" : "",
  };
  return (
    <button
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-lg border px-3 py-1.5 text-sm transition-colors ${on ? tones[tone] : "border-line/70 bg-surface2/40 text-mist hover:text-foam"}`}
    >
      {children}
    </button>
  );
}
