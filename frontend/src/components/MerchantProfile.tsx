import { Disclosure } from "./Disclosure";
import { Heading } from "./Heading";
import { useState } from "react";
import { api } from "../api";
import { Async, Modal, Pill, useAsync } from "./ui";
import TransactionReview from "./TransactionReview";
import { nativeMoney, bankTimestamp } from "../banking";
import { shortDate } from "../format";

export default function MerchantProfile({ id, onClose, onEdit }: { id: number; onClose: () => void; onEdit: () => void }) {
  const activity = useAsync(signal => api.merchantActivity(id, signal), [id]);
  const [reviewId, setReviewId] = useState<number | null>(null);
  if (reviewId !== null) return <TransactionReview id={reviewId} onClose={() => setReviewId(null)} onSaved={activity.refetch} />;
  return <Modal title={activity.data?.merchant.canonical_name || "Saved name activity"} onClose={onClose}>
    <Async state={activity}>{data => {
      const recentMonths = data.monthly.slice(-6);
      const maximum = Math.max(1, ...recentMonths.map(month => month.money_out));
      const researchUrl = data.merchant.source === "public_research" && /^https:\/\//i.test(data.merchant.source_url || "") ? data.merchant.source_url : null;
      return <div className="space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex flex-wrap gap-2"><Pill tone="mute">{data.merchant.kind === "person" ? "Person" : data.merchant.kind === "self" ? "Own account" : data.merchant.kind === "financial" ? "Financial service" : "Saved merchant"}</Pill>{data.merchant.category && <Pill tone="teal">{data.merchant.category}</Pill>}</div><button className="btn" onClick={onEdit}>Edit saved name</button></div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3"><div className="rounded-xl border border-line bg-deep p-4"><p className="eyebrow">Recorded money out</p><p className="mt-2 text-xl font-semibold tabular-nums">{nativeMoney(data.summary.money_out, data.currency)}</p></div><div className="rounded-xl border border-line bg-deep p-4"><p className="eyebrow">Recorded money in</p><p className="mt-2 text-xl font-semibold tabular-nums text-kelp">{nativeMoney(data.summary.money_in, data.currency)}</p></div><div className="col-span-2 rounded-xl border border-line bg-deep p-4 sm:col-span-1"><p className="eyebrow">Saved payments</p><p className="mt-2 text-xl font-semibold tabular-nums">{data.summary.transactions.toLocaleString()}</p><p className="mt-1 text-xs text-mist">Across {data.summary.accounts} {data.summary.accounts === 1 ? "account" : "accounts"}</p></div></div>
        <p className="text-xs leading-relaxed text-mist">{data.method}</p>
        {researchUrl && <p className="text-xs text-mist">Category checked against <a href={researchUrl} target="_blank" rel="noopener noreferrer" className="text-teal underline underline-offset-2">business website ↗</a></p>}
        <div className="flex flex-wrap gap-x-6 gap-y-2 border-y border-line py-3 text-xs text-mist"><span>First recorded: {shortDate(data.summary.first_seen)}</span><span>Latest: {shortDate(data.summary.last_seen)}</span><span>{data.summary.active_months} {data.summary.active_months === 1 ? "month" : "months"} with activity</span></div>
        {recentMonths.length > 1 && <section aria-label="Recorded monthly outflow"><Heading level={3} variant="subsection">Recent recorded outflow</Heading><div className="mt-3 space-y-3">{recentMonths.map(month => <div key={month.month} className="grid grid-cols-[4rem_minmax(0,1fr)_auto] items-center gap-3 text-xs"><span className="text-mist">{month.month}</span><div className="h-2 overflow-hidden rounded-full bg-surface2" aria-hidden="true"><div className="h-full rounded-full bg-teal/70" style={{ width: `${month.money_out / maximum * 100}%` }} /></div><span className="tabular-nums">{nativeMoney(month.money_out, data.currency)}</span></div>)}</div></section>}
        <Disclosure title={`Bank labels linked to this saved name (${data.bank_labels.length})`}><p className="mt-2 text-xs leading-relaxed text-mist">These labels come from matched or imported bank transactions. Your saved name defines the identity; bank wording can differ.</p>{data.bank_labels.length ? <ul className="mt-3 divide-y divide-line">{data.bank_labels.map((label, index) => <li key={`${label.bank}-${index}`} className="py-2.5"><p className="break-words text-sm">{label.label}</p><p className="mt-1 text-xs text-mist">{label.bank} · {label.transactions} linked transactions · fetched {bankTimestamp(label.fetched_at)}</p></li>)}</ul> : <p className="mt-3 text-xs text-mist">No connected bank labels have been linked to this saved name yet.</p>}</Disclosure>
        <section aria-label="Recent merchant transactions"><div className="flex flex-wrap justify-between gap-2"><Heading level={3} variant="subsection">Recent transactions</Heading><span className="text-xs text-mist">Latest {data.transactions.length} of {data.summary.transactions}</span></div>{!data.transactions.length ? <p className="py-5 text-sm text-mist">No ledger transactions currently match this saved rule.</p> : <div className="mt-2 divide-y divide-line">{data.transactions.map(transaction => <button key={transaction.id} className="flex w-full items-start gap-3 py-3 text-left hover:text-teal" onClick={() => setReviewId(transaction.id)} aria-label={`Review payment on ${transaction.txn_date}`}><span className="min-w-0 flex-1"><span className="block break-words text-sm">{transaction.description}</span><span className="mt-1 block text-xs text-mist">{shortDate(transaction.txn_date)} · {transaction.account}</span></span><span className={`shrink-0 text-sm font-medium tabular-nums ${transaction.amount > 0 ? "text-kelp" : ""}`}>{nativeMoney(transaction.amount, data.currency)}</span></button>)}</div>}</section>
      </div>;
    }}</Async>
  </Modal>;
}
