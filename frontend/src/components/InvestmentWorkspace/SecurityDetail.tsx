import { Heading } from "../Heading";
import { DataTable } from "../DataTable";
import { useEffect, useState } from "react";
import LedgerChart from "../LedgerChart";
import { fetchSecurityAnalysis, type AnalysedHolding, type InvestmentRange, type SecurityAnalysis } from "../../investmentAnalysisApi";
import { gainTone, signedPercent } from "../../investmentAnalysis";
import { isKnown, nativeMoney, portfolioDate, quantity } from "../../connectedPortfolio";
import { C, axisBase, tooltipBase } from "../../theme";
import { Card, Empty, Spinner } from "../ui";

export default function SecurityDetail({ holding, choices, currency, range, revision, onChoose }: {
  holding: AnalysedHolding; choices: AnalysedHolding[]; currency: string; range: InvestmentRange; revision: string; onChoose: (id: string) => void;
}) {
  const [result, setResult] = useState<{ key: string; data: SecurityAnalysis } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"price" | "return">("price");
  const [detailRange, setDetailRange] = useState<InvestmentRange>(range);
  const key = `${holding.account_id}|${holding.position_id}|${currency}|${detailRange}|${revision}`;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null);
    void fetchSecurityAnalysis(holding.account_id, holding.position_id, currency, detailRange, controller.signal).then((data) => {
      if (!controller.signal.aborted) setResult({ key, data });
    }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Price history is unavailable."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // Exact account, position, range and refresh revision identify this history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const data = result?.key === key ? result.data : null;
  const points = data?.points ?? [];
  const latest = [...points].reverse().find((point) => isKnown(point.close));
  const marketCurrency = data?.market_currency ?? holding.market_currency;
  const hasValues = points.some((point) => isKnown(mode === "price" ? point.close : point.return_pct));
  // A chart can start after the requested baseline (for example a recent IPO).
  // Its rebased change then describes only the available observations.
  const gain = data ? data.holding.period_return_pct : detailRange === range ? holding.period_return_pct : null;
  const chartGain = latest?.return_pct ?? null;
  const costLine = mode === "price" && marketCurrency === holding.currency && isKnown(holding.native_average_cost) && holding.native_average_cost > 0 ? holding.native_average_cost : null;
  const option = {
    animation: false, backgroundColor: "transparent", textStyle: { fontFamily: "system-ui, sans-serif" },
    aria: { enabled: true, description: `${holding.symbol} ${mode === "price" ? "market price" : "price return"} history. This is security market performance, not your personal investment return. Chart values are available below.` },
    tooltip: { ...tooltipBase, trigger: "axis", renderMode: "richText", formatter: (params: { dataIndex: number }[]) => {
      const point = points[params[0]?.dataIndex];
      return point ? `${portfolioDate(point.date)}\n${mode === "price" ? nativeMoney(point.close, marketCurrency) : signedPercent(point.return_pct)}\n${holding.symbol}` : "";
    } },
    grid: { left: 58, right: 18, top: 20, bottom: 34 },
    xAxis: { ...axisBase, type: "time", axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => new Date(value).toLocaleDateString("en-GB", { month: "short", ...(detailRange === "all" || detailRange === "1y" ? { year: "2-digit" as const } : { day: "numeric" as const }), timeZone: "UTC" }) } },
    yAxis: { ...axisBase, type: "value", scale: true, splitNumber: 4, axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => mode === "return" ? `${value.toFixed(0)}%` : new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 2 }).format(value) } },
    series: [{ type: "line", smooth: false, connectNulls: false, symbol: "circle", showSymbol: points.length < 3, symbolSize: 5,
      lineStyle: { width: 2.5, color: isKnown(chartGain) && chartGain < 0 ? C.coral : C.teal }, itemStyle: { color: isKnown(chartGain) && chartGain < 0 ? C.coral : C.teal },
      areaStyle: { color: isKnown(chartGain) && chartGain < 0 ? "rgba(181,71,71,0.05)" : "rgba(175,67,33,0.06)" },
      data: points.map((point) => [Date.parse(`${point.date.slice(0, 10)}T00:00:00Z`), mode === "price" ? point.close : point.return_pct]),
      ...(costLine !== null ? { markLine: { silent: true, symbol: "none", lineStyle: { type: "solid", color: C.mist, opacity: 0.5 }, label: { show: false }, data: [{ yAxis: costLine }] } } : {}),
    }],
  };
  return <Card className="!p-0 overflow-hidden">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
      <label className="min-w-0 flex-1 basis-full sm:basis-40 sm:max-w-md"><span className="sr-only">Security to chart</span><select aria-label="Security to chart" className="input w-full font-medium" value={holding.id} onChange={(event) => onChoose(event.target.value)}>{choices.filter((choice) => !choice.is_cash).map((choice) => <option key={choice.id} value={choice.id}>{choice.symbol} · {choice.account_name}</option>)}</select></label>
      <select aria-label="Security history period" className="input w-28 text-xs" value={detailRange} onChange={(event) => setDetailRange(event.target.value as InvestmentRange)}>{([['1w', '1 week'], ['1m', '1 month'], ['3m', '3 months'], ['6m', '6 months'], ['ytd', 'This year'], ['1y', '1 year'], ['all', 'All history']] as const).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
      <div className="flex rounded-lg bg-surface2 p-1" role="group" aria-label="Security chart measure">{(["price", "return"] as const).map((value) => <button key={value} className={`min-h-11 rounded-md px-3 text-xs ${mode === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`} aria-pressed={mode === value} onClick={() => setMode(value)}>{value === "price" ? "Price" : "Price return"}</button>)}</div>
    </div>
    <div className="min-w-0">
      <div className="min-w-0 px-4 pb-2 pt-4 sm:px-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2"><div><Heading level={2} variant="section" className="text-mist">{holding.name || holding.symbol}</Heading><p className="mt-1 text-2xl font-semibold tabular-nums">{mode === "price" ? nativeMoney(latest?.close, marketCurrency) : signedPercent(gain)}</p></div><div className="text-right"><p className={`text-sm font-medium tabular-nums ${gainTone(gain)}`}>{signedPercent(gain)}<span className="ml-1 text-xs font-normal text-mist">period price return</span></p><p className="mt-1 text-xs text-mist">{portfolioDate(holding.market_as_of)} · {holding.market_symbol || holding.symbol}</p></div></div>
        {loading ? <div className="min-h-[220px]"><Spinner label="Loading security history…" /></div> : error ? <p role="status" className="flex min-h-[220px] items-center text-sm text-amber">{error}</p> : !hasValues ? <div className="flex min-h-[220px] items-center justify-center"><Empty>No supported market history is saved for this security yet.</Empty></div> : <LedgerChart option={option} notMerge style={{ height: 220, width: "100%", minWidth: 0 }} />}
        <p className="pb-2 text-xs text-mist">Price movement excludes dividends and FX.{costLine !== null ? " Grey reference line: your reported average cost." : ""}</p>
        {data && points.length > 0 && <p className="pb-2 text-xs text-mist">Available observations: {portfolioDate(points[0].date)} – {portfolioDate(points[points.length - 1].date)}.{!isKnown(gain) ? " Requested-period return is unavailable; chart returns start at the first available observation." : ""}</p>}
      </div>
      <details open className="border-t border-line px-5 text-xs"><summary className="cursor-pointer py-3 font-medium">Your position · {nativeMoney(holding.value, currency)}</summary><dl className="grid grid-cols-2 gap-x-5 gap-y-4 pb-4 text-xs"><div><dt className="text-mist">Value · {currency}</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{nativeMoney(holding.value, currency)}</dd></div><div><dt className="text-mist">Unrealised gain</dt><dd className={`mt-1 font-semibold tabular-nums ${gainTone(holding.unrealized_pnl)}`}>{nativeMoney(holding.unrealized_pnl, currency)}<span className="ml-1 text-xs font-normal">{signedPercent(holding.unrealized_pct)}</span></dd></div><div><dt className="text-mist">Quantity / average cost</dt><dd className="mt-1 tabular-nums">{quantity(holding.quantity)} / {nativeMoney(holding.native_average_cost, holding.currency)}</dd></div><div><dt className="text-mist">Sector</dt><dd className="mt-1">{holding.sector || "Unclassified"}</dd></div></dl>
    {data && <details className="border-t border-line px-5 text-xs text-mist"><summary className="cursor-pointer py-3">Price observations &amp; source</summary><div className="space-y-2 pb-3">{data.warnings.map((warning, index) => <p key={index}>{warning}</p>)}<p>The latest daily bar may still represent an ongoing session. Security history can include periods before you owned this holding.</p>{data.source_url?.startsWith("https://") && <a href={data.source_url} target="_blank" rel="noreferrer" className="inline-block text-teal underline">Market data source</a>}<div className="max-h-56 overflow-auto"><DataTable className="w-full text-left"><thead><tr><th className="py-2 font-medium">Date</th><th className="py-2 text-right font-medium">Price ({marketCurrency || "unknown"})</th><th className="py-2 text-right font-medium">Price return</th></tr></thead><tbody>{points.map((point) => <tr key={point.date} className="border-t border-line"><td className="py-2">{portfolioDate(point.date)}</td><td className="py-2 text-right tabular-nums">{nativeMoney(point.close, marketCurrency)}</td><td className="py-2 text-right tabular-nums">{signedPercent(point.return_pct)}</td></tr>)}</tbody></DataTable></div></div></details>}
      </details>
    </div>
  </Card>;
}
