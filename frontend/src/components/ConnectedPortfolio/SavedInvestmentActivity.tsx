import { useEffect, useState } from "react";
import { fetchSavedActivities, fetchSavedActivityAccounts, type SavedActivities, type SavedActivityAccount } from "../../investmentCoverageApi";
import type { ConnectedAccount } from "../../snaptradeApi";
import { nativeMoney, portfolioDate, quantity } from "../../connectedPortfolio";
import { Card, Empty, SectionTitle, Spinner } from "../ui";
import { DataTable, TableMeta } from "../DataTable";
import { TableRegion } from "./common";

export default function SavedInvestmentActivity({ accounts, scope, revision }: { accounts: ConnectedAccount[]; scope: string; revision: string }) {
  const [listing, setListing] = useState<{ key: string; accounts: SavedActivityAccount[] } | null>(null);
  const [result, setResult] = useState<{ key: string; data: SavedActivities } | null>(null);
  const [selected, setSelected] = useState<{ scope: string; id: string }>({ scope, id: "" });
  const [page, setPage] = useState<{ account: string; offset: number }>({ account: "", offset: 0 });
  const [listError, setListError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const listKey = JSON.stringify([revision, retry]);
  useEffect(() => {
    const controller = new AbortController(); setListError(null);
    void fetchSavedActivityAccounts(controller.signal).then(values => { if (!controller.signal.aborted) setListing({ key: listKey, accounts: values }); })
      .catch(reason => { if (!controller.signal.aborted) setListError(reason instanceof Error ? reason.message : "Saved accounts could not be loaded."); });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listKey]);
  const ready = listing?.key === listKey;
  const archived = ready ? listing.accounts.filter(account => scope === "all" ? !account.is_paper : scope === "paper" ? account.is_paper : account.id === scope) : [];
  const choices = [...archived, ...accounts.filter(account => !archived.some(saved => saved.id === account.id)).map(account => ({ id: account.id, name: account.name, institution: account.institution, is_paper: !!account.is_paper, stored_activities: 0 }))];
  const chosen = selected.scope === scope && choices.some(account => account.id === selected.id) ? selected.id : choices[0]?.id ?? "";
  const pageAccount = JSON.stringify([scope, chosen]);
  const offset = page.account === pageAccount ? page.offset : 0;
  const key = JSON.stringify([chosen, offset, listKey, scope]);
  useEffect(() => {
    const controller = new AbortController(); setError(null);
    if (ready && chosen) void fetchSavedActivities(chosen, offset, controller.signal)
      .then(data => { if (!controller.signal.aborted) setResult({ key, data }); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Saved activity could not be loaded."); });
    return () => controller.abort();
    // All request inputs are represented by key and ready.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready]);
  const data = result?.key === key ? result.data : null;
  return <Card><SectionTitle>Saved investment activity</SectionTitle><p className="text-sm leading-relaxed text-mist">Activity collected by Am I Broke? stays available after it leaves the provider’s recent window. These records do not establish complete lifetime history.</p>
    {listError ? <div className="mt-4"><p role="alert" className="text-sm text-amber">{listError}</p><button className="btn mt-3" onClick={() => setRetry(value => value + 1)}>Retry saved history</button></div> : !ready ? <Spinner label="Loading saved activity accounts…" /> : !choices.length ? <Empty>No saved activity accounts in this selection.</Empty> : <>
      <label className="field-label mt-4 max-w-xl">Saved activity account<select className="input" value={chosen} onChange={event => { setSelected({ scope, id: event.target.value }); setPage({ account: "", offset: 0 }); }}>{choices.map(account => <option key={account.id} value={account.id}>{account.name} · {account.institution}{accounts.some(current => current.id === account.id) ? "" : " · archived"}{account.is_paper ? " · paper" : ""}</option>)}</select></label>
      {error ? <div className="mt-4"><p role="alert" className="text-sm text-amber">{error}</p><button className="btn mt-3" onClick={() => setRetry(value => value + 1)}>Retry saved history</button></div> : !data ? <Spinner label="Loading saved investment activity…" /> : <>
        <p className="my-4 text-xs text-mist">{data.pagination.total} saved records · {portfolioDate(data.coverage.first_activity_at)} – {portfolioDate(data.coverage.last_activity_at)}</p>
        {!data.activities.length ? <Empty>No activity has been saved for this account on this page.</Empty> : <TableRegion label="Saved investment activity"><DataTable className="min-w-[660px]"><thead><tr><th scope="col">Date</th><th scope="col">Activity</th><th scope="col">Symbol</th><th scope="col" className="text-right">Quantity</th><th scope="col" className="text-right">Amount</th></tr></thead><tbody>{data.activities.map(activity => <tr key={activity.id}><td className="whitespace-nowrap text-xs text-mist">{portfolioDate(activity.date)}</td><td className="max-w-80"><span className="font-medium">{(activity.type || "OTHER").replace(/_/g, " ")}</span><TableMeta>{activity.description || "No description reported"}</TableMeta></td><td>{activity.symbol || "—"}</td><td className="text-right tabular-nums">{quantity(activity.quantity)}</td><td className="whitespace-nowrap text-right tabular-nums">{nativeMoney(activity.amount, activity.currency)}</td></tr>)}</tbody></DataTable></TableRegion>}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs"><p className="text-mist">Showing {data.pagination.total ? offset + 1 : 0}–{offset + data.activities.length} of {data.pagination.total}</p><div className="flex gap-2"><button className="btn" disabled={offset === 0} onClick={() => setPage({ account: pageAccount, offset: Math.max(0, offset - 25) })}>Previous saved activity</button><button className="btn" disabled={!data.pagination.has_more} onClick={() => setPage({ account: pageAccount, offset: offset + 25 })}>Next saved activity</button></div></div>
        {data.coverage.limitations.length > 0 && <details className="mt-3 text-xs text-mist"><summary className="cursor-pointer py-2">Activity coverage limits</summary><div className="space-y-2 pb-2">{data.coverage.limitations.map((line, index) => <p key={index}>{line}</p>)}</div></details>}
      </>}
    </>}
  </Card>;
}
