import { lazy, Suspense, useState } from "react";
import type { TxnPage } from "../api";
import { shortDate, money } from "../format";
import { openFinancePage } from "../navigation";
import { Heading } from "./Heading";
import { DataTable } from "./DataTable";
import { Card, Empty, ErrorBox, Modal, RefreshStatus, Spinner, useCompactLayout } from "./ui";
import { TableSkeleton } from "./Skeleton";

const TransactionReview = lazy(() => import("./TransactionReview"));
type RecentState = { data: TxnPage | null; loading: boolean; error: string | null; refetch: () => void };

export default function RecentTransactions({ state, reportingCurrency = "GBP" }: { state: RecentState; reportingCurrency?: string }) {
  const compact = useCompactLayout();
  const [reviewId, setReviewId] = useState<number | null>(null);
  const items = state.data?.items.slice(0, 10) ?? [];
  return <>
    <Card className="p-0 overflow-hidden" role="region" aria-label="Latest transactions">
      <div className="border-b border-line px-3 py-2 sm:px-4">
        <div className="flex items-center justify-between gap-2">
          <Heading level={2} variant="section">Latest transactions</Heading>
          <button className="btn btn-quiet shrink-0" aria-label="View activity →" onClick={() => openFinancePage("transactions")}><span className="sm:hidden">View all →</span><span className="hidden sm:inline">View activity →</span></button>
        </div>
        <p className="text-xs text-mist">Latest 10 saved transactions · all accounts · GBP</p>
      </div>
      {!state.data && state.loading ? <TableSkeleton label="Loading recent transactions…" rows={10} density="compact" /> : !state.data && state.error ? <div className="p-3"><ErrorBox error={state.error} retry={state.refetch} /></div> : <>
        <RefreshStatus loading={state.loading} error={state.error} retry={state.refetch} />
        {!items.length ? <Empty>{reportingCurrency === "GBP" ? "No saved transactions yet. Connected or imported activity will appear here." : <><p>No GBP transactions in this list yet. Review your other currencies in Workspace reports.</p><button className="btn mt-3" onClick={() => openFinancePage("settings", undefined, "reports")}>Open workspace reports</button></>}</Empty> : compact ? <ul className="divide-y divide-line" aria-busy={state.loading}>
          {items.map(item => <li key={item.id}><button className="flex w-full items-center justify-between gap-3 px-3 py-3 text-left hover:bg-surface2" onClick={() => setReviewId(item.id)} aria-label={`Review ${item.merchant || item.description}`}>
            <span className="min-w-0"><span className="block break-words text-sm font-medium">{item.merchant || item.description}</span><span className="mt-0.5 block text-xs leading-4 text-mist">{shortDate(item.txn_date)} · {item.account}</span></span>
            <span className={`shrink-0 text-sm font-semibold tabular-nums ${item.amount < 0 ? "text-coral" : "text-kelp"}`}>{money(item.amount)}</span>
          </button></li>)}
        </ul> : <div className="overflow-x-auto focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-teal" role="region" aria-label="Latest transaction records" tabIndex={0} aria-busy={state.loading}><DataTable>
          <thead><tr><th>Date</th><th>Transaction</th><th>Account</th><th className="text-right">Amount · GBP</th></tr></thead>
          <tbody>{items.map(item => <tr key={item.id}><td className="whitespace-nowrap text-mist">{shortDate(item.txn_date)}</td><td><button className="min-h-11 text-left font-medium underline decoration-line underline-offset-4 hover:text-teal" onClick={() => setReviewId(item.id)} aria-label={`Review ${item.merchant || item.description}`}>{item.merchant || item.description}</button></td><td className="text-xs text-mist">{item.account}</td><td className={`whitespace-nowrap text-right font-semibold tabular-nums ${item.amount < 0 ? "text-coral" : "text-kelp"}`}>{money(item.amount)}</td></tr>)}</tbody>
        </DataTable></div>}
      </>}
    </Card>
    {reviewId !== null && <Suspense fallback={<Modal title="Transaction details" onClose={() => setReviewId(null)}><Spinner label="Opening transaction…" /></Modal>}><TransactionReview id={reviewId} onClose={() => setReviewId(null)} onSaved={state.refetch} /></Suspense>}
  </>;
}
