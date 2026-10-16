import { Heading } from "../Heading";
import { useEffect, useState } from "react";
import LedgerChart from "../LedgerChart";
import { fetchInvestmentBenchmarks, fetchSecurityAnalysis, type AnalysedHolding, type BenchmarkSeries, type InvestmentRange } from "../../investmentAnalysisApi";
import { rebaseCommonHistory, signedPercent, type PriceHistoryForComparison } from "../../investmentAnalysis";
import { portfolioDate } from "../../connectedPortfolio";
import { C, axisBase, tooltipBase } from "../../theme";
import { Card, Empty, Spinner } from "../ui";

export default function SecurityComparison({ holdings, currency, range, revision }: { holdings: AnalysedHolding[]; currency: string; range: InvestmentRange; revision: string }) {
  const unique = new Map<string, AnalysedHolding>();
  for (const holding of holdings) if (!holding.is_cash) {
    const key = holding.instrument_id || holding.market_symbol || holding.id;
    if (!unique.has(key)) unique.set(key, holding);
  }
  const choices = [...unique.values()];
  const [selection, setSelection] = useState<string[] | null>(null);
  const [benchmarkIds, setBenchmarkIds] = useState<string[]>([]);
  const [benchmarkResult, setBenchmarkResult] = useState<{ key: string; rows: BenchmarkSeries[] } | null>(null);
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null);
  const [result, setResult] = useState<{ key: string; rows: PriceHistoryForComparison[]; errors: string[] } | null>(null);
  const [search, setSearch] = useState("");
  const benchmarkKey = `${range}|${revision}`;
  const benchmarks = benchmarkResult?.key === benchmarkKey ? benchmarkResult.rows : [];
  const ids = (selection ?? choices.slice(0, 2).map((holding) => holding.id)).filter((id) => choices.some((holding) => holding.id === id));
  const chosenBenchmarks = benchmarks.filter((benchmark) => benchmarkIds.includes(benchmark.id));
  // Benchmarks are already loaded separately. Adding one only recomposes the
  // chart and must not discard or re-fetch unchanged security histories.
  const key = `${ids.join("|")}|${currency}|${range}|${revision}`;
  useEffect(() => {
    const controller = new AbortController();
    setBenchmarkError(null);
    void fetchInvestmentBenchmarks(range, controller.signal).then((rows) => { if (!controller.signal.aborted) { setBenchmarkResult({ key: benchmarkKey, rows }); setBenchmarkError(null); } }).catch(() => { if (!controller.signal.aborted) setBenchmarkError("Benchmark histories are not available in the local cache."); });
    return () => controller.abort();
  }, [range, revision, benchmarkKey]);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled(ids.map(async (id) => {
      const holding = choices.find((choice) => choice.id === id)!;
      const detail = await fetchSecurityAnalysis(holding.account_id, holding.position_id, currency, range, controller.signal);
      return { id: holding.id, name: holding.symbol, currency: detail.market_currency, points: detail.points, asOf: detail.holding.market_as_of, status: detail.holding.market_status, sourceUrl: detail.source_url };
    })).then((values) => {
      if (controller.signal.aborted) return;
      const rows: PriceHistoryForComparison[] = [], errors: string[] = [];
      values.forEach((value, index) => value.status === "fulfilled" ? rows.push(value.value) : errors.push(`${choices.find((choice) => choice.id === ids[index])?.symbol || "A security"}: price history unavailable.`));
      setResult({ key, rows, errors });
    });
    return () => controller.abort();
    // The key includes the selected security identities and exact period.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const rows = result?.key === key ? [...result.rows, ...chosenBenchmarks.map((benchmark) => ({ id: benchmark.id, name: benchmark.name, currency: benchmark.currency, points: benchmark.points, asOf: benchmark.as_of, status: benchmark.status, sourceUrl: benchmark.source_url }))] : [];
  const rebased = rebaseCommonHistory(rows);
  const colors = [C.teal, C.cyan, C.kelp, C.seablue];
  const option = {
    animation: false, color: colors, textStyle: { fontFamily: "system-ui, sans-serif" },
    aria: { enabled: true, description: "Native-currency security and index price returns, rebased to zero on the first shared trading date. This does not measure your investment return or include exchange-rate changes." },
    tooltip: { ...tooltipBase, trigger: "axis", renderMode: "richText", valueFormatter: (value: number | number[]) => signedPercent(Array.isArray(value) ? value[1] : value) },
    legend: { type: "scroll", top: 0, left: 0, right: 0, textStyle: { color: C.mist, fontSize: 11 } },
    grid: { top: 42, left: 54, right: 18, bottom: 35 },
    xAxis: { ...axisBase, type: "time", axisLabel: { ...axisBase.axisLabel, hideOverlap: true, formatter: (date: number) => new Date(date).toLocaleDateString("en-GB", { month: "short", ...(range === "all" || range === "1y" ? { year: "2-digit" as const } : { day: "numeric" as const }), timeZone: "UTC" }) } },
    yAxis: { ...axisBase, type: "value", splitNumber: 4, axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => `${value.toFixed(0)}%` } },
    series: rebased.series.map((series, seriesIndex) => ({ name: `${series.name} · ${series.currency || "currency unknown"}`, type: "line", smooth: false, connectNulls: false, showSymbol: rebased.dates.length < 3, symbol: ["circle", "diamond", "rect", "triangle"][seriesIndex], lineStyle: { width: 2.2, type: "solid" }, data: series.values.map((value, index) => [Date.parse(`${rebased.dates[index]}T00:00:00Z`), value]) })),
  };
  return <Card className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><Heading level={2} variant="section">Compare price trends</Heading><p className="mt-1 text-xs text-mist">Up to 3 securities and 4 total series · native price returns</p></div><span className="text-xs text-mist">{ids.length + chosenBenchmarks.length} selected</span></div>
    <details className="rounded-lg border border-line px-3"><summary className="cursor-pointer py-3 text-sm font-medium">Choose securities &amp; benchmarks</summary><div className="space-y-3 pb-3"><input type="search" aria-label="Find comparison security" className="input w-full" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find a company or symbol" /><fieldset className="grid max-h-56 gap-2 overflow-y-auto sm:grid-cols-2"><legend className="sr-only">Securities to compare</legend>{choices.filter((holding) => `${holding.symbol} ${holding.name}`.toLowerCase().includes(search.toLowerCase())).map((holding) => <label className="flex items-start gap-2 p-2 text-xs" key={holding.id}><input type="checkbox" className="mt-0.5 h-4 w-4 accent-teal" checked={ids.includes(holding.id)} disabled={!ids.includes(holding.id) && (ids.length >= 3 || ids.length + chosenBenchmarks.length >= 4)} onChange={(event) => setSelection(event.target.checked ? [...ids, holding.id] : ids.filter((id) => id !== holding.id))} /><span><strong>{holding.symbol}</strong> · {holding.name}<span className="mt-1 block text-xs text-mist">{holding.market_currency || holding.currency || "Currency unknown"}</span></span></label>)}</fieldset>
      {benchmarks.length > 0 && <fieldset className="flex flex-wrap gap-3 border-t border-line pt-3"><legend className="pt-2 text-xs font-medium">Benchmarks</legend>{benchmarks.map((benchmark) => <label key={benchmark.id} className="flex items-center gap-2 text-xs"><input type="checkbox" className="h-4 w-4 accent-teal" checked={benchmarkIds.includes(benchmark.id)} disabled={!benchmarkIds.includes(benchmark.id) && (ids.length + chosenBenchmarks.length >= 4 || benchmark.points.length < 2)} onChange={(event) => setBenchmarkIds(event.target.checked ? [...benchmarkIds, benchmark.id] : benchmarkIds.filter((id) => id !== benchmark.id))} />{benchmark.name}{benchmark.points.length < 2 ? " · no data" : ""}</label>)}</fieldset>}{benchmarkError && <p className="text-xs text-mist">{benchmarkError}</p>}
    </div></details>
    {result?.key !== key ? <Spinner label="Loading comparison histories…" /> : !rebased.series.length ? <Empty>{ids.length + chosenBenchmarks.length < 1 ? "Choose a security to begin." : "The selected series need at least two shared price dates to compare."}</Empty> : <><LedgerChart option={option} notMerge style={{ height: 310, width: "100%", minWidth: 0 }} /><p className="text-xs text-mist">Common dates: {portfolioDate(rebased.dates[0])} – {portfolioDate(rebased.dates[rebased.dates.length - 1])}. Each line starts at 0%; dividends and FX movements are excluded.</p><dl className="grid gap-3 sm:grid-cols-2">{rebased.series.map((series, index) => <div key={series.id} className="flex items-center justify-between gap-2 rounded-lg bg-surface2/50 px-3 py-2 text-sm"><dt className="flex items-center gap-2"><span className="h-2 w-2 rounded-full" style={{ background: colors[index % colors.length] }} /><span>{series.name}<span className="mt-1 block text-[10px] font-normal text-mist">{series.currency || "Currency unknown"} · {portfolioDate(series.asOf ?? null)}{series.status && series.status !== "ok" ? ` · ${series.status.replace(/_/g, " ")}` : ""}</span></span></dt><dd className="font-semibold tabular-nums">{signedPercent(series.values[series.values.length - 1])}</dd></div>)}</dl></>}
    {result?.errors.map((error, index) => <p className="text-xs text-amber" key={index}>{error}</p>)}
  </Card>;
}
