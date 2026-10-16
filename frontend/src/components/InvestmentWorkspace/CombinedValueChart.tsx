import { useMemo, useRef, useState } from "react";
import type ReactECharts from "echarts-for-react/esm/core";
import type { WealthData } from "../../wealthApi";
import type { MarketValueRange } from "../../marketValueApi";
import { historyTimestamp, investmentValueChartData } from "../../investmentValueChart";
import { marketAxisLabel, marketValueDate, marketValueTimestamp } from "../../marketValue";
import { compactWealth, wealthChartHistory, wealthHistoryConnectorSeries, wealthHistoryDateLabel, wealthHistoryNavigator } from "../../wealth";
import { nativeMoney, portfolioDate } from "../../connectedPortfolio";
import { C, axisBase, tooltipBase } from "../../theme";
import LedgerChart from "../LedgerChart";
import { DataTable } from "../DataTable";
import { Empty, useCompactLayout } from "../ui";
import useMarketValue from "./useMarketValue";
import type { InvestmentRange } from "../../investmentAnalysisApi";
import type { ReactNode } from "react";

type Props = {
  accountIds: string[]; currency: string; revision: string; range: InvestmentRange;
  historyData: WealthData; showRecent: boolean; notes?: ReactNode;
};
type Observation = { timestamp: number; history?: number; recent?: number };
const colour = { history: C.seablue, recent: C.teal };

export default function CombinedValueChart({ accountIds, currency, revision, range, historyData, showRecent, notes }: Props) {
  const history = useMemo(() => wealthChartHistory(historyData), [historyData]);
  const continuity = historyData.history?.continuity;
  const lastGap = continuity?.gaps[continuity.gaps.length - 1];
  const comparisonIds = useMemo(() => history.account_ids
    .filter(id => id.startsWith("connected:")).map(id => id.slice("connected:".length))
    .filter(id => accountIds.includes(id)), [accountIds, history]);
  const marketRange: MarketValueRange = range === "1w" ? "1w" : "1m";
  const market = useMarketValue({ accountIds: comparisonIds, currency, revision, range: marketRange, interval: "1h", enabled: showRecent && comparisonIds.length > 0 });
  const compact = useCompactLayout();
  const chart = useRef<ReactECharts>(null);
  const pointerStart = useRef<{ x: number; y: number } | null>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const [zoom, setZoom] = useState({ start: 0, end: 100 });
  const end = Math.max(marketValueTimestamp(historyData.generated_at) ?? 0, marketValueTimestamp(market.data?.generated_at) ?? 0) || null;
  const connectors = useMemo(() => wealthHistoryConnectorSeries(history.points, colour.history, point => historyTimestamp(point.date, point.observed_at, end ?? undefined, true)), [history.points, end]);
  const firstHistoryDate = history.points[0]?.date;
  const historyStart = firstHistoryDate && /^\d{4}-\d{2}-\d{2}$/.test(firstHistoryDate) ? `${firstHistoryDate}T00:00:00Z` : firstHistoryDate;
  const starts = [marketValueTimestamp(historyStart), marketValueTimestamp(market.data?.points[0]?.timestamp)].filter((value): value is number => value !== null);
  const start = starts.length ? Math.min(...starts) : null;
  const model = useMemo(() => investmentValueChartData(history.points, market.data?.points ?? [], { start: start ?? undefined, end: end ?? undefined, dailyAtEnd: true }), [history, market.data, start, end]);
  const displayModel = model;
  const plotStart = start ?? undefined;
  const observations = useMemo(() => {
    const values = new Map<number, Observation>();
    for (const series of model.series) for (const [timestamp, value] of series.data) {
      if (value === null) continue;
      const point = values.get(timestamp) ?? { timestamp };
      point[series.kind] = value;
      values.set(timestamp, point);
    }
    return [...values.values()].sort((a, b) => a.timestamp - b.timestamp);
  }, [model]);
  const low = plotStart ?? model.start ?? 0, high = end ?? model.end ?? low;
  const visible = observations.filter((point) => point.timestamp >= low + (high - low) * zoom.start / 100 && point.timestamp <= low + (high - low) * zoom.end / 100);
  const selected = visible.findIndex((point) => point.timestamp === focus);
  const focusIndex = selected < 0 ? visible.length - 1 : selected;
  const focused = visible[focusIndex];
  const option = useMemo(() => {
    const amounts = displayModel.series.flatMap((series) => series.data.flatMap(([, value]) => value === null ? [] : [value]));
    const span = amounts.length ? Math.max(...amounts) - Math.min(...amounts) : 0;
    const axisStep = span ? span / 6 : Math.max(Math.abs(amounts[0] ?? 0) * .02, .01);
    return {
      animation: false, useUTC: true,
      aria: { enabled: true, description: `Portfolio value in ${currency}. Blue: account history including cash flows. Blue dashed links bridge missing dates or a change in valuation source; no values are inferred between their endpoints. Terracotta dashed line: today's holdings valued using recent prices, an estimate. Changed account coverage stays unconnected. Use the observation slider or arrow keys to inspect dated values.` },
      tooltip: { ...tooltipBase, trigger: "axis", renderMode: "richText", textStyle: { fontSize: compact ? 11 : 12 }, axisPointer: { type: "line", snap: true }, formatter: (params: { value: unknown; seriesId?: string }[]) => {
        const time = params.find((param) => displayModel.series.some(series => series.id === param.seriesId) && Array.isArray(param.value) && param.value[1] !== null)?.value;
        const observation = Array.isArray(time) ? observations.find((point) => point.timestamp === Number(time[0])) : null;
        if (!observation) return "";
        return [marketValueDate(new Date(observation.timestamp).toISOString()), ...(["history", "recent"] as const).flatMap((kind) => observation[kind] === undefined ? [] : [`${kind === "history" ? "Asset history" : "Holdings estimate"}: ${nativeMoney(observation[kind], currency)}`])].join("\n");
      } },
      grid: { left: 2, right: 12, top: 22, bottom: compact ? 12 : 48, containLabel: true },
      xAxis: { ...axisBase, type: "time", min: plotStart, max: end ?? undefined, splitNumber: compact ? 3 : 7, axisLabel: { ...axisBase.axisLabel, hideOverlap: true, formatter: (value: number) => range === "1w" ? marketAxisLabel(value, "1w") : wealthHistoryDateLabel(value, new Date(low).toISOString().slice(0, 10), new Date(high).toISOString().slice(0, 10)) } },
      yAxis: { ...axisBase, type: "value", scale: true, splitNumber: 4, axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => compactWealth(value, axisStep) } },
      dataZoom: [
        { type: "inside", start: zoom.start, end: zoom.end, filterMode: "none", disabled: compact, zoomOnMouseWheel: "ctrl", moveOnMouseMove: false, preventDefaultMouseMove: false },
        { type: "slider", show: !compact, start: zoom.start, end: zoom.end, filterMode: "none", showDetail: false, brushSelect: false },
      ],
      series: [wealthHistoryNavigator(history.points), ...displayModel.series.map((series) => ({
        id: series.id, name: series.label, type: "line", preserveLinePattern: series.kind === "recent", connectNulls: false, smooth: false,
        showSymbol: (series.kind === "history" && observations.length <= 60) || series.data.filter(([, value]) => value !== null).length < 3,
        symbol: series.kind === "history" ? "diamond" : "circle", symbolSize: series.kind === "history" ? 6 : 5,
        lineStyle: { color: colour[series.kind], width: 2.5, type: series.kind === "recent" ? "dashed" : "solid" }, itemStyle: { color: colour[series.kind], borderColor: "white", borderWidth: 1 },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: series.kind === "history" ? "rgba(83,111,163,0.14)" : "rgba(175,67,33,0.10)" }, { offset: 1, color: "rgba(255,255,255,0)" }] } },
        emphasis: { focus: "series" }, data: series.data,
      })), ...connectors],
    };
  }, [displayModel, observations, compact, currency, range, plotStart, end, zoom, low, high, history.points, connectors]);

  function inspect(index: number) {
    const point = visible[Math.max(0, Math.min(visible.length - 1, index))];
    if (!point) return;
    setFocus(point.timestamp);
    const seriesIndex = displayModel.series.findIndex((series) => series.data.some(([time, value]) => time === point.timestamp && value !== null));
    const dataIndex = displayModel.series[seriesIndex]?.data.findIndex(([time]) => time === point.timestamp);
    if (seriesIndex >= 0) chart.current?.getEchartsInstance().dispatchAction({ type: "showTip", seriesIndex: seriesIndex + 1, dataIndex });
  }
  function changeZoom(next: { start: number; end: number }) {
    setZoom((current) => current.start === next.start && current.end === next.end ? current : next);
    setFocus(null);
  }
  function zoomBy(factor: number) {
    const width = Math.min(100, Math.max(5, (zoom.end - zoom.start) * factor));
    const centre = focused && high > low ? (focused.timestamp - low) / (high - low) * 100 : (zoom.start + zoom.end) / 2;
    const nextStart = Math.max(0, Math.min(100 - width, centre - width / 2));
    changeZoom({ start: nextStart, end: nextStart + width });
  }
  return <div className="min-w-0 px-3 pb-1 sm:px-5" data-testid="combined-value-view">
    <details className="mt-2 text-xs text-mist"><summary className="min-h-11 cursor-pointer py-2">Chart sources &amp; coverage{lastGap ? " · history gap" : ""}</summary>
    {lastGap && <p className="mt-3 text-xs leading-relaxed text-mist" aria-label="Account history gap">Account history has a gap from {portfolioDate(lastGap.start_date)} to {portfolioDate(lastGap.end_date)}.{continuity?.recorded_start_date ? ` Recorded broker balances begin ${portfolioDate(continuity.recorded_start_date)}.` : " Dated account records are unavailable for this interval."}</p>}
    {history.account_count !== history.selected_accounts && <p className="mt-2 text-xs text-mist">History covers {history.account_count} of {history.selected_accounts} selected accounts{showRecent ? "; recent prices use the same accounts" : ""}.</p>}
    {history.end_date && history.end_date.slice(0, 10) !== historyData.generated_at.slice(0, 10) && <p className="mt-2 text-xs text-mist">Dated account history through {portfolioDate(history.end_date)}. The balance above is the latest snapshot.</p>}
    {connectors.length > 0 && <p className="mt-2 text-xs leading-relaxed text-mist" aria-label="History visual links">Dashed links bridge missing dates or a change in valuation source.</p>}
    {showRecent && <p className="mt-2 leading-relaxed">The dashed line values today’s quantities and cash using completed hourly market bars and daily reference FX. Broker balances can use newer prices or different FX. Closed markets carry their last close; this is not a live portfolio quote or past account value.</p>}
    {showRecent && market.data && <div className="mt-2 space-y-2">
      <p>Account marks use recorded observation or receipt times where available. Date-only history is placed at the end of its UTC day (today at calculation time); this is an axis convention, not a known quote time. The hourly estimate uses completed regular and extended-session bars where available and carries earlier closes while markets are shut.</p>
      <p>Holdings saved: {marketValueDate(market.data.holdings_as_of)}. Newest constituent bar: {marketValueDate(market.data.market_as_of)}.</p>
      <p>Oldest price in the latest estimate: {marketValueDate(market.data.points[market.data.points.length - 1]?.oldest_price_at)}.</p>
      <p>{market.data.summary.priced_positions} / {market.data.summary.positions} holdings priced · {market.data.summary.known_accounts} / {market.data.summary.selected_accounts} accounts included.</p>
      {market.data.warnings.map(warning => <p key={warning} className="text-amber">{warning}</p>)}
    </div>}
    </details>
    <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2 text-xs text-mist" aria-label="Chart legend"><span><span className="mr-2 inline-block h-0.5 w-5 bg-seablue align-middle" />Asset history</span>{showRecent && <span><span className="mr-2 inline-block w-5 border-t-2 border-dashed border-teal align-middle" />Today’s holdings · hourly-price estimate</span>}</div>
    {showRecent && <p className="mt-2 text-xs leading-relaxed text-mist">The dashed line applies past prices to today’s holdings and cash. Earlier trades, deposits and withdrawals can make it differ from asset history; this gap cannot be filled from prices alone.</p>}
    {showRecent && market.data && <p className="mt-2 text-xs text-mist">Price range: {marketValueDate(market.data.points[market.data.points.length - 1]?.oldest_price_at)} to {marketValueDate(market.data.points[market.data.points.length - 1]?.newest_price_at ?? market.data.market_as_of)}{market.data.points[market.data.points.length - 1]?.sessions?.length ? ` · ${market.data.points[market.data.points.length - 1].sessions!.join(", ")}` : ""}{market.data.summary.partial ? " · partial estimate; see coverage above" : " · completed hourly bars"}</p>}
    {showRecent && market.loading && !market.data && <p className="mt-2 text-xs text-mist">Loading recent prices…</p>}
    {showRecent && market.data && !market.data.points.some(point => point.value !== null) && <p className="mt-2 text-xs text-mist">No recent prices available for these accounts.</p>}
    {market.error && <p role="status" className="mt-2 text-xs text-amber">{market.error}{market.data && " Saved observations remain visible."} <button className="min-h-11 px-1 underline" onClick={market.retry}>Retry prices</button></p>}
    {!observations.length ? <Empty>No dated account values are available in this period.</Empty> : <>
      <div tabIndex={0} role="group" aria-label="Interactive account value chart" className="mt-1 h-[260px] rounded-xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal sm:h-[320px]" style={{ touchAction: "pan-y" }} onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); inspect(event.key === "Home" ? 0 : event.key === "End" ? visible.length - 1 : focusIndex + (event.key === "ArrowLeft" ? -1 : 1));
      }} onPointerDown={(event) => { pointerStart.current = { x: event.clientX, y: event.clientY }; }} onPointerUp={(event) => {
        const down = pointerStart.current; pointerStart.current = null;
        if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 8 || !visible.length) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const pixel = [event.clientX - rect.left, event.clientY - rect.top];
        const instance = chart.current?.getEchartsInstance();
        if (!instance?.containPixel({ gridIndex: 0 }, pixel)) return;
        const time = instance.convertFromPixel({ xAxisIndex: 0 }, pixel[0]) as number;
        if (!Number.isFinite(time)) return;
        inspect(visible.reduce((nearest, point, index) => Math.abs(point.timestamp - time) < Math.abs(visible[nearest].timestamp - time) ? index : nearest, 0));
      }}><LedgerChart ref={chart} option={option} notMerge onEvents={{
        datazoom: (event: { start?: number; end?: number; batch?: { start?: number; end?: number }[] }) => { const value = event.batch?.[0] ?? event; if (value.start !== undefined && value.end !== undefined) changeZoom({ start: value.start, end: value.end }); },
        updateAxisPointer: (event: { axesInfo?: { value: number }[] }) => { const time = event.axesInfo?.[0]?.value; if (observations.some((point) => point.timestamp === time)) setFocus(time!); },
      }} style={{ height: "100%", width: "100%", minWidth: 0 }} /></div>
      <div className="rounded-xl border border-line bg-deep/50 p-3">
        <div className="flex items-center justify-between gap-2"><output aria-live="polite" aria-atomic="true" aria-label="Inspected portfolio values" className="min-w-0 text-xs leading-relaxed">
          {focused ? <><span className="block text-xs text-mist">{marketValueDate(new Date(focused.timestamp).toISOString())}</span>{(["history", "recent"] as const).map((kind) => focused[kind] === undefined ? null : <span key={kind} className="mt-1 block" style={{ color: colour[kind] }}>{kind === "history" ? "Asset history" : "Holdings estimate"} <strong className="font-semibold tabular-nums">{nativeMoney(focused[kind], currency)}</strong></span>)}</> : "No dated observations in this zoom. Zoom out to see more."}
        </output><div className="flex shrink-0 gap-1"><button type="button" aria-label="Previous comparison observation" className="h-11 w-11 rounded-lg border border-line bg-white text-lg disabled:opacity-35" disabled={focusIndex <= 0} onClick={() => inspect(focusIndex - 1)}>←</button><button type="button" aria-label="Next comparison observation" className="h-11 w-11 rounded-lg border border-line bg-white text-lg disabled:opacity-35" disabled={focusIndex < 0 || focusIndex >= visible.length - 1} onClick={() => inspect(focusIndex + 1)}>→</button></div></div>
        <label className="mt-2 block text-xs text-mist">Slide to inspect<input aria-label="Inspect comparison observations" type="range" min={0} max={Math.max(0, visible.length - 1)} step={1} value={Math.max(0, focusIndex)} disabled={visible.length < 2} onChange={(event) => inspect(Number(event.target.value))} className="block h-11 w-full cursor-pointer accent-teal" /></label>
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><p className="text-[10px] text-mist">{compact ? "Tap a line or slide to inspect" : "Hover to inspect · Ctrl + scroll to zoom"} · UTC</p><div className="flex gap-1"><button type="button" className="h-11 w-11 rounded-lg border border-line text-lg" aria-label="Zoom in on comparison" disabled={zoom.end - zoom.start <= 5} onClick={() => zoomBy(.5)}>+</button><button type="button" className="h-11 w-11 rounded-lg border border-line text-lg" aria-label="Zoom out of comparison" disabled={zoom.start === 0 && zoom.end === 100} onClick={() => zoomBy(2)}>−</button><button type="button" className="min-h-11 rounded-lg px-3 text-xs text-mist underline decoration-line underline-offset-4" aria-label="Reset comparison zoom" onClick={() => changeZoom({ start: 0, end: 100 })}>Reset zoom</button></div></div>
    </>}
    <details className="mt-3 border-t border-line text-xs text-mist"><summary className="min-h-11 cursor-pointer py-3">Dated values &amp; sources</summary><div className="space-y-3 pb-4 leading-relaxed">
      <p>Account history uses dated balances or reconstructed historical holdings, including cash flows. Recent prices apply cached market prices to today’s quantities and cash, excluding past trades. Their difference is not profit or loss.</p>
      <p>Lines only join dated observations. Blue dashed links bridge missing dates or changed valuation sources for the same accounts; they do not create values between the endpoints. Different account coverage remains unconnected. Account marks use observation or receipt times where available. Date-only marks sit at UTC day end, or calculation time today; that placement is not a known quote time. The inspector shows the observations at each plotted time.</p>
      <p>Daily history keeps its original dated evidence. Recent prices use hourly observations; points are not merged every two days.</p>
      {notes}
      {history && <p>{history.coverage_note}</p>}
      {market.data && <p>Newest constituent bar: {marketValueDate(market.data.market_as_of)}. Holdings snapshot: {marketValueDate(market.data.holdings_as_of)}. Calculated: {marketValueDate(market.data.generated_at)}.</p>}
      {[...(historyData?.warnings ?? []), ...(market.data?.warnings ?? [])].map((warning, index) => <p key={index} className="text-amber">{warning}</p>)}
      {observations.length > 0 && <div role="region" aria-label="Portfolio comparison observations" tabIndex={0} className="max-h-64 overflow-auto rounded-lg border border-line"><DataTable className="w-full min-w-[460px] text-left text-xs"><thead className="sticky top-0 bg-surface2"><tr><th className="p-2 font-medium">Chart time (UTC)</th><th className="p-2 text-right font-medium">History · {currency}</th><th className="p-2 text-right font-medium">Estimate · {currency}</th></tr></thead><tbody>{observations.map((point) => <tr key={point.timestamp} className="border-t border-line"><td className="p-2">{marketValueDate(new Date(point.timestamp).toISOString())}</td><td className="p-2 text-right tabular-nums">{nativeMoney(point.history, currency)}</td><td className="p-2 text-right tabular-nums">{nativeMoney(point.recent, currency)}</td></tr>)}</tbody></DataTable></div>}
    </div></details>
  </div>;
}
