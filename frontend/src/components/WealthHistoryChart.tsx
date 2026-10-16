import { DataTable } from "./DataTable";
import { useRef, useState, type ReactNode } from "react";
import LedgerChart from "./LedgerChart";
import type ReactECharts from "echarts-for-react/esm/core";
import type { WealthPoint } from "../wealthApi";
import { chartPointValue, clampWealthDates, compactWealth, pointHasIncompleteCoverage, wealthHistoryConnectorSeries, wealthHistoryDateLabel, wealthHistorySegments, wealthPointEvidence } from "../wealth";
import { nativeMoney, portfolioDate } from "../connectedPortfolio";
import { C, axisBase, tooltipBase } from "../theme";
import { Empty, useCompactLayout } from "./ui";

type Entry = { date: string; net?: WealthPoint; investment?: WealthPoint };
const dayTime = (date: string) => Date.parse(`${date.slice(0, 10)}T00:00:00Z`);
const evidence = (point: WealthPoint, compact: boolean) => !compact ? wealthPointEvidence(point)
  : chartPointValue(point) === null ? "No complete value"
  : point.valuation_basis === "daily_holdings" ? "Reconstructed estimate"
  : point.valuation_basis === "provider_estimate" ? "Provider estimate"
  : point.valuation_basis === "mixed" ? "Mixed sources"
  : point.estimated ? "Recorded balance · estimate" : "Recorded balance";

export default function WealthHistoryChart({ points, investmentPoints = [], currency, selectedAccounts, investmentAccounts = 0, compact = false, notes }: {
  points: WealthPoint[]; investmentPoints?: WealthPoint[]; currency: string; selectedAccounts: number;
  investmentAccounts?: number; compact?: boolean; notes?: ReactNode;
}) {
  const chart = useRef<ReactECharts>(null);
  const touchLayout = useCompactLayout();
  const [chosenDates, setChosenDates] = useState({ start: "", end: "" });
  const [focusDate, setFocusDate] = useState<string | null>(null);
  const netByDate = new Map(points.map(point => [point.date.slice(0, 10), point]));
  const investmentByDate = new Map(investmentPoints.map(point => [point.date.slice(0, 10), point]));
  const dates = [...new Set([...netByDate.keys(), ...investmentByDate.keys()])].sort();
  const entries: Entry[] = dates.map(date => ({ date, net: netByDate.get(date), investment: investmentByDate.get(date) }));
  const first = dates[0] ?? "", last = dates[dates.length - 1] ?? "";
  const window = clampWealthDates([...points, ...investmentPoints], chosenDates.start, chosenDates.end);
  const times = dates.map(dayTime);
  const timeToEntry = new Map(times.map((time, index) => [time, entries[index]]));
  const hasInvestments = investmentAccounts > 0 && investmentPoints.some(point => chartPointValue(point) !== null);
  const partial = [...points, ...investmentPoints].some(pointHasIncompleteCoverage);
  const reconstructed = [...points, ...investmentPoints].some(point => point.valuation_basis === "daily_holdings" || (point.reconstructed_accounts ?? 0) > 0);
  const datedPoints = points.filter(point => chartPointValue(point) !== null);
  const datedInvestmentPoints = hasInvestments ? investmentPoints.filter(point => chartPointValue(point) !== null) : [];
  const observedEntries = entries.filter(entry => (entry.net && chartPointValue(entry.net) !== null)
    || (hasInvestments && entry.investment && chartPointValue(entry.investment) !== null));
  const visibleEntries = observedEntries.filter(entry => entry.date >= window.start && entry.date <= window.end);
  const earlierDatesHidden = visibleEntries.length < observedEntries.length;
  const latestValuationChanged = datedPoints.length > 1 && datedPoints[datedPoints.length - 1].valuation_key !== datedPoints[datedPoints.length - 2].valuation_key;
  const visibleValues = visibleEntries.flatMap(entry => [entry.net, hasInvestments ? entry.investment : undefined]
    .map(point => point ? chartPointValue(point) : null).filter((value): value is number => value !== null));
  const valueSpan = visibleValues.length ? Math.max(...visibleValues) - Math.min(...visibleValues) : 0;
  const axisStep = valueSpan ? valueSpan / 8 : Math.max(Math.abs(visibleValues[0] ?? 0) * 0.02, 0.01);
  const chosenIndex = visibleEntries.findIndex(entry => entry.date === focusDate);
  const focusIndex = chosenIndex < 0 ? visibleEntries.length - 1 : chosenIndex;
  const focusedEntry = visibleEntries[focusIndex];
  const netSegments = wealthHistorySegments(points);
  const investmentSegments = hasInvestments ? wealthHistorySegments(investmentPoints) : [];
  const netConnectors = wealthHistoryConnectorSeries(points, C.teal);
  const investmentConnectors = hasInvestments ? wealthHistoryConnectorSeries(investmentPoints, C.seablue)
    .map((connector, index) => ({ ...connector, id: `investment-link-${index}` })) : [];
  const connectorCount = netConnectors.length + investmentConnectors.length;
  const pointAt = (value: unknown): Entry | undefined => {
    if (!Array.isArray(value)) return undefined;
    return timeToEntry.get(Number(value[0]));
  };
  if (!observedEntries.length) return <Empty>No fully covered dated values are available yet. Current balances remain visible above.</Empty>;

  function lineSeries(source: WealthPoint[], segments: ReturnType<typeof wealthHistorySegments>, colour: string, label: string, idPrefix: string) {
    const sourceIndexByDate = new Map(source.map((point, index) => [point.date.slice(0, 10), index]));
    return segments.map((segment, index) => ({
      id: `${idPrefix}-${index}`, name: `${label} · ${currency}`, type: "line", smooth: false, connectNulls: false,
      showSymbol: (segment.partial && visibleEntries.length <= 60) || segment.values.filter(value => value !== null).length < 3,
      lineStyle: { color: colour, width: idPrefix === "history-observation" ? 2.5 : 2.2, type: "solid" },
      itemStyle: { color: segment.partial ? C.amber : colour, borderColor: "white", borderWidth: 1 },
      data: entries.map(entry => {
        const point = (idPrefix === "history-observation" ? entry.net : entry.investment);
        const sourceIndex = sourceIndexByDate.get(entry.date) ?? -1;
        const value = sourceIndex < 0 ? null : segment.values[sourceIndex];
        return { value: [dayTime(entry.date), value], symbol: point && pointHasIncompleteCoverage(point) ? "diamond" : "circle",
          symbolSize: point && pointHasIncompleteCoverage(point) ? 6 : 5,
          itemStyle: { color: point && pointHasIncompleteCoverage(point) ? C.amber : colour } };
      }),
    }));
  }
  const option = {
    animation: false, useUTC: true, backgroundColor: "transparent", textStyle: { fontFamily: "system-ui, sans-serif" },
    aria: { enabled: true, description: `Net-assets history in ${currency} across all selected accounts.${hasInvestments ? ` A separate investment-only line covers all ${investmentAccounts} selected investment accounts; older investment values are not full net assets.` : ""} Changes include transfers and cash flows. ${partial ? "Diamond markers identify estimates; missing dates have no value." : "Selected account coverage is complete."} Dashed links bridge missing dates or a change in valuation source; no values are inferred between their endpoints. Use left and right arrow keys to inspect observations, Home and End to jump, and the From and To date controls for keyboard zoom. A table of observations is available below.` },
    tooltip: { ...tooltipBase, trigger: "axis", renderMode: "richText", formatter: (params: { value: unknown; seriesId?: string }[]) => {
      const entry = params.filter(param => param.seriesId?.startsWith("history-observation-") || param.seriesId?.startsWith("investment-observation-"))
        .map(param => pointAt(param.value)).find(Boolean);
      if (!entry) return "";
      const net = entry.net && chartPointValue(entry.net);
      const investment = entry.investment && chartPointValue(entry.investment);
      const lines = [portfolioDate(entry.date)];
      if (net !== null && net !== undefined) {
        lines.push(`${hasInvestments ? "Net assets: " : ""}${nativeMoney(net, currency)}`);
        lines.push(`${entry.net!.known_accounts} of ${selectedAccounts} selected accounts`);
        lines.push(evidence(entry.net!, touchLayout));
      } else if (hasInvestments && investment !== null && investment !== undefined) {
        lines.push("Full net assets unavailable on this date");
      }
      if (hasInvestments && investment !== null && investment !== undefined) {
        lines.push(`Investments only: ${nativeMoney(investment, currency)}`);
        lines.push(`${entry.investment!.known_accounts} of ${investmentAccounts} selected investment accounts`);
        lines.push(evidence(entry.investment!, touchLayout));
      }
      if (entry.net?.carried_accounts && net !== null && net !== undefined) lines.push(`${entry.net.carried_accounts} ${touchLayout ? "carried forward" : "prior balances carried forward"}`);
      if (entry.net?.source_break && net !== null && net !== undefined) lines.push(touchLayout ? "Valuation source changed" : "Valuation source changed · comparison restarts");
      return lines.join("\n");
    } },
    grid: { left: 8, right: 18, top: 20, bottom: touchLayout ? 82 : 48, containLabel: true },
    xAxis: { ...axisBase, type: "time", boundaryGap: false, axisLabel: { ...axisBase.axisLabel, hideOverlap: true, formatter: (date: number) => {
      // ECharts adds hourly ticks on a short range; repeating one calendar day obscures the dates.
      if (dayTime(window.end) - dayTime(window.start) < 31 * 86_400_000 && date % 86_400_000 !== 0) return "";
      return wealthHistoryDateLabel(date, window.start, window.end);
    } } },
    yAxis: { ...axisBase, type: "value", scale: true, splitNumber: 4, axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => compactWealth(value, axisStep) } },
    dataZoom: [
      { type: "inside", disabled: touchLayout, moveOnMouseMove: false, filterMode: "filter", zoomOnMouseWheel: "ctrl", preventDefaultMouseMove: false, startValue: dayTime(window.start), endValue: dayTime(window.end) },
      { type: "slider", filterMode: "filter", height: touchLayout ? 44 : 18, bottom: touchLayout ? 4 : 8, borderColor: C.line, fillerColor: "rgba(175,67,33,0.10)", handleStyle: { color: C.teal }, moveHandleStyle: { color: C.teal, opacity: 0.25 }, dataBackground: { lineStyle: { color: C.mist }, areaStyle: { color: C.line } }, selectedDataBackground: { lineStyle: { color: C.teal }, areaStyle: { color: "rgba(175,67,33,0.15)" } }, textStyle: { color: C.mist }, showDetail: false },
    ],
    series: [
      { id: "history-navigator", name: "History navigator", type: "line", silent: true, showSymbol: false, symbol: "none", connectNulls: false,
        lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 }, emphasis: { disabled: true }, tooltip: { show: false },
        data: entries.map(entry => [dayTime(entry.date), (entry.net && chartPointValue(entry.net)) ?? (entry.investment && chartPointValue(entry.investment)) ?? null]) },
      ...lineSeries(points, netSegments, C.teal, "Net assets", "history-observation"),
      ...lineSeries(investmentPoints, investmentSegments, C.seablue, "Investments only", "investment-observation"),
      ...netConnectors, ...investmentConnectors,
    ],
  };
  function onZoom(event: { start?: number; end?: number; batch?: { start?: number; end?: number }[] }) {
    const changed = event.batch?.[0] ?? event;
    if (changed.start === undefined || changed.end === undefined || times.length < 2) return;
    const min = times[0], span = times[times.length - 1] - min;
    setChosenDates({ start: new Date(min + span * changed.start / 100).toISOString().slice(0, 10), end: new Date(min + span * changed.end / 100).toISOString().slice(0, 10) });
    setFocusDate(null);
  }
  function inspect(index: number) {
    const entry = visibleEntries[Math.max(0, Math.min(visibleEntries.length - 1, index))];
    if (entry) setFocusDate(entry.date);
  }
  const focusedNet = focusedEntry?.net && chartPointValue(focusedEntry.net);
  const focusedInvestment = focusedEntry?.investment && chartPointValue(focusedEntry.investment);
  return <div className="min-w-0">
    {hasInvestments && <div className="mb-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-mist" aria-label="History series legend"><span className="inline-flex items-center gap-2"><span aria-hidden="true" className="inline-block w-5 border-t-[3px]" style={{ borderColor: C.teal }} />Net assets · all {selectedAccounts} selected accounts</span><span className="inline-flex items-center gap-2"><span aria-hidden="true" className="inline-block w-5 border-t-[3px]" style={{ borderColor: C.seablue }} />Investments only · {investmentAccounts} selected investment account{investmentAccounts === 1 ? "" : "s"}</span></div>}
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-mist" role="status">
      <span>{earlierDatesHidden ? `Showing ${visibleEntries.length} of ${observedEntries.length} dated values` : `${observedEntries.length} dated value${observedEntries.length === 1 ? "" : "s"} shown`}{hasInvestments ? ` · ${datedPoints.length} net-assets dates · ${datedInvestmentPoints.length} investment dates` : ""}</span>
      {earlierDatesHidden && <button type="button" className="min-h-11 text-teal underline decoration-line underline-offset-4" onClick={() => setChosenDates({ start: "", end: "" })}>Show all dates</button>}
    </div>
    {latestValuationChanged && <p className="mb-2 text-xs leading-relaxed text-mist">The latest point uses a different valuation method. Earlier net-assets values remain on the historical line; the dashed link shows the change in method.</p>}
    <div tabIndex={0} role="group" aria-label="Interactive net assets chart" style={{ touchAction: "pan-y" }} className={`${compact ? "h-[240px]" : "h-[280px] sm:h-[320px]"} rounded-lg focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal`} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) chart.current?.getEchartsInstance().dispatchAction({ type: "hideTip" }); }} onKeyDown={event => {
      if (event.key === "Escape") { chart.current?.getEchartsInstance().dispatchAction({ type: "hideTip" }); return; }
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      inspect(event.key === "Home" ? 0 : event.key === "End" ? visibleEntries.length - 1 : focusIndex + (event.key === "ArrowLeft" ? -1 : 1));
    }}><LedgerChart ref={chart} option={option} notMerge onEvents={{ datazoom: onZoom, click: (event: { value?: unknown }) => setFocusDate(pointAt(event.value)?.date ?? null) }} style={{ height: "100%", width: "100%", minWidth: 0 }} /></div>
    {connectorCount > 0 && <p className="mt-1 text-xs leading-relaxed text-mist" aria-label="History visual links"><span aria-hidden="true" className="mr-2 inline-block w-5 border-t-2 border-dashed border-teal align-middle" />Dashed links bridge missing dates or a change in valuation source.</p>}
    <div className="mt-1 flex items-center justify-between gap-2 text-xs"><output aria-live="polite" aria-label="Inspected net assets" className="min-w-0 text-mist">{focusedEntry ? <>{focusedNet !== null && focusedNet !== undefined ? <><span className="font-medium tabular-nums text-foam">{hasInvestments ? "Net assets " : ""}{nativeMoney(focusedNet, currency)}</span>{hasInvestments && focusedInvestment !== null && focusedInvestment !== undefined && <> · Investments {nativeMoney(focusedInvestment, currency)}</>}</> : focusedInvestment !== null && focusedInvestment !== undefined ? <><span className="font-medium tabular-nums text-foam">Investments only {nativeMoney(focusedInvestment, currency)}</span> · full net assets unavailable</> : null} · {portfolioDate(focusedEntry.date)}{(focusedEntry.net ?? focusedEntry.investment)?.valuation_basis === "daily_holdings" ? " · reconstructed estimate" : (focusedEntry.net ?? focusedEntry.investment)?.estimated ? " · estimate" : ""}</> : "No observations inside this date window"}</output><div className="flex shrink-0 gap-1"><button type="button" aria-label="Previous value observation" disabled={focusIndex <= 0} className="h-11 w-11 rounded-lg border border-line text-sm disabled:opacity-35" onClick={() => inspect(focusIndex - 1)}>←</button><button type="button" aria-label="Next value observation" disabled={focusIndex < 0 || focusIndex >= visibleEntries.length - 1} className="h-11 w-11 rounded-lg border border-line text-sm disabled:opacity-35" onClick={() => inspect(focusIndex + 1)}>→</button></div></div>
    <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-xs text-mist"><span>{!compact && "Dated balances · includes transfers and cash flows"}{partial && <span className="ml-2 inline-flex items-center gap-1"><span aria-hidden="true" className="text-amber">◆</span> Estimates or incomplete coverage</span>}</span><details className="relative w-full sm:w-auto"><summary className="min-h-11 cursor-pointer py-3 underline decoration-line underline-offset-4">Choose exact dates</summary><div className="bottom-full right-0 z-10 mb-2 w-full sm:absolute sm:w-72 rounded-xl border border-line bg-white p-3 shadow-lg"><div className="grid grid-cols-2 gap-2"><label className="space-y-1">From<input aria-label="History start date" type="date" className="input min-h-11 w-full min-w-0 !px-2 !text-base sm:!text-xs" min={first} max={window.end} value={window.start} onChange={event => setChosenDates({ start: event.target.value, end: window.end })} /></label><label className="space-y-1">To<input aria-label="History end date" type="date" className="input min-h-11 w-full min-w-0 !px-2 !text-base sm:!text-xs" min={window.start} max={last} value={window.end} onChange={event => setChosenDates({ start: window.start, end: event.target.value })} /></label></div><button className="mt-3 min-h-11 text-xs font-medium text-teal underline" onClick={() => setChosenDates({ start: first, end: last })}>Reset chart zoom</button></div></details></div>
    <details className="mt-3 border-t border-line text-xs text-mist" onToggle={event => { if (event.currentTarget.open) chart.current?.getEchartsInstance().dispatchAction({ type: "hideTip" }); }}><summary className="cursor-pointer py-3">Dated values &amp; chart details</summary><p className="mb-3 leading-relaxed">The net-assets line includes every selected account. The investment line includes every selected investment account but omits banks and cards; its earlier values are not full net assets. Lines join dated observations visually and do not create values between them. Dashed links bridge missing dates or changed valuation sources. Amber diamonds mark estimates when zoomed in; the table names estimates at every range. Balance movement includes cash flows and is not investment return.</p>{reconstructed && <p className="mb-3 leading-relaxed">Reconstructed dates use supported historical holdings, market prices and dated exchange rates. Cash and account coverage depend on the available records.</p>}{notes && <div className="mb-3 space-y-2 leading-relaxed">{notes}</div>}<p className="mb-2 text-xs text-mist sm:hidden">Scroll sideways for {hasInvestments ? "investments, coverage and evidence" : "coverage and evidence"} →</p><div role="region" aria-label="Net assets observations" tabIndex={0} className="max-h-72 overflow-auto rounded-lg border border-line"><DataTable className="w-full min-w-[640px] text-left"><thead className="sticky top-0 bg-surface2"><tr><th className="p-3 font-medium">Date</th><th className="p-3 text-right font-medium">Net assets ({currency})</th>{hasInvestments && <th className="p-3 text-right font-medium">Investments only ({currency})</th>}<th className="p-3 font-medium">Accounts</th><th className="p-3 font-medium">Evidence</th></tr></thead><tbody>{entries.map(entry => <tr key={entry.date} className="border-t border-line/60"><td className="p-3">{portfolioDate(entry.date)}</td><td className="p-3 text-right tabular-nums">{entry.net && chartPointValue(entry.net) !== null ? nativeMoney(chartPointValue(entry.net), currency) : "—"}</td>{hasInvestments && <td className="p-3 text-right tabular-nums">{entry.investment && chartPointValue(entry.investment) !== null ? nativeMoney(chartPointValue(entry.investment), currency) : "—"}</td>}<td className="p-3">{entry.net && chartPointValue(entry.net) !== null ? `${entry.net.known_accounts} / ${selectedAccounts}` : entry.investment ? `${entry.investment.known_accounts} / ${investmentAccounts} investments` : entry.net ? `${entry.net.known_accounts} / ${selectedAccounts}` : "—"}</td><td className="p-3">{entry.net && chartPointValue(entry.net) !== null ? wealthPointEvidence(entry.net) : entry.investment && chartPointValue(entry.investment) !== null ? `Investments only · ${wealthPointEvidence(entry.investment)}${entry.net ? " · full net assets unavailable" : ""}` : entry.net ? wealthPointEvidence(entry.net) : "—"}{entry.net?.carried_accounts && chartPointValue(entry.net) !== null ? ` · ${entry.net.carried_accounts} carried forward` : ""}{entry.net?.source_break && chartPointValue(entry.net) !== null ? " · valuation source changed" : ""}{hasInvestments && entry.net && chartPointValue(entry.net) !== null && entry.investment && chartPointValue(entry.investment) !== null ? ` · Investments: ${wealthPointEvidence(entry.investment)}` : ""}</td></tr>)}</tbody></DataTable></div></details>
  </div>;
}
