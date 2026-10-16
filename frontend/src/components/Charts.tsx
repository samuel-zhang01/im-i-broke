import LedgerChart from "./LedgerChart";
import { useState } from "react";
import type { Analytics, NetPoint, TimeSeries } from "../api";
import { C, axisBase, tooltipBase, accountColor, chartStyle } from "../theme";
import { moneyShort, money } from "../format";
import { balanceSeriesData, balanceTooltip, chartPeriodLabel, finiteChartValue, signedBarPoint, escapeChartText } from "../chartData";

// Keep account distinctions visible in small plots; tooltips retain full names.
const compactAccountName = (name: string) => name
  .replace("Trading 212 · Stocks & Shares ISA", "T212 ISA")
  .replace("Trading 212 · Stocks", "T212 Stocks")
  .replace("Moomoo Margin · ", "Moomoo · ");
const SCROLL_LEGEND = chartStyle.legend;

// ECharts media queries use the chart container's width, including when a card
// moves between grid columns. Explicit regular options restore the layout when
// it grows again without resetting the user's zoom or legend selection.
function Chart({ option, height, compact, regular }: {
  option: Record<string, unknown>;
  height: number;
  compact?: Record<string, unknown>;
  regular?: Record<string, unknown>;
}) {
  const responsive = compact ? {
    baseOption: option,
    media: [
      { query: { maxWidth: 480 }, option: compact },
      { option: regular ?? {} },
    ],
  } : option;
  return <LedgerChart option={responsive} style={{ height, width: "100%", minWidth: 0 }} notMerge />;
}

// Period cashflows and cumulative cashflow use separate, clearly named views.
// An independently scaled second axis can make their relative height misleading.
export function NetChart({ net }: { net: NetPoint[] }) {
  const [view, setView] = useState<"cashflow" | "cumulative">("cashflow");
  const periods = net.map((p) => p.period);
  const cumulative = view === "cumulative";
  const option = {
    aria: { enabled: true, description: cumulative
      ? "Cumulative recorded net cashflow in pounds over the selected history. This is income less spending, not account value."
      : "Recorded income and spending in pounds by period. Money leaving is shown below zero; net refunds can appear above zero." },
    tooltip: { ...tooltipBase, trigger: "axis", valueFormatter: (v: number) => money(v) },
    legend: {
      ...SCROLL_LEGEND,
      data: cumulative ? ["Cumulative net cashflow"] : ["Income", "Spending"],
      icon: "roundRect",
    },
    grid: { left: 56, right: 16, top: 36, bottom: 54 },
    xAxis: { type: "category", data: periods, ...axisBase, boundaryGap: !cumulative,
      axisLabel: { ...axisBase.axisLabel, formatter: chartPeriodLabel } },
    yAxis: { type: "value", ...axisBase, axisLabel: { ...axisBase.axisLabel, formatter: moneyShort } },
    dataZoom: [
      { type: "inside", start: Math.max(0, 100 - (1800 / Math.max(periods.length, 1))) },
      { type: "slider", height: 16, bottom: 12, borderColor: C.line,
        fillerColor: "rgba(175,67,33,0.12)", handleStyle: { color: C.teal },
        textStyle: { color: C.mist } },
    ],
    series: cumulative ? [
      { name: "Cumulative net cashflow", type: "line", smooth: false, symbol: "circle", showSymbol: net.length === 1,
        connectNulls: false, data: net.map((p) => finiteChartValue(p.cumulative)),
        lineStyle: { color: C.cyan, width: 2 },
        itemStyle: { color: C.cyan },
        areaStyle: { color: "rgba(71,116,134,0.08)" } },
    ] : [
      { name: "Income", type: "bar", stack: "cashflow", data: net.map((p) => signedBarPoint(p.income, C.kelp)), itemStyle: { color: C.kelp } },
      { name: "Spending", type: "bar", stack: "cashflow", data: net.map((p) => signedBarPoint(p.spending, C.coral)), itemStyle: { color: C.coral } },
    ],
  };
  if (!net.length) return <div className="py-12 text-center text-sm text-mist">No recorded cashflow for this selection.</div>;
  return <div className="min-w-0">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <div className="flex rounded-lg bg-surface2 p-1" role="group" aria-label="Cashflow chart measure">
        {([['cashflow', 'Income & spending'], ['cumulative', 'Cumulative net']] as const).map(([value, label]) => <button key={value} className={`min-h-11 rounded-md px-3 text-xs ${view === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`} aria-pressed={view === value} onClick={() => setView(value)}>{label}</button>)}
      </div>
      <span className="text-xs text-mist">{cumulative ? "Recorded income less spending · GBP" : "Outflows below zero · GBP"}</span>
    </div>
    <Chart option={option} height={280}
      compact={{ grid: { left: 46, right: 12 }, yAxis: { splitNumber: 3 } }}
      regular={{ grid: { left: 56, right: 16 }, yAxis: { splitNumber: 5 } }} />
  </div>;
}

// Running balance per account, multi-line, brushable.
export function BalanceChart({ ts }: { ts: TimeSeries }) {
  const names = Object.keys(ts.balances);
  const periods = Array.from(
    new Set(Object.values(ts.balances).flatMap((s) => s.map((p) => p.period)))
  ).sort();
  const series = names.map((name, i) => {
    const data = balanceSeriesData(periods, ts.balances[name]);
    const color = accountColor(name, i);
    const sameColorBefore = names.slice(0, i).filter(other => accountColor(other) === color).length;
    return {
      name, type: "line", step: "end", symbol: ["circle", "rect", "triangle", "diamond"][sameColorBefore % 4], symbolSize: 4, showSymbol: true, connectNulls: false,
      data, lineStyle: { width: 2, color, type: "solid" },
      itemStyle: { color },
    };
  });
  const option = {
    aria: { enabled: true, description: "Saved account balance points in pounds. Step lines carry the last saved point through gaps; a flat line does not prove that the balance was unchanged." },
    tooltip: { ...tooltipBase, trigger: "axis", renderMode: "html", formatter: balanceTooltip },
    legend: { ...SCROLL_LEGEND, data: names },
    grid: { left: 56, right: 24, top: 36, bottom: 54 },
    xAxis: { type: "category", data: periods, ...axisBase, boundaryGap: false,
      axisLabel: { ...axisBase.axisLabel, formatter: chartPeriodLabel } },
    yAxis: { type: "value", ...axisBase,
      axisLabel: { ...axisBase.axisLabel, formatter: moneyShort } },
    dataZoom: [
      { type: "inside" },
      { type: "slider", height: 16, bottom: 12, borderColor: C.line,
        fillerColor: "rgba(175,67,33,0.12)", handleStyle: { color: C.teal },
        textStyle: { color: C.mist } },
    ],
    series,
  };
  return names.length ? <Chart option={option} height={320}
    compact={{ grid: { left: 46, right: 12 }, legend: { textStyle: { width: 130 } } }}
    regular={{ grid: { left: 56, right: 24 }, legend: { textStyle: { width: 180 } } }} /> : <div className="py-12 text-center text-sm text-mist">No supported balance history for this selection.</div>;
}

// Spend vs income per account, grouped bars.
export function AccountBars({ data }: { data: Analytics["by_account"] }) {
  const option = {
    tooltip: { ...tooltipBase, trigger: "axis", axisPointer: { type: "shadow" },
      valueFormatter: (v: number) => money(v) },
    legend: { ...SCROLL_LEGEND, data: ["Spending", "Income"] },
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    xAxis: { type: "category", data: data.map((d) => d.account), ...axisBase,
      axisLabel: { ...axisBase.axisLabel, interval: 0, width: 80, overflow: "truncate", formatter: (value: string) => value } },
    yAxis: { type: "value", ...axisBase,
      axisLabel: { ...axisBase.axisLabel, formatter: moneyShort } },
    series: [
      { name: "Spending", type: "bar", stack: "cashflow", data: data.map((d) => signedBarPoint(d.spend, C.coral)),
        itemStyle: { color: C.coral, borderRadius: [4, 4, 0, 0] } },
      { name: "Income", type: "bar", stack: "cashflow", data: data.map((d) => signedBarPoint(d.income, C.kelp)),
        itemStyle: { color: C.kelp, borderRadius: [4, 4, 0, 0] } },
    ],
  };
  return <Chart option={option} height={280}
    compact={{
      xAxis: { type: "value", data: [], splitNumber: 2, axisLabel: { width: null, overflow: "none", formatter: moneyShort } },
      yAxis: { type: "category", data: data.map((d) => d.account), axisLabel: { width: 96, overflow: "truncate", formatter: compactAccountName } },
      series: [
        { data: data.map(d => signedBarPoint(d.spend, C.coral, true)) },
        { data: data.map(d => signedBarPoint(d.income, C.kelp, true)) },
      ],
    }}
    regular={{
      xAxis: { ...option.xAxis, splitNumber: 5 },
      yAxis: { ...option.yAxis, data: [], axisLabel: { ...option.yAxis.axisLabel, width: null, overflow: "none" } },
      series: option.series,
    }} />;
}

// Sankey of money flow between accounts (aggregated transfer routes).
export function TransferSankey({ flows }: { flows: { from: string; to: string; value: number }[] }) {
  const escape = escapeChartText;
  flows = flows.filter(flow => finiteChartValue(flow.value) !== null && flow.value > 0);
  const names = Array.from(new Set(flows.flatMap((f) => [`${f.from} `, ` ${f.to}`])));
  const palette = [C.teal, C.seablue, C.cyan, C.kelp, C.amber, "#A78BFA", C.coral];
  const option = {
    tooltip: { ...tooltipBase, trigger: "item", triggerOn: "mousemove|click",
      formatter: (p: { data?: { source?: string; target?: string }; value?: number; name?: string }) =>
        p.data && p.data.source
          ? `${escape(p.data.source.trim())} → ${escape(p.data.target?.trim() ?? "")}<br/><b>${money(p.value)}</b>`
          : escape((p.name ?? "").trim()) },
    series: [{
      type: "sankey", left: 8, right: 120, top: 10, bottom: 10,
      nodeWidth: 14, nodeGap: 14,
      data: names.map((n, i) => ({ name: n,
        itemStyle: { color: palette[i % palette.length], borderColor: "transparent" },
        label: { color: C.foam, formatter: () => compactAccountName(n.trim()) } })),
      links: flows.map((f) => ({ source: `${f.from} `, target: ` ${f.to}`, value: f.value })),
      lineStyle: { color: "gradient", opacity: 0.35, curveness: 0.5 },
      label: { color: C.foam, fontSize: 11, width: 110, overflow: "truncate" },
      emphasis: { focus: "adjacency" },
    }],
  };
  return flows.length ? <Chart option={option} height={Math.max(260, names.length * 34)}
    compact={{ series: [{ right: 88, nodeWidth: 10, label: { width: 78, fontSize: 11 } }] }}
    regular={{ series: [{ right: 120, nodeWidth: 14, label: { width: 110, fontSize: 11 } }] }} /> : <div className="py-12 text-center text-sm text-mist">No matched transfer routes for this selection.</div>;
}
