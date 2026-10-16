import { Heading } from "../Heading";
import { useEffect, useState } from "react";
import { fetchInvestmentInsights, type AnalysedAccount, type AnalysedHolding, type InvestmentInsightAction, type InvestmentInsightsData } from "../../investmentAnalysisApi";
import { portfolioDate } from "../../connectedPortfolio";
import { Card, Spinner } from "../ui";

export default function InvestmentInsights({ accountIds, currency, revision, holdings, accounts, onAction }: { accountIds: string[]; currency: string; revision: string; holdings: AnalysedHolding[]; accounts: AnalysedAccount[]; onAction: (action: InvestmentInsightAction) => void }) {
  const [result, setResult] = useState<{ key: string; data: InvestmentInsightsData } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const key = `${accountIds.join("|")}|${currency}|${revision}|${retry}`;
  useEffect(() => {
    const controller = new AbortController(); setError(null);
    void fetchInvestmentInsights(accountIds, currency, controller.signal).then((data) => { if (!controller.signal.aborted) setResult({ key, data }); }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Investment review is unavailable."); });
    return () => controller.abort();
    // Account scope and refresh revision are represented by key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const data = result?.key === key ? result.data : null;
  if (error) return <Card><p role="alert" className="text-sm text-amber">{error}</p><button className="btn mt-3" onClick={() => setRetry((value) => value + 1)}>Retry review</button></Card>;
  if (!data) return <Spinner label="Checking saved investment observations…" />;
  const subjects = (item: InvestmentInsightsData["items"][number]) => {
    const positions = item.holding_ids.map((id) => holdings.find((holding) => holding.id === id)).filter((holding): holding is AnalysedHolding => !!holding);
    return positions.length ? [...new Set(positions.map((holding) => `${holding.symbol} · ${holding.account_name}`))] : item.account_ids.map((id) => accounts.find((account) => account.id === id)).filter((account): account is AnalysedAccount => !!account).map((account) => `${account.name} · ${account.institution}`);
  };
  const groups = [["quality", "Data to check"], ["movement", "Unusual price movements"], ["context", "Portfolio context"]] as const;
  return <div className="space-y-4">
    <Card><div className="flex flex-wrap items-baseline justify-between gap-2"><Heading level={2} variant="section">Investment review</Heading><span className="text-xs text-mist">{data.summary.review_count ? `${data.summary.review_count} findings to review` : "No current review findings"}</span></div><p className="mt-2 text-xs text-mist">{data.coverage.verified_market_holdings} / {data.coverage.holdings} holdings with verified market data · {data.coverage.usable_cost_holdings} with usable cost · {data.coverage.known_accounts} / {data.coverage.accounts} known account values</p></Card>
    {groups.map(([category, title]) => {
      const items = data.items.filter((item) => item.category === category);
      if (!items.length) return null;
      return <Card key={category} className="!p-0 overflow-hidden"><Heading level={3} variant="subsection" className="border-b border-line px-4 py-3">{title}</Heading><div className="divide-y divide-line">{items.map((item) => <article key={item.id} className="px-4 py-4"><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0 flex-1 basis-64"><Heading level={4} variant="subsection" className={`${item.severity === "warning" ? "text-amber" : "text-foam"}`}>{item.title}</Heading>{subjects(item).length > 0 && <p className="mt-1 text-xs font-medium text-foam" title={subjects(item).join("; ")}>{subjects(item).slice(0, 3).join("; ")}{subjects(item).length > 3 ? `; +${subjects(item).length - 3} more` : ""}</p>}<p className="mt-1 text-xs leading-relaxed text-mist">{item.detail}</p></div><button className="btn text-xs" onClick={() => onAction(item.action)}>{item.action.label} <span aria-hidden="true">↗</span></button></div>{item.evidence.length > 0 && <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-mist">{item.evidence.map((evidence, index) => <div key={index} className="flex gap-1"><dt>{evidence.label}:</dt><dd className="font-medium text-foam">{evidence.value === null ? "Unknown" : evidence.unit === "percent" ? `${evidence.value.toFixed(1)}%` : `${new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 }).format(evidence.value)}${evidence.unit === "days" ? " days" : ""}`}</dd>{evidence.as_of && <time className="text-mist" dateTime={evidence.as_of} title={portfolioDate(evidence.as_of, true)}>· {portfolioDate(evidence.as_of)}</time>}</div>)}</dl>}</article>)}</div></Card>;
    })}
    {!data.summary.movement_count && <p className="text-xs text-mist">No unusual movement was identified in the available price histories. {data.coverage.movement_eligible_instruments} / {data.coverage.unique_instruments} instruments have enough observations for that check.</p>}
    <details className="rounded-xl border border-line bg-white px-4 text-xs text-mist"><summary className="cursor-pointer py-3">Review evidence &amp; limits</summary><div className="space-y-2 pb-4"><p>Checked on {portfolioDate(data.as_of, true)}. Portfolio snapshot {portfolioDate(data.retrieved_at, true)}. Findings describe the available data; they are not trading recommendations.</p>{data.methodology.map((method, index) => <p key={index}>{method}</p>)}</div></details>
  </div>;
}
