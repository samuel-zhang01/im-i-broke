import { Heading } from "../components/Heading";
import InstitutionLogo from "../components/InstitutionLogo";
import { AvailableCreditEvidence } from "../components/AvailableCreditEvidence";
import { BankBalanceRuleSettings } from "../components/BankBalanceRuleSettings";
import { DataTable } from "../components/DataTable";
import { Disclosure } from "../components/Disclosure";
import MobileDisclosure from "../components/MobileDisclosure";
import BankReviewQueue from "../components/BankReviewQueue";
import { useWorkspaceState } from "../workspaceState";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { api, type TxnPage } from "../api";
import { Card, Empty, ErrorBox, Pill, RefreshStatus, Spinner, useAsync } from "../components/ui";
import { LoadingRegion, PageSkeleton, SkeletonBlock, TableSkeleton } from "../components/Skeleton";
import { watchData } from "../dataEvents";
import { shortDate } from "../format";
import { openFinancePage } from "../navigation";
import { accountColor, chartStyle, C } from "../theme";
import { bankBalanceGeometry, bankBalanceTickLabels } from "../chartData";
import { accountBalance, bankBalanceTotals, bankRows, bankTimestamp, isOldBankData, nativeMoney, type BankRow } from "../banking";
import {
  fetchBanking, fetchBankAccount, mapBankAccount, syncBanking,
  type BankingData, type BankAccountDetail, type BankBalance, type BankTransaction, type BankMappingOptions,
} from "../bankingApi";

const BankConnectionHealth = lazy(() => import("../components/BankConnectionHealth"));

function RefreshIcon({ spinning = false }: { spinning?: boolean }) {
  return <svg aria-hidden="true" className={spinning ? "animate-spin" : ""} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M20 7a9 9 0 00-15-2L2 8m0-6v6h6M4 17a9 9 0 0015 2l3-3m0 6v-6h-6" /></svg>;
}
function AccountStatus({ row }: { row: BankRow }) {
  const bank = row.provider;
  if (!bank) return <Pill tone="mute">{row.local?.balance_source === "manual" ? "Manually tracked" : "Statement only"}</Pill>;
  if (bank.present === false) return <Pill tone="amber">Saved connection</Pill>;
  if (bank.last_error || /disconnect|expired|reauth|error|inactive/i.test(bank.status)) return <Pill tone="amber">Check connection</Pill>;
  if (!bank.account_id) return <Pill tone="amber">Link history</Pill>;
  if (isOldBankData(bank.last_synced_at)) return <Pill tone="amber">Update due</Pill>;
  return <Pill tone="kelp"><span aria-hidden="true" className="mr-1 h-1.5 w-1.5 rounded-full bg-kelp" />Connected</Pill>;
}
function accountValuation(row: BankRow) {
  return row.provider ? row.provider.valuation ?? row.provider.balance?.valuation : row.local?.valuation;
}
function accountBalanceLabel(row: BankRow) {
  if (!row.provider && row.local?.balance_source === "manual") return "Manual balance";
  if (row.provider?.requires_balance_direction) return "Unconfirmed bank balance";
  const valuation = accountValuation(row);
  if (valuation?.current_unknown) return "Current balance unknown";
  if (valuation?.basis === "available_credit") return "Credit limit − available credit";
  if (valuation?.estimated) return "Estimated booked balance";
  if (valuation?.reported_source === "statement") return "Statement balance";
  return row.provider ? accountBalance(row) === null ? "Balance unavailable" : "Bank balance" : "Statement balance";
}
function AccountButton({ row, onOpen }: { row: BankRow; onOpen: () => void }) {
  const balance = accountBalance(row);
  return <button onClick={onOpen} className="group flex w-full items-center gap-3 border-t border-line/60 px-4 py-3 text-left transition-colors hover:bg-deep/70 sm:px-5" aria-label={`Open ${row.name}, ${row.currency}`}>
    <span aria-hidden="true" className="h-8 w-1 shrink-0 rounded-full" style={{ background: accountColor(row.institution) }} />
    <span className="min-w-0 flex-1"><span className="block break-words text-sm font-medium text-foam group-hover:text-teal">{row.name}</span><span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-mist"><span>{row.currency}</span><span aria-hidden="true">·</span><span>{row.provider ? row.provider.account_id ? "Bank feed + saved history" : "Bank feed · history to link" : row.local?.balance_source === "manual" ? "Manual observation" : "Saved statements"}</span>{(row.provider?.counts.review ?? 0) > 0 && <span className="text-amber">{row.provider!.counts.review} to review</span>}</span></span>
    <span className="max-w-[45%] shrink-0 text-right"><span className="block text-base font-semibold tracking-tight tabular-nums">{accountValuation(row)?.current_unknown ? "Unknown" : nativeMoney(balance, row.currency)}</span><span className="mt-1 block text-xs text-mist">{accountBalanceLabel(row)}</span></span>
    <span aria-hidden="true" className="ml-1 text-mist/60 group-hover:text-teal">›</span>
  </button>;
}
function BankGroup({ name, rows, onBank, onAccount }: { name: string; rows: BankRow[]; onBank: () => void; onAccount: (row: BankRow) => void }) {
  const connected = rows.filter(r => r.provider).length;
  const attention = rows.filter(r => r.provider && (!r.provider.account_id || r.provider.last_error || r.provider.present === false || r.provider.counts.review > 0)).length;
  return <section className="card mb-4 break-inside-avoid overflow-hidden" aria-label={`${name} accounts`}>
    <button className="flex min-h-20 w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-deep/60 sm:px-5" onClick={onBank} aria-label={`Explore ${name}`}>
      <InstitutionLogo name={name} /><span className="min-w-0 flex-1"><span className="block text-base font-semibold">{name}</span><span className="mt-1 block text-xs text-mist">{rows.length} {rows.length === 1 ? "account" : "accounts"} · {connected ? `${connected} connected` : rows.every(row => row.local?.balance_source === "manual") ? "Manual balances" : "Statement history"}</span></span>
      {attention ? <Pill tone="amber">Needs attention</Pill> : connected ? <Pill tone="kelp">Connected</Pill> : <Pill tone="mute">Manual</Pill>}
    </button>
    {rows.map(row => <AccountButton key={row.key} row={row} onOpen={() => onAccount(row)} />)}
  </section>;
}
function Totals({ totals, className = "" }: { totals: Record<string, number>; className?: string }) {
  const values = Object.entries(totals).sort(([a], [b]) => a === "GBP" ? -1 : b === "GBP" ? 1 : a.localeCompare(b));
  return <div className={`grid grid-cols-2 gap-x-5 gap-y-4 sm:grid-cols-3 xl:grid-cols-4 ${className}`}>{values.map(([currency, amount]) => <div key={currency}><p className="text-xs font-semibold uppercase tracking-[.15em] opacity-65">{currency}</p><p className="mt-1 text-xl font-semibold tracking-tight tabular-nums sm:text-2xl">{nativeMoney(amount, currency)}</p></div>)}</div>;
}
function BalanceHistory({ points, currency, availableCredit = false }: { points: BankBalance[]; currency: string; availableCredit?: boolean }) {
  const history = bankBalanceGeometry(points.filter((point): point is BankBalance & { amount: number } => point.amount !== null), currency), ordered = history.points;
  if (!ordered.length) return <div className="flex h-full min-h-28 flex-col justify-center rounded-xl border border-dashed border-line bg-deep/50 p-4"><p className="text-xs font-medium text-foam">Your balance timeline starts here</p><p className="mt-2 text-xs leading-relaxed text-mist">Each successful refresh saves a balance observation in {currency}.</p></div>;
  const ticks = bankBalanceTickLabels(history.low!, history.high!);
  const axisWidth = Math.max(48, Math.max(ticks.low.length, ticks.high.length) * 6);
  const x = (value: number) => 6 + value * 448, y = (value: number) => 5 + value * 66;
  const line = ordered.map(point => `${x(point.x)},${y(point.y)}`).join(" ");
  return <figure className="min-w-0 rounded-xl border border-line bg-deep/30 p-3">
    <figcaption className="flex flex-wrap items-center justify-between gap-2 text-xs"><span className="font-medium">{availableCredit ? "Available credit history" : "Provider-reported balance history"}</span><span className="text-mist">{currency} · {ordered.length} {ordered.length === 1 ? "observation" : "observations"}</span></figcaption>
    <div className="mt-2 flex h-[76px] gap-2">
      <div aria-hidden="true" className="flex shrink-0 flex-col justify-between py-0.5 text-right text-xs tabular-nums text-mist" style={{ width: axisWidth }}><span>{ticks.high}</span><span>{ticks.low}</span></div>
      <svg role="img" aria-label={`Saved ${currency} balances, placed by actual elapsed fetch time. ${ordered.length} observations from ${bankTimestamp(ordered[0].fetched_at)} to ${bankTimestamp(ordered[ordered.length - 1].fetched_at)}. Minimum ${nativeMoney(history.minimum, currency)}, maximum ${nativeMoney(history.maximum, currency)}. Lines connect saved observations; values between fetches are unknown. Exact values are available below.`} viewBox="0 0 460 76" className="min-w-0 flex-1" preserveAspectRatio="none">
        <path d="M6 5H454M6 71H454" fill="none" stroke={chartStyle.gridLineColor} vectorEffect="non-scaling-stroke" />
        {history.zeroY !== null && <path d={`M6 ${y(history.zeroY)}H454`} fill="none" stroke={chartStyle.zeroLineColor} vectorEffect="non-scaling-stroke" />}
        {ordered.length > 1 && <polyline fill="none" stroke={C.kelp} strokeWidth={chartStyle.lineWidth} strokeLinejoin="round" strokeLinecap="round" points={line} vectorEffect="non-scaling-stroke" />}
        {ordered.map(point => <circle key={point.timestamp} cx={x(point.x)} cy={y(point.y)} r={chartStyle.markerSize / 2} fill={C.kelp}><title>{new Date(point.timestamp).toISOString()} · {nativeMoney(point.amount, currency)}</title></circle>)}
      </svg>
    </div>
    <div className="mt-1 flex justify-between gap-3 text-xs text-mist" style={{ paddingLeft: axisWidth + 8 }}><span>{bankTimestamp(ordered[0].fetched_at)}</span>{ordered.length > 1 && <span className="text-right">{bankTimestamp(ordered[ordered.length - 1].fetched_at)}</span>}</div>
    <details className="mt-2 border-t border-line text-xs"><summary className="min-h-11 cursor-pointer py-3 font-medium">Exact observations</summary><p className="mb-2 leading-relaxed text-mist">Fetch times show when Am I Broke? saved the bank response. Lines connect those observations; the balance between refreshes is unknown. The vertical scale follows the saved range.{availableCredit && " These are available credit observations, not debt balances; today’s credit limit is not applied to past observations."}</p><div className="max-h-52 overflow-auto"><DataTable className="w-full text-left text-xs"><thead><tr><th className="pb-2 font-medium">Fetched (UTC)</th><th className="pb-2 text-right font-medium">Balance · {currency}</th></tr></thead><tbody>{ordered.map(point => { const stamp = new Date(point.timestamp).toISOString(); return <tr key={point.timestamp} className="border-t border-line/70"><td className="py-2"><time dateTime={stamp}><span className="block">{stamp.slice(0, 10)}</span><span className="text-mist">{stamp.slice(11, 23)}</span></time></td><td className="py-2 text-right tabular-nums">{nativeMoney(point.amount, currency)}</td></tr>; })}</tbody></DataTable></div></details>
  </figure>;
}

function LinkHistory({ row, data, onUpdated }: { row: BankRow; data: BankingData; onUpdated: () => void }) {
  const [selected, setSelected] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bank = row.provider!;
  const [reverseBalance, setReverseBalance] = useState(bank.balance_multiplier === -1);
  const [reverseAmounts, setReverseAmounts] = useState(bank.amount_multiplier === -1);
  const [balanceChoice, setBalanceChoice] = useState(bank.requires_balance_direction ? "" : "confirmed");
  const [accountType, setAccountType] = useState<BankMappingOptions["account_type"]>(bank.requires_balance_direction ? "credit" : "current");
  const [confirmedIdentity, setConfirmedIdentity] = useState(false);
  const predecessors = data.accounts.filter(account => account.account_id && !account.excluded && account.id !== bank.id
    && account.currency === row.currency && (account.present === false || account.connection_enabled === false || account.status !== "ACTIVE"));
  const predecessor = predecessors.find(account => account.account_id === selected);
  const needsIdentity = Boolean(predecessor || (selected === "__new__" && bank.mapping_reason));
  const candidates = data.local_accounts.filter(local => (!local.connected || predecessors.some(account => account.account_id === local.id)) && (local.currency || "GBP") === row.currency);
  const suggested = candidates.filter(local => bank.mapping_candidates.includes(local.id));
  async function save() {
    if (!selected || saving || !balanceChoice || (needsIdentity && !confirmedIdentity)) return;
    setSaving(true); setError(null);
    try { await mapBankAccount(bank.id, selected === "__new__" ? null : selected, selected === "__new__", { amount_multiplier: predecessor?.amount_multiplier ?? (reverseAmounts ? -1 : 1), balance_multiplier: reverseBalance ? -1 : 1, ...(selected === "__new__" ? { account_type: accountType, confirm_distinct_account: confirmedIdentity } : {}), ...(predecessor ? { replace_connection_id: predecessor.id, confirm_reconnection: confirmedIdentity } : {}) }); onUpdated(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "This history could not be linked."); }
    finally { setSaving(false); }
  }
  return <section className="rounded-xl border border-amber/30 bg-amber/5 p-4 sm:p-5" aria-label="Link existing account history"><p className="eyebrow text-amber">Keep your history together</p><Heading level={3} variant="subsection" className="mt-2">Where does this account belong?</Heading>{bank.mapping_reason && <p className="mt-3 rounded-lg border border-amber/25 bg-white/70 p-3 text-sm leading-relaxed text-amber">{bank.mapping_reason}</p>}<p className="mt-2 max-w-3xl text-sm leading-relaxed text-mist">Link {row.name} to its existing statement account so bank activity continues the same history. Choose a new account only when it has no saved statement history.</p>{suggested.length > 0 && <p className="mt-2 text-xs text-amber">Possible matches: {suggested.map(a => a.display_name).join(", ")}. Check the bank and currency before linking.</p>}<div className="mt-4 flex flex-wrap items-end gap-3"><label className="field-label min-w-0 flex-1 basis-64">Saved account<select className="input w-full" value={selected} disabled={saving} onChange={e => { setSelected(e.target.value); setConfirmedIdentity(false); }}><option value="">Choose where to add this activity</option>{candidates.map(local => <option key={local.id} value={local.id}>{local.display_name} · {local.currency || "GBP"}{bank.mapping_candidates.includes(local.id) ? " · Possible match" : ""}</option>)}<option value="__new__">Create a new {row.currency} account</option></select></label><button className="btn-primary" disabled={!selected || saving || !balanceChoice || (needsIdentity && !confirmedIdentity)} onClick={() => void save()}>{saving ? "Linking…" : selected === "__new__" ? "Create account" : predecessor ? "Reconnect saved history" : "Link saved history"}</button></div>{needsIdentity && <label className="mt-3 flex min-h-11 items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={confirmedIdentity} disabled={saving} onChange={e => setConfirmedIdentity(e.target.checked)} /><span>{predecessor ? "This is the same financial account as the previous feed. Keep its saved history and card settings." : "This is a different financial account, not a renewed connection to an existing account."}</span></label>}{bank.requires_balance_direction && <label className="field-label mt-4">How should this card balance appear?<select aria-label="How should this card balance appear?" className="input w-full" value={balanceChoice} onChange={e => { setBalanceChoice(e.target.value); setReverseBalance(e.target.value === "-1"); }}><option value="">Confirm how your bank reports debt</option><option value="1">Keep the bank sign · debt is already negative</option><option value="-1">Reverse the bank sign · positive debt becomes negative</option></select><span className="text-xs font-normal">Check the bank balance against the amount owed on your latest statement.</span></label>}<details className="mt-4 text-xs text-mist"><summary className="min-h-11 cursor-pointer font-medium text-foam">Balance and account settings</summary><div className="mt-2 space-y-3 rounded-lg border border-amber/20 bg-white/70 p-3">{selected === "__new__" && <label className="field-label">Account type<select className="input" value={accountType} onChange={e => setAccountType(e.target.value as BankMappingOptions["account_type"])}><option value="current">Current account</option><option value="savings">Savings account</option><option value="credit">Credit card</option><option value="cash">Cash account</option></select></label>}<label className="flex items-start gap-2 leading-relaxed"><input type="checkbox" className="mt-0.5 h-4 w-4 shrink-0 accent-[#AF4321]" checked={reverseBalance} onChange={e => { setReverseBalance(e.target.checked); if (bank.requires_balance_direction) setBalanceChoice(e.target.checked ? "-1" : "1"); }} /><span>Reverse the balance sign<span className="mt-1 block font-normal text-mist">Use when the bank reports money owed as a positive balance. Am I Broke? records debt as negative.</span></span></label><label className="flex items-start gap-2 leading-relaxed"><input type="checkbox" className="mt-0.5 h-4 w-4 shrink-0 accent-[#AF4321]" checked={reverseAmounts} onChange={e => setReverseAmounts(e.target.checked)} /><span>Reverse transaction signs<span className="mt-1 block font-normal text-mist">Use only if purchases appear positive and incoming money appears negative. Check a known transaction first.</span></span></label></div></details>{error && <div className="mt-3"><ErrorBox error={error} /></div>}<p className="mt-3 text-xs text-mist">Existing transactions and your saved classifications are preserved. Ambiguous overlaps stay in review.</p></section>;
}

function ActivityRow({ transaction }: { transaction: { key: string; date: string | null; amount: number; currency: string; merchant: string | null; description: string; category?: string | null; badge?: string; pending?: boolean } }) {
  const t = transaction;
  return <div className="flex items-start gap-3 border-t border-line/60 py-3.5 sm:gap-4"><span aria-hidden="true" className={`mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm ${t.pending ? "bg-amber/10 text-amber" : t.amount > 0 ? "bg-kelp/10 text-kelp" : "bg-surface2 text-mist"}`}>{t.pending ? "◷" : t.amount > 0 ? "↙" : "↗"}</span><div className="min-w-0 flex-1"><p className="break-words text-sm font-medium">{t.merchant || t.description || "Bank transaction"}</p>{t.merchant && t.description && t.merchant !== t.description && <p className="mt-1 break-words text-xs leading-relaxed text-mist">{t.description}</p>}<div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-1 text-xs text-mist"><span>{shortDate(t.date)}</span>{t.category && <><span aria-hidden="true">·</span><span>{t.category}</span></>}{t.badge && <><span aria-hidden="true">·</span><span className={t.pending ? "text-amber" : ""}>{t.badge}</span></>}</div></div><div className={`shrink-0 text-right text-sm font-semibold tabular-nums ${t.amount > 0 ? "text-kelp" : "text-foam"}`}>{t.amount > 0 ? "+" : ""}{nativeMoney(t.amount, t.currency)}<span className="mt-1 block text-xs font-normal text-mist">{t.currency}</span></div></div>;
}
const FEED_LABELS: Record<string, string> = { imported: "Added to ledger", matched: "Matched to statement", native: "Saved in bank feed", pending: "Pending", review: "Needs review", ignored: "Cache only", waiting_mapping: "Link history first" };

function AccountActivity({ row, refresh }: { row: BankRow; refresh: number }) {
  const hasLedger = Boolean(row.local && row.currency === "GBP");
  const [source, setSource] = useWorkspaceState<"ledger" | "feed" | "review">(`banking:${row.key}:source`, hasLedger ? "ledger" : "feed");
  const [query, setQuery] = useWorkspaceState(`banking:${row.key}:query`, "");
  const [search, setSearch] = useState(query);
  const [dateFrom, setDateFrom] = useWorkspaceState(`banking:${row.key}:from`, "");
  const [dateTo, setDateTo] = useWorkspaceState(`banking:${row.key}:to`, "");
  const [status, setStatus] = useWorkspaceState<"all" | "booked" | "pending">(`banking:${row.key}:status`, "all");
  const [offset, setOffset] = useWorkspaceState(`banking:${row.key}:offset`, 0);
  const results = useRef<HTMLDivElement>(null);
  const focusNext = useRef<number | "page" | null>(null);
  const [decision, setDecision] = useState(0);
  const limit = 30;
  useEffect(() => {
    if (query === search) return;
    const timer = setTimeout(() => { setSearch(query); setOffset(0); }, 250);
    return () => clearTimeout(timer);
  }, [query, search, setOffset]);
  const invalidDates = Boolean(dateFrom && dateTo && dateFrom > dateTo);
  const requestKey = JSON.stringify([row.key, source, search, dateFrom, dateTo, status, offset]);
  const activity = useAsync(async (signal): Promise<{ source: string; key: string; ledger?: TxnPage; feed?: BankAccountDetail }> => {
    if (invalidDates) return { source, key: requestKey };
    if (source === "ledger" && row.local) return { source, key: requestKey, ledger: await api.transactions({ account: row.local.id, q: search, date_from: dateFrom, date_to: dateTo, sort: "date", order: "desc", limit, offset }, signal) };
    if (row.provider) return { source, key: requestKey, feed: await fetchBankAccount(row.provider.id, { limit, offset, q: search, date_from: dateFrom, date_to: dateTo, status: source === "review" ? "review" : status }, signal) };
    // Saved statement accounts retain their history, including missing balances.
    if (row.local) return { source: "ledger", key: requestKey, ledger: await api.transactions({ account: row.local.id, q: search, date_from: dateFrom, date_to: dateTo, sort: "date", order: "desc", limit, offset }, signal) };
    return { source, key: requestKey };
  }, [requestKey, refresh]);
  const current = activity.data?.source === source || !row.provider ? activity.data : null;
  const ledger = current?.ledger, feed = current?.feed;
  const totals = ledger ? { GBP: { money_in: ledger.sum_in, money_out: Math.abs(ledger.sum_out) } } : feed?.totals_by_currency || {};
  const rows = ledger ? ledger.items.map(t => ({ key: `ledger:${t.id}`, date: t.txn_date, amount: t.amount, currency: "GBP", merchant: t.merchant, description: t.description, category: t.category, badge: t.transfer_group ? "Transfer" : undefined })) : (feed?.transactions || []).map((t: BankTransaction) => ({ key: `bank:${t.id}`, date: t.date, amount: t.amount, currency: t.currency, merchant: t.merchant, description: t.description, pending: t.is_pending, badge: t.is_pending ? "Pending" : FEED_LABELS[t.status] || t.status }));
  const total = ledger?.total ?? feed?.total ?? 0;
  const settled = !activity.loading && !activity.error && activity.data?.key === requestKey;
  const correctingPage = settled && !invalidDates && offset > 0 && offset >= total;
  useEffect(() => {
    // A decision can remove the last row of a later page. Return to the last
    // remaining page using the new response, never a retained filter result.
    if (correctingPage) setOffset(Math.max(0, Math.floor((total - 1) / limit) * limit));
  }, [correctingPage, total, setOffset]);
  useEffect(() => {
    if (!settled || correctingPage || focusNext.current === null) return;
    if (typeof focusNext.current === "number" && feed?.reviews.some(item => item.id === focusNext.current)) return;
    focusNext.current = null;
    const target = results.current?.querySelector<HTMLElement>("[data-review-heading]") ?? results.current;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: "nearest", behavior: "instant" });
  }, [settled, correctingPage, feed, decision]);
  function changeSource(next: typeof source) { focusNext.current = null; setSource(next); setOffset(0); }
  function clearFilters() { setQuery(""); setSearch(""); setDateFrom(""); setDateTo(""); setStatus("all"); setOffset(0); }
  function reviewed(id: number) {
    focusNext.current = id;
    setDecision(value => value + 1);
    // resolveBankReview invalidates the shared read cache and announces the
    // mutation. useAsync and the bank directory already subscribe to it.
  }
  function page(next: number) { focusNext.current = "page"; setOffset(next); }
  const filtersActive = Boolean(query || dateFrom || dateTo || (source === "feed" && status !== "all"));
  const filterSummary = [dateFrom && `From ${shortDate(dateFrom)}`, dateTo && `To ${shortDate(dateTo)}`, source === "feed" && status !== "all" && `${status === "pending" ? "Pending" : "Booked"} only`].filter(Boolean).join(" · ") || "All dates";
  const busy = activity.loading || correctingPage || (!activity.error && activity.data?.key !== requestKey);
  return <Card aria-label="Account activity" className="p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><Heading level={3} variant="subsection">Account activity</Heading><p className="mt-1 text-xs leading-relaxed text-mist">{source === "ledger" ? "Saved history uses the statement’s transaction date when confirmed; the bank’s posting date can be later." : source === "review" ? "Bank dates can reflect posting. Compare them with transaction dates in saved history." : "Bank dates can reflect posting after the transaction date in saved history. These filters use bank dates."}</p></div>{row.provider && <div className="flex max-w-full flex-wrap gap-1 rounded-lg border border-line bg-surface2 p-1" aria-label="Activity source">{hasLedger && <button className={`min-h-11 rounded-md px-3 text-xs font-medium ${source === "ledger" ? "bg-white text-foam shadow-sm" : "text-mist"}`} aria-pressed={source === "ledger"} onClick={() => changeSource("ledger")}>Complete ledger</button>}<button className={`min-h-11 rounded-md px-3 text-xs font-medium ${source === "feed" ? "bg-white text-foam shadow-sm" : "text-mist"}`} aria-pressed={source === "feed"} onClick={() => changeSource("feed")}>Bank feed</button>{(row.provider.counts.review > 0 || source === "review") && <button className={`min-h-11 rounded-md px-3 text-xs font-medium ${source === "review" ? "bg-white text-amber shadow-sm" : "text-amber"}`} aria-pressed={source === "review"} onClick={() => changeSource("review")}>Review ({row.provider.counts.review})</button>}</div>}</div>
    <div className="my-3 grid items-end gap-3 xl:grid-cols-[minmax(200px,1fr)_2fr]"><label className="field-label min-w-0">Search activity<input className="input w-full" type="search" maxLength={200} value={query} placeholder="Merchant or description" onChange={event => setQuery(event.target.value)} /></label><MobileDisclosure label="Activity filters" summary={filterSummary} contentClassName="grid grid-cols-2 items-end gap-3 sm:grid-cols-3"><label className="field-label min-w-0">From<input className="input w-full" type="date" value={dateFrom} max={dateTo || undefined} onChange={event => { setDateFrom(event.target.value); setOffset(0); }} /></label><label className="field-label min-w-0">To<input className="input w-full" type="date" value={dateTo} min={dateFrom || undefined} onChange={event => { setDateTo(event.target.value); setOffset(0); }} /></label>{source === "feed" && <label className="field-label col-span-2 min-w-0 sm:col-span-1">Settlement<select className="input w-full" value={status} onChange={event => { setStatus(event.target.value as typeof status); setOffset(0); }}><option value="all">All bank activity</option><option value="booked">Booked only</option><option value="pending">Pending only</option></select></label>}</MobileDisclosure></div>
    {filtersActive && <div className="mb-3 grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2"><p className="min-w-0 truncate text-xs text-mist" title={`${filterSummary}${query ? ` · Search: ${query}` : ""}`}>{filterSummary}{query ? ` · Search: ${query}` : ""}</p><button className="min-h-11 shrink-0 text-xs font-medium text-mist underline underline-offset-4" onClick={clearFilters}>Clear filters</button></div>}
    {invalidDates && <p role="alert" className="mb-4 text-sm text-coral">Choose an end date on or after the start date.</p>}
    <RefreshStatus loading={busy} error={activity.error} retry={activity.refetch} retaining={Boolean(current)} />
    {activity.error && !current ? null : busy && !current ? <TableSkeleton label="Loading account activity…" rows={5} /> : <div ref={results} tabIndex={-1} aria-label="Account activity results" className="async-content outline-hidden scroll-mt-24" aria-busy={busy}>
      {!invalidDates && source !== "review" && Object.entries(totals).length > 0 && <div className="mb-3 rounded-lg border border-line bg-deep/40 px-3 py-2.5"><div className="flex flex-wrap gap-x-8 gap-y-3">{Object.entries(totals).map(([currency, total]) => <div key={currency} className="flex flex-wrap gap-x-6 gap-y-2"><div><p className="text-xs text-mist">Money in · {currency}</p><p className="mt-0.5 text-lg font-semibold tabular-nums text-kelp">{nativeMoney(total.money_in, currency)}</p></div><div><p className="text-xs text-mist">Money out · {currency}</p><p className="mt-0.5 text-lg font-semibold tabular-nums">{nativeMoney(Math.abs(total.money_out), currency)}</p></div></div>)}</div><details className="mt-1 text-xs text-mist"><summary className="min-h-11 cursor-pointer py-3">About these totals</summary><p className="pb-2 leading-relaxed">Recorded, booked movement for these filters. Includes transfers; excludes pending and unresolved bank items. {source === "ledger" ? "Amounts are in GBP. Matched bank transactions appear once alongside saved statement history." : "Each currency is shown separately."}</p></details></div>}
      {invalidDates ? null : (correctingPage || (source === "review" && busy && !feed?.reviews.length)) ? <TableSkeleton label="Opening the remaining reviews…" rows={3} /> : source === "review" ? <BankReviewQueue accountKey={row.key} items={feed?.reviews || []} total={total} offset={offset} loading={busy} onResolved={reviewed} /> : !rows.length ? <Empty>{filtersActive ? "No transactions match these filters." : "No activity is available in this history yet."}</Empty> : <div>{rows.map(transaction => <ActivityRow key={transaction.key} transaction={transaction} />)}</div>}
      {total > 0 && !invalidDates && <nav aria-label="Account activity pages" className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3"><p className="text-xs text-mist">{Math.min(offset + 1, total)}–{Math.min(offset + limit, total)} of {total.toLocaleString()} {source === "review" ? "reviews" : "transactions"}</p>{total > limit && <div className="flex gap-2"><button className="btn" disabled={offset === 0 || busy} onClick={() => page(Math.max(0, offset - limit))}>← Previous</button><button className="btn" disabled={offset + limit >= total || busy} onClick={() => page(offset + limit)}>Next →</button></div>}</nav>}
    </div>}
  </Card>;
}

function AccountDetail({ row, data, refresh, onUpdated, onBack, onBank }: { row: BankRow; data: BankingData; refresh: number; onUpdated: () => void; onBack: () => void; onBank: () => void }) {
  const detail = useAsync(signal => row.provider ? fetchBankAccount(row.provider.id, { limit: 1 }, signal) : Promise.resolve<BankAccountDetail | null>(null), [row.key, refresh]);
  const provider = row.provider;
  const valuation = accountValuation(row);
  const balance = accountBalance(row);
  return <div className="space-y-5"><nav aria-label="Bank account breadcrumb" className="flex flex-wrap items-center gap-2 text-xs text-mist"><button className="min-h-11 hover:text-teal" onClick={onBack}>All banks</button><span aria-hidden="true">/</span><button className="min-h-11 hover:text-teal" onClick={onBank}>{row.institution}</button><span aria-hidden="true">/</span><span className="break-words text-foam" aria-current="page">{row.name}</span></nav>
    <section className="relative overflow-hidden rounded-2xl bg-[#27362e] p-4 text-white sm:p-5" aria-label="Account balance"><div aria-hidden="true" className="pointer-events-none absolute -right-12 -top-20 h-56 w-56 rounded-full border border-white/5" /><div aria-hidden="true" className="pointer-events-none absolute -right-4 -top-12 h-40 w-40 rounded-full border border-white/5" /><div className="relative"><div className="flex flex-wrap items-center justify-between gap-3"><p className="text-xs font-medium text-[#c5d2c7]">{row.institution} <span className="mx-1 opacity-50">/</span> {row.currency}</p><span className="rounded-md bg-white/10 px-2 py-1 text-xs font-medium">{provider ? "BANK CONNECTION" : row.local?.balance_source === "manual" ? "MANUAL OBSERVATION" : "SAVED STATEMENTS"}</span></div><Heading level={2} variant="section" className="mt-3 break-words">{row.name}</Heading><p className="mt-3 text-xs font-semibold uppercase tracking-[.16em] text-[#bacbbe]">{accountBalanceLabel(row)}</p><p className="mt-2 break-words text-3xl font-semibold tracking-tight tabular-nums sm:text-4xl">{valuation?.current_unknown ? "Unknown" : nativeMoney(balance, row.currency)}</p>{valuation?.basis === "available_credit" && <div className="mt-3 text-[#c5d2c7]"><AvailableCreditEvidence valuation={valuation} currency={row.currency} /></div>}{valuation && !["reported", "available_credit"].includes(valuation.basis) && <div className="mt-3 space-y-2 text-xs leading-relaxed text-[#c5d2c7]"><p>{valuation.explanation}</p>{valuation.estimated && valuation.effective_as_of && <p>Booked activity through {bankTimestamp(valuation.effective_as_of)}. Pending charges excluded.</p>}<p>{valuation.reported_source === "statement" || !provider ? "Previous statement balance" : "Provider-reported balance"}: {nativeMoney(valuation.reported_balance, row.currency)}. Excluded from the current calculation.</p></div>}<p className="mt-4 text-xs leading-relaxed text-[#c5d2c7]">{provider ? provider.balance ? `Bank feed fetched ${bankTimestamp(provider.balance.fetched_at)} · saved locally` : "The bank has not returned a balance yet." : row.local?.balance_asof ? `${row.local.balance_source === "manual" ? "Observation" : "Statement"} dated ${shortDate(row.local.balance_asof)}` : "No statement balance is available."}</p></div></section>
    {provider?.requires_balance_direction && provider.balance?.reported_amount != null && <p className="rounded-xl border border-amber/30 bg-amber/5 p-4 text-sm leading-relaxed text-amber">Provider-reported balance: {nativeMoney(provider.balance.reported_amount, row.currency)}. Confirm how the bank reports debt before including this card in balances.</p>}
    {provider?.present === false && <div role="status" className="rounded-xl border border-amber/30 bg-amber/5 p-4 text-sm text-amber">This account was not returned in the latest bank account list. Its saved history remains available, and its balance is excluded from connected bank totals.</div>}
    {provider?.last_error && <div role="status" className="rounded-xl border border-amber/30 bg-amber/5 p-4 text-sm text-amber">{provider.last_error} Saved balances and history remain available. Check the connection in Lunch Flow if it needs reconnecting.</div>}
    {provider && !provider.account_id && <LinkHistory row={row} data={data} onUpdated={onUpdated} />}
    {row.local && !provider && row.currency !== "GBP" ? <Card aria-label="Account activity" className="space-y-3">
      <Heading level={3} variant="subsection">Imported activity</Heading>
      <p className="text-sm leading-relaxed text-mist">For this {row.currency} account, open Workspace reports and expand “Amounts and conversion evidence” to see imported transactions and their original amounts. This account has no connected bank feed.</p>
      <button className="btn" onClick={() => openFinancePage("settings", undefined, "reports")}>Open workspace reports →</button>
    </Card> : <AccountActivity row={row} refresh={refresh} />}
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Card className="space-y-3" aria-label="Connection & history"><div className="flex flex-wrap justify-between gap-2"><Heading level={3} variant="subsection">Connection &amp; history</Heading><AccountStatus row={row} /></div><dl className="space-y-3 text-xs">{row.local?.account_type === "credit" && <div className="flex justify-between gap-3"><dt className="text-mist">Account type</dt><dd className="text-right">Credit card</dd></div>}<div className="flex justify-between gap-3"><dt className="text-mist">Source</dt><dd className="text-right">{provider ? "Lunch Flow bank feed" : row.local?.balance_source === "manual" ? "Manual balance observation" : "Imported statements"}</dd></div><div className="flex justify-between gap-3"><dt className="text-mist">{!provider && row.local?.balance_source === "manual" ? "Recorded" : "Last fetched"}</dt><dd className="text-right">{provider ? bankTimestamp(provider.last_synced_at) : row.local?.balance_source === "manual" ? bankTimestamp(row.local.balance_recorded_at) : "Saved statements"}</dd></div><div className="flex justify-between gap-3"><dt className="text-mist">Saved ledger history</dt><dd>{row.local?.n_txns?.toLocaleString() ?? "—"} transactions</dd></div>{provider && <><div className="flex justify-between gap-3"><dt className="text-mist">Cached bank activity</dt><dd>{provider.counts.cached.toLocaleString()} transactions</dd></div><div className="flex justify-between gap-3"><dt className="text-mist">Pending / needs review</dt><dd>{provider.counts.pending} / {provider.counts.review}</dd></div></>}</dl><p className="border-t border-line pt-3 text-xs leading-relaxed text-mist">{provider ? "Fetch time records when Am I Broke? saved the response. The bank may refresh on a different schedule. A returned balance can include pending items according to the bank’s own rules." : row.local?.balance_source === "manual" ? "This balance was entered manually for its observation date. It does not establish complete transaction history or confirm later activity." : "Statement history stays available. When a bank connection returns this account, link it to keep one continuous history."}</p></Card>
      {provider && <div className="async-content min-h-[204px]" aria-busy={detail.loading}>
        <RefreshStatus loading={detail.loading} error={detail.error} retry={detail.refetch} retaining={Boolean(detail.data)} />
        {detail.loading && !detail.data ? <LoadingRegion label="Loading saved balance observations…" className="h-[204px] rounded-xl border border-line bg-deep/30 p-3"><SkeletonBlock className="h-4 w-52 max-w-full" /><SkeletonBlock className="mt-5 h-[76px] w-full" /><SkeletonBlock className="mt-5 h-4 w-36" /></LoadingRegion> : detail.data && <BalanceHistory points={detail.data.balance_history.map(point => valuation?.basis === "available_credit" && point.amount !== null ? { ...point, amount: point.amount * (provider.balance_multiplier ?? 1) } : point)} currency={row.currency} availableCredit={valuation?.basis === "available_credit"} />}
      </div>}
    </div>
    {provider?.account_id && (row.local?.account_type === "credit" || provider.balance_rule) && <BankBalanceRuleSettings account={provider} currency={row.currency} onUpdated={onUpdated} />}

  </div>;
}

export default function Banking({ initialAccountId }: { initialAccountId?: string | null } = {}) {
  const [data, setData] = useState<BankingData | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [healthOpen, setHealthOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [bank, setBank] = useWorkspaceState<string | null>("banking:bank", null);
  const [selection, setSelection] = useWorkspaceState<string | null>("banking:selection", null);
  const [filter, setFilter] = useWorkspaceState("banking:filter", "");
  const heading = useRef<HTMLHeadingElement>(null);
  const previousLocation = useRef({ bank, selection });
  useEffect(() => {
    if (previousLocation.current.bank === bank && previousLocation.current.selection === selection) return;
    previousLocation.current = { bank, selection };
    heading.current?.focus({ preventScroll: true });
    heading.current?.scrollIntoView({ block: "start", behavior: "instant" });
  }, [bank, selection]);
  useEffect(() => watchData(() => setRefresh(n => n + 1)), []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void fetchBanking(controller.signal).then(result => { if (!controller.signal.aborted) { setData(result); setError(null); } }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Bank accounts could not be loaded."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    // Poll uncached status while a server refresh runs, and periodically pick up
    // scheduled refreshes without requiring navigation or another manual sync.
    let busy = false;
    const timer = window.setInterval(() => {
      if (busy || document.visibilityState !== "visible") return;
      busy = true;
      void fetchBanking(controller.signal, true).then(result => {
        if (!controller.signal.aborted) setData(result);
      }).catch(() => { /* Keep saved data; the normal load exposes read errors. */ }).finally(() => { busy = false; });
    }, data?.sync.running ? 3000 : 30000);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [data?.sync.running]);
  const rows = useMemo(() => data ? bankRows(data) : [], [data]);
  useEffect(() => {
    if (!initialAccountId || !data) return;
    const chosen = rows.find(row => row.local?.id === initialAccountId || row.key === initialAccountId || row.provider?.id === initialAccountId);
    if (chosen) { setSelection(chosen.key); setBank(chosen.institution); }
    // Open a requested account once, without resetting drilldowns after refresh.
  }, [initialAccountId, Boolean(data)]); // eslint-disable-line react-hooks/exhaustive-deps
  const selected = rows.find(row => row.key === selection);
  const names = [...new Set(rows.map(row => row.institution))];
  const connected = rows.filter(row => row.provider && row.provider.present !== false);
  const unresolved = connected.filter(row => !row.provider!.account_id).length;
  const scoped = rows.filter(row => !bank || row.institution === bank);
  const scopedConnected = scoped.filter(row => row.provider && row.provider.present !== false);
  const balanceTotals = !bank && data?.balance_totals_by_currency ? data.balance_totals_by_currency : bankBalanceTotals(scoped.filter(row => !row.provider?.requires_balance_direction));
  const balanceChecks = (data?.balance_checks ?? []).filter(check => (check.requires_review ?? !["no_discrepancy", "available_credit"].includes(check.status)) && (!bank || scoped.some(row => row.local?.id === check.account_id || row.provider?.id === check.account_id)) && (!selected || selected.local?.id === check.account_id || selected.provider?.id === check.account_id));
  function checkValuation(accountId: string) {
    const row = rows.find(item => item.local?.id === accountId || item.provider?.id === accountId);
    return row ? accountValuation(row) : undefined;
  }
  const estimatedRows = scoped.filter(row => row.provider && row.provider.present !== false && accountValuation(row)?.estimated);
  const unknownRows = scoped.filter(row => accountValuation(row)?.current_unknown);
  const uncheckedUnknownRows = unknownRows.filter(row => !balanceChecks.some(check => check.account_id === row.local?.id || check.account_id === row.provider?.id));
  const displayed = scoped.filter(row => (!filter || `${row.name} ${row.institution} ${row.currency}`.toLowerCase().includes(filter.toLowerCase())));
  const groups = [...new Set(displayed.map(row => row.institution))];
  async function sync() {
    if (syncing) return;
    setSyncing(true); setError(null); setNotice("");
    try { const result = await syncBanking(); setData(result); setRefresh(n => n + 1); setNotice(result.sync.error ? "Refresh finished with some account errors. Saved history remains available." : "Bank refresh complete. New activity is saved locally."); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "The bank refresh failed. Your saved history is still available."); }
    finally { setSyncing(false); }
  }
  function allBanks() { setSelection(null); setBank(null); setFilter(""); }
  function openBank(name: string) { setSelection(null); setBank(name); setFilter(""); }
  const updating = syncing || Boolean(data?.sync.running);
  return <div className="space-y-4"><header className="flex flex-col items-start gap-3 sm:flex-row sm:justify-between"><div><Heading level={2} variant="intro" ref={heading} tabIndex={-1} className="scroll-mt-24 focus:outline-hidden">{selected ? "Account details" : bank ? bank : "Bank balances"}</Heading><p className="mt-1 text-xs leading-relaxed text-mist">{selected ? "Bank activity and saved history." : `${rows.length} account${rows.length === 1 ? "" : "s"} · ${names.length} bank${names.length === 1 ? "" : "s"}${rows.some(row => row.local?.n_txns) ? " · saved history" : ""}`}</p></div><button className="btn" disabled={updating || !data?.configured} onClick={() => void sync()}><RefreshIcon spinning={updating} />{updating ? "Refreshing banks…" : "Refresh banks"}</button></header>
    <details className="text-xs text-mist"><summary className="min-h-11 cursor-pointer">Last successful refresh: {bankTimestamp(data?.sync.last_success_at)}</summary><p className="pt-2 leading-relaxed">Read-only access. {data?.sync.interval_seconds ? `Automatic check every ${Math.round(data.sync.interval_seconds / 60)} minutes while the server runs. ` : ""}Fetched time records when Am I Broke? saved the bank response; the bank can update on a different schedule.</p></details>
    {notice && <p role="status" className="rounded-xl border border-kelp/20 bg-kelp/5 px-4 py-3 text-sm text-kelp">{notice}</p>}
    {error && <ErrorBox error={error} retry={() => setRefresh(n => n + 1)} />}
    {data?.sync.error && !updating && <div role="status" className="rounded-xl border border-amber/25 bg-amber/5 px-4 py-3 text-sm leading-relaxed text-amber">{data.sync.error} Saved account data remains visible below.</div>}
    {loading && !data && <PageSkeleton variant="banking" label="Opening your banks and saved history…" />}
    {data && !selected && <>
      {selection && <p role="status" className="rounded-xl border border-amber/30 bg-amber/5 p-4 text-sm text-amber">This account is no longer in the saved account list. Choose an available account below.</p>}
      {bank && <button className="min-h-11 text-sm font-medium text-mist hover:text-teal" onClick={allBanks}>← All banks</button>}
      <div className="flex flex-wrap items-end justify-between gap-3"><Heading level={3} variant="subsection">{bank ? "Your accounts" : "Your banks"}</Heading><label className="field-label w-full sm:w-80"><span className="sr-only">Find bank or account</span><input className="input w-full" type="search" aria-label="Find bank or account" value={filter} onChange={e => setFilter(e.target.value)} placeholder="Find a bank, account or currency" /></label></div>
    </>}
    {data && !data.configured && <Card><Heading level={3} variant="subsection">Connect a bank (optional)</Heading><p className="mt-2 text-sm text-mist">Add a provider connection in Settings to refresh bank accounts, or use a manual account and CSV imports. Saved history remains available below.</p><button className="btn mt-3" onClick={() => openFinancePage("settings")}>Manage connections and imports</button></Card>}
    {balanceChecks.length > 0 && <section aria-label="Bank balance checks" className="space-y-3 rounded-xl border border-amber/30 bg-amber/5 p-4"><Heading level={3} variant="subsection">Balance estimates &amp; missing balances</Heading>{balanceChecks.map(check => <div key={check.account_id}><p className="text-sm font-medium">{check.name}</p><p className="mt-1 text-xs text-mist">Original source balance: {nativeMoney(check.reported_balance, check.currency)}</p><p className="mt-1 text-xs leading-relaxed text-amber">{checkValuation(check.account_id)?.explanation || check.message}</p>{check.status === "discrepancy" && rows.some(row => row.provider && (row.local?.id === check.account_id || row.provider.id === check.account_id)) && <p className="mt-2 text-xs text-mist"><a className="underline decoration-line underline-offset-4" href="https://www.lunchflow.app/docs/guides/configuration/balance" target="_blank" rel="noopener noreferrer">Check Lunch Flow balance settings ↗</a> · Compare the actual amount owed or current balance with available credit.</p>}</div>)}<button className="min-h-11 text-xs font-medium underline decoration-line underline-offset-4" onClick={() => openFinancePage("net-worth")}>View full net assets and debt sources</button></section>}
    {selected && data ? <AccountDetail key={selected.key} row={selected} data={data} refresh={refresh} onUpdated={() => setRefresh(n => n + 1)} onBack={allBanks} onBank={() => openBank(selected.institution)} /> : data && <>
      {scopedConnected.length > 0 && <section className="rounded-xl border border-line bg-white p-4" aria-label="Connected bank balances"><Heading level={3} variant="subsection">{estimatedRows.length ? "Estimated connected net balances" : "Connected net balances"} <span className="ml-2 text-xs font-normal text-mist">{Object.keys(balanceTotals).length} currencies · {scopedConnected.length} accounts</span></Heading><div className="mt-3 rounded-xl bg-[#27362e] p-4 text-white"><p className="text-xs leading-relaxed text-[#bdcfc2]">Positive bank balances minus supported debt balances, kept in their original currencies. Excludes investments and statement-only accounts.</p>{Object.keys(balanceTotals).length ? <><Totals totals={Object.fromEntries(Object.entries(balanceTotals).map(([currency, total]) => [currency, total.net]))} className="mt-4" /><details className="mt-3 border-t border-white/15 text-xs"><summary className="min-h-11 cursor-pointer py-3 font-medium">Positive balances and debt by currency</summary><dl className="space-y-2 pb-2">{Object.entries(balanceTotals).map(([currency, total]) => <div key={currency} className="flex flex-wrap justify-between gap-2"><dt>{currency} positive balances − {estimatedRows.length ? "estimated debt" : "debt"}</dt><dd className="tabular-nums">{nativeMoney(total.assets, currency)} − {nativeMoney(total.liabilities, currency)} = {nativeMoney(total.net, currency)}</dd></div>)}</dl></details></> : <p className="mt-3 text-sm">No confirmed connected balance is available yet.</p>}</div>{scoped.some(row => row.provider?.requires_balance_direction) && <p className="mt-3 text-xs text-amber">Unconfirmed card balances are excluded until you link the account and confirm how debt is reported.</p>}{estimatedRows.length > 0 && <p className="mt-3 text-xs text-amber">Includes estimates for {estimatedRows.map(row => row.name).join(", ")} from statements and booked activity. Pending charges excluded.</p>}{unknownRows.length > 0 && <p className="mt-3 text-xs text-amber">Current balance unknown for {unknownRows.map(row => row.name).join(", ")}. Unknown balances are excluded from totals, not treated as zero.</p>}{!bank && Object.entries(data.statement_liabilities_by_currency ?? {}).filter(([, amount]) => amount > 0).map(([currency, amount]) => <p key={currency} className="mt-3 text-xs text-mist">Additional supported statement-only debt: {nativeMoney(amount, currency)}. This is excluded from connected balances above.</p>)}<button className="mt-2 min-h-11 text-xs text-mist underline decoration-line underline-offset-4" onClick={() => openFinancePage("net-worth")}>View full net assets and debt sources</button></section>}
      {scopedConnected.length === 0 && <>
        {!bank && Object.entries(data.statement_liabilities_by_currency ?? {}).filter(([, amount]) => amount > 0).map(([currency, amount]) => <p key={currency} className="text-sm text-mist">Supported statement-only debt: <span className="font-medium tabular-nums text-foam">{nativeMoney(amount, currency)}</span>. <button className="inline min-h-11 text-sm text-teal underline decoration-teal/30 underline-offset-4" onClick={() => openFinancePage("net-worth")}>View net assets</button></p>)}
        {uncheckedUnknownRows.length > 0 && <p className="text-sm leading-relaxed text-amber">Current balance unknown for {uncheckedUnknownRows.map(row => row.name).join(", ")}. Unknown balances are excluded from totals, not treated as zero.</p>}
      </>}
      {(unresolved > 0 || data.stats.review > 0) && <p className="rounded-lg border border-amber/25 bg-amber/5 px-3 py-2.5 text-xs leading-relaxed text-mist">{unresolved > 0 ? `${unresolved} accounts need a history link. ` : ""}{data.stats.review > 0 ? `${data.stats.review} possible overlaps need review. ` : ""}Open an account marked “Needs attention” to inspect its history.</p>}
      <div className="columns-1 gap-4 xl:columns-2">{groups.map(name => <BankGroup key={name} name={name} rows={displayed.filter(row => row.institution === name)} onBank={() => openBank(name)} onAccount={row => { setSelection(row.key); setBank(row.institution); }} />)}</div>
      {!displayed.length && <Card><Empty>{filter ? "No accounts match your search." : "No bank accounts yet. Connect a bank or add an account in Settings."}</Empty></Card>}
      <Disclosure title="Connection health" onToggle={event => setHealthOpen(event.currentTarget.open)}>{healthOpen && <Suspense fallback={<Spinner label="Checking saved connections…" />}><BankConnectionHealth /></Suspense>}</Disclosure>
      <Disclosure title="How bank feeds and statement history work together"><div className="grid gap-3 text-xs leading-relaxed text-mist sm:grid-cols-2"><p>New activity is cached locally and checked against saved history. Confirmed overlaps link to the existing transaction; uncertain matches stay in review. Your classifications and previous reviews stay attached to the original records.</p><p>Fetch dates show when Am I Broke? saved a response. Banks update on their own schedules. Currencies stay separate, pending activity is labelled, and existing statement history is retained when an account is linked.</p><p className="sm:col-span-2">Missing a connected account? Check which accounts your provider connection can access in Settings, then refresh banks here.</p></div></Disclosure>
    </>}
  </div>;
}
