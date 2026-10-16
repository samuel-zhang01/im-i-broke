import { Heading } from "../components/Heading";
import { Disclosure } from "../components/Disclosure";
import { DataTable } from "../components/DataTable";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { api, type Transfer } from "../api";
import {
  Card,
  ErrorBox,
  Pill,
  Spinner,
  useAsync,
  useCompactLayout,
  PageControls,
} from "../components/ui";
import TransactionReview from "../components/TransactionReview";
const TransferSankey = lazy(() => import("../components/Charts").then(module => ({ default: module.TransferSankey })));
import { money, shortDate } from "../format";

type ExplainedTransfer = Transfer & {
  matched?: boolean;
  txn_id?: number;
  evidence?: string;
};

function TransferEvidence({ transfer }: { transfer: ExplainedTransfer }) {
  return <details className="group text-xs text-mist">
    <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 sm:min-h-6">
      <span aria-hidden="true" className="motion-safe:transition-transform group-open:rotate-90">▸</span>
      <Pill tone={transfer.matched === false ? "amber" : "blue"}>{transfer.matched === false ? "Identified · no pair" : "Paired"}</Pill>
      <span className="underline underline-offset-4">Why excluded?</span>
    </summary>
    <p className="mt-2 max-w-xl leading-relaxed">{transfer.evidence || "Equal amount in two accounts, close dates and corroborating transfer descriptions."}</p>
    {transfer.from_desc && <p className="mt-1 break-words">Outgoing: {transfer.from_desc}</p>}
    {transfer.to_desc && <p className="mt-1 break-words">Incoming: {transfer.to_desc}</p>}
  </details>;
}

export default function Transfers() {
  const transfers = useAsync(signal => api.transfers(signal));
  const [reviewId, setReviewId] = useState<number | null>(null);
  const [show, setShow] = useState<"all" | "paired" | "identified">("all");
  const compact = useCompactLayout();
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const records = useRef<HTMLDivElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
  const [routesOpen, setRoutesOpen] = useState(false);
  useEffect(() => setPage(0), [show, search]);
  const [notice, setNotice] = useState<string | null>(null);
  const items: ExplainedTransfer[] = useMemo(
    () => transfers.data ?? [],
    [transfers.data],
  );
  const paired = useMemo(
    () => items.filter((t) => t.matched !== false),
    [items],
  );
  const identified = useMemo(
    () => items.filter((t) => t.matched === false),
    [items],
  );
  const routes = useMemo(() => {
    const m = new Map<
      string,
      { from: string; to: string; value: number; count: number }
    >();
    for (const t of paired) {
      const k = `${t.from}→${t.to}`;
      const r = m.get(k) ?? { from: t.from, to: t.to, value: 0, count: 0 };
      r.value = Math.round((r.value + t.amount) * 100) / 100;
      r.count += 1;
      m.set(k, r);
    }
    return [...m.values()].sort((a, b) => b.value - a.value);
  }, [paired]);

  if (transfers.loading && transfers.data === null) return <Spinner />;
  if (transfers.error && transfers.data === null)
    return <ErrorBox error={transfers.error} retry={transfers.refetch} />;
  const normalizedSearch = search.trim().toLowerCase();
  const hasFilters = show !== "all" || !!normalizedSearch;
  function clearFilters() { setShow("all"); setSearch(""); setPage(0); searchField.current?.focus(); }
  function changePage(next: number) {
    setPage(next);
    requestAnimationFrame(() => {
      records.current?.scrollTo({ top: 0 });
      records.current?.focus({ preventScroll: true });
      records.current?.scrollIntoView({ block: "start" });
    });
  }
  const filtered =
    (show === "paired" ? paired : show === "identified" ? identified : items).filter(item => !normalizedSearch || `${item.from} ${item.to} ${item.from_desc} ${item.to_desc}`.toLowerCase().includes(normalizedSearch));
  const currentPage = Math.min(page, Math.max(0, Math.ceil(filtered.length / 30) - 1));
  const displayed = filtered.slice(currentPage * 30, (currentPage + 1) * 30);

  return (
    <div className="space-y-5">
      {transfers.loading && <p role="status" className="text-xs text-mist">Updating transfers… Your review stays open.</p>}
      {transfers.error && <ErrorBox error={transfers.error} retry={transfers.refetch}/>}

      {notice && (
        <p role="status" className="text-sm text-kelp">
          {notice}
        </p>
      )}
      {reviewId !== null && (
        <TransactionReview
          id={reviewId}
          onClose={() => setReviewId(null)}
          onSaved={() => {
            transfers.refetch();
            setNotice(
              "Review saved. Transfer treatment and spending have been recalculated.",
            );
          }}
        />
      )}
      <div>
        <Heading level={2} variant="intro" >
          Transfers between accounts
        </Heading>
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-mist">
          Own-account movements are excluded from spending and income.
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-3 rounded-xl border border-line bg-white p-4 sm:grid-cols-3"><div><dt className="text-xs text-mist">Paired transfers</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{paired.length}</dd><p className="mt-1 text-xs text-mist">{routes.length} recorded account routes</p></div><div><dt className="text-xs text-mist">Without a paired entry</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{identified.length}</dd><p className="mt-1 text-xs text-mist">Supported own-account movements</p></div><div className="col-span-2 border-t border-line pt-3 sm:col-span-1 sm:border-0 sm:pt-0"><dt className="text-xs text-mist">Moved between accounts</dt><dd className="mt-1 text-xl font-semibold tabular-nums text-seablue">{money(paired.reduce((sum, t) => sum + t.amount, 0))}</dd><p className="mt-1 text-xs text-mist">Each matched pair counted once · saved history</p></div></dl>
      <Card className="overflow-hidden p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line p-4">
          <div>
            <Heading level={3} variant="subsection">Transfer entries</Heading>
            <p className="mt-1 text-xs text-mist">
              Open the evidence or review a classification.
            </p>
          </div>
          <label className="text-xs text-mist">
            Show{" "}
            <select
              aria-label="Transfer evidence"
              className="input ml-2"
              value={show}
              onChange={(e) => setShow(e.target.value as typeof show)}
            >
              <option value="all">All evidence</option>
              <option value="paired">Paired transfers</option>
              <option value="identified">Identified without a pair</option>
            </select>
          </label>
        </div>
        <div className="px-4 pb-3"><input ref={searchField} className="input w-full" type="search" aria-label="Search transfer accounts or descriptions" placeholder="Find an account or transfer description" value={search} onChange={event => setSearch(event.target.value)} /></div>
        {hasFilters && <div className="flex items-center justify-between gap-3 px-4 pb-3 text-xs text-mist"><span>{filtered.length} of {items.length} transfers</span><button className="btn" onClick={clearFilters}>Clear transfer filters</button></div>}
        {compact && <div ref={records} className="data-table-region max-h-[620px] overflow-auto" role="region" aria-label="Transfer records" tabIndex={0}>
          {displayed.map(t => <div key={t.group} className="space-y-3 border-t border-line p-4"><div className="flex justify-between gap-3 text-sm"><span className="text-mist">{shortDate(t.date)}</span><strong className="shrink-0 tabular-nums">{money(t.amount)}</strong></div><p className="break-words text-sm">{t.from} <span className="text-mist">→</span> {t.to}</p><TransferEvidence transfer={t} />{t.txn_id && <button className="btn w-full" aria-label={`Review transfer on ${t.date}`} onClick={() => setReviewId(t.txn_id!)}>Review transfer</button>}</div>)}
          {!filtered.length && <p className="p-5 text-sm text-mist">No transfers match this view.</p>}
        </div>}
        {!compact && <div ref={records} className="data-table-region max-h-[620px] overflow-auto" role="region" aria-label="Transfer records" tabIndex={0}>
          <DataTable className="w-full text-sm">
            <thead className="sticky top-0 bg-surface2 text-left text-xs uppercase tracking-wider text-mist">
              <tr>
                <th className="px-4 py-3">Date</th>
                <th className="px-4 py-3">Movement & evidence</th>
                <th className="px-4 py-3 text-right">Amount</th>
                <th className="px-4 py-3">Review</th>
              </tr>
            </thead>
            <tbody>
              {displayed.map((t) => (
                <tr key={t.group} className="border-t border-line">
                  <td className="whitespace-nowrap px-4 py-3 align-top text-mist">
                    {shortDate(t.date)}
                  </td>
                  <td className="min-w-64 px-4 py-3">
                    <p>
                      {t.from} <span className="text-mist">→</span> {t.to}
                    </p>
                    <div className="mt-0.5"><TransferEvidence transfer={t} /></div>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-right align-top font-medium tabular-nums">
                    {money(t.amount)}
                  </td>
                  <td className="px-4 py-3 align-top">
                    {t.txn_id && (
                      <button
                        className="btn text-xs"
                        aria-label={`Review transfer on ${t.date}`}
                        onClick={() => setReviewId(t.txn_id!)}
                      >
                        Review
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-10 text-center text-mist">
                    No transfers match this view.
                  </td>
                </tr>
              )}
            </tbody>
          </DataTable>
        </div>}
        <PageControls total={filtered.length} page={currentPage} size={30} onPage={changePage} label="Transfer pages" />
      </Card>
      <Disclosure title={<>Observed transfer routes <span className="ml-2 text-xs font-normal text-mist">{routes.length} routes · paired entries only</span></>} onToggle={event => setRoutesOpen(event.currentTarget.open)}><p className="text-xs leading-relaxed text-mist">Equal amounts in different accounts, close dates and supporting descriptions establish a pair. Unpaired movements are kept separate; these flows are not spending or new wealth.</p>{routesOpen && (routes.length ? <Suspense fallback={<Spinner label="Opening transfer routes…" />}><TransferSankey flows={routes} /></Suspense> : <p className="py-6 text-sm text-mist">No paired transfer routes are recorded.</p>)}</Disclosure>
    </div>
  );
}
