import { Heading } from "../Heading";
import { useEffect, useState } from "react";
import { fetchInvestmentCoverage, type InvestmentCoverage as Coverage } from "../../investmentCoverageApi";
import { portfolioDate } from "../../connectedPortfolio";
import { Card, Empty, SectionTitle, Spinner } from "../ui";

const labels: Record<string, string> = { positions: "Holdings", balances: "Cash balances", activities: "Activity", history: "Account history", return_rates: "Broker returns", orders: "Orders" };
function status(section: Coverage["accounts"][number]["sections"][number]) {
  const label = section.status === "unsupported" ? "Not supported" : section.status === "error" ? "Retrieval incomplete"
    : section.status === "empty" ? "No records returned" : `${section.rows} records`;
  return `${label}${section.truncated ? " · truncated" : ""}${section.retained ? " · retained data" : ""}`;
}

export default function InvestmentCoverage({ accountIds, currency, revision }: { accountIds: string[]; currency: string; revision: string }) {
  const [result, setResult] = useState<{ key: string; data: Coverage } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const key = JSON.stringify([accountIds, currency, revision, retry]);
  useEffect(() => {
    const controller = new AbortController(); setError(null);
    void fetchInvestmentCoverage(accountIds, currency, controller.signal)
      .then(data => { if (!controller.signal.aborted) setResult({ key, data }); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Coverage could not be loaded."); });
    return () => controller.abort();
    // All request inputs are represented by key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const data = result?.key === key ? result.data : null;
  if (error) return <Card><p role="alert" className="text-sm text-amber">{error}</p><button className="btn mt-3" onClick={() => setRetry(value => value + 1)}>Retry coverage</button></Card>;
  if (!data) return <Spinner label="Loading investment coverage…" />;
  return <div className="space-y-4">
    <Card><SectionTitle>Connection data coverage</SectionTitle><p className="text-sm text-mist">Source dates and saved history for this selection. Missing information remains unknown.</p>
      {!data.accounts.length ? <Empty>No accounts selected.</Empty> : <div className="mt-5 space-y-6">{data.accounts.map(account => <section key={account.id} className="min-w-0 border-b border-line pb-5 last:border-0 last:pb-0">
        <div className="flex flex-wrap items-baseline justify-between gap-2"><Heading level={3} variant="subsection" className="break-words">{account.name} · {account.institution}</Heading><p className={`text-xs ${account.connection_disabled || !account.account_value_known ? "text-amber" : "text-mist"}`}>{account.connection_disabled ? "Connection disabled · " : ""}{account.account_value_known ? "Account total available" : "Account total unknown"}{account.is_paper ? " · simulated account" : ""}</p></div>
        <p className="mt-1 text-xs text-mist">Initial sync: {account.initial_sync_complete === true ? "complete" : account.initial_sync_complete === false ? "in progress" : "not reported"}</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{account.sections.map(section => <div key={section.key} className="min-w-0 rounded-lg bg-surface2/65 p-3">
          <Heading level={4} variant="subsection">{labels[section.key] || section.key}</Heading><p className={`mt-1 text-xs ${section.retained || section.truncated || section.status === "error" ? "text-amber" : "text-mist"}`}>{status(section)}</p>
          <dl className="mt-2 space-y-1 text-xs text-mist"><div><dt className="inline">Source synced: </dt><dd className="inline">{portfolioDate(section.source_at, true)}{section.source_outdated ? " · check age" : ""}</dd></div><div><dt className="inline">Received: </dt><dd className="inline">{portfolioDate(section.retrieved_at, true)}</dd></div>{section.first_date && <div><dt className="inline">Records dated: </dt><dd className="inline">{portfolioDate(section.first_date)} – {portfolioDate(section.last_date)}</dd></div>}</dl>
          {section.error && <p className="mt-2 text-xs text-mist">{section.error}</p>}
        </div>)}</div>
        <details className="mt-3 rounded-lg border border-line px-3 text-xs"><summary className="cursor-pointer py-3">Saved coverage timeline · {account.activity_archive?.stored_activities ?? 0} activity records</summary><div className="space-y-4 pb-3">
          <div><Heading level={4} variant="subsection">Activity collection</Heading>{account.activity_archive ? <><p className="mt-1 text-mist">Records dated {portfolioDate(account.activity_archive.first_activity_at)} – {portfolioDate(account.activity_archive.last_activity_at)}. {account.activity_archive.partial_or_failed_requests} partial or failed requests recorded.</p>{account.activity_archive.fully_fetched_windows.length ? <ul className="mt-2 space-y-1 text-mist">{account.activity_archive.fully_fetched_windows.map((window, index) => <li key={`${window.retrieved_at}-${index}`}>{portfolioDate(window.start)} – {portfolioDate(window.end)} · fetched {portfolioDate(window.retrieved_at, true)}</li>)}</ul> : <p className="mt-1 text-mist">No fully fetched date window has been recorded. Imported cache rows do not establish a past collection window.</p>}<p className="mt-2 text-mist">Fetched windows show completed requests; they do not prove the broker returned every transaction.</p></> : <p className="mt-1 text-mist">No activity has been archived for this account yet.</p>}</div>
          <div><Heading level={4} variant="subsection">Recent saved account observations</Heading>{account.observations.length ? <ul className="mt-2 space-y-2 text-mist">{account.observations.map((observation, index) => <li key={`${observation.retrieved_at}-${index}`}>Received {portfolioDate(observation.retrieved_at, true)} · source {observation.source_time_known ? portfolioDate(observation.observed_at, true) : "date unknown"} · {observation.holdings_status}{observation.holdings_truncated ? " · truncated" : ""}</li>)}</ul> : <p className="mt-1 text-mist">No saved account observations.</p>}</div>
        </div></details>
      </section>)}</div>}
    </Card>
    <Card><Heading level={3} variant="subsection">Personal return coverage</Heading><p className="mt-2 text-xs leading-relaxed text-mist">{data.performance.reason}</p><details className="mt-3 text-xs text-mist"><summary className="cursor-pointer py-2">Coverage definitions</summary><div className="space-y-2 pb-2">{data.methodology.map((line, index) => <p key={index}>{line}</p>)}</div></details></Card>
  </div>;
}
