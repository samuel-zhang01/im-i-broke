import { Heading } from "../Heading";
import { useState } from "react";
import LedgerChart from "../LedgerChart";
import type { AnalysedHolding, AnalysedSector } from "../../investmentAnalysisApi";
import { isKnown, nativeMoney } from "../../connectedPortfolio";
import { gainTone } from "../../investmentAnalysis";
import { Card, Empty } from "../ui";
import { C, tooltipBase } from "../../theme";
import InvestmentExposure from "./InvestmentExposure";

export default function SectorOverview({ sectors, holdings, currency, accountIds, revision, onChoose }: { sectors: AnalysedSector[]; holdings: AnalysedHolding[]; currency: string; accountIds: string[]; revision: string; onChoose: (sector: string) => void }) {
  const [sort, setSort] = useState("value");
  const [allocation, setAllocation] = useState<"sector" | "asset" | "currency">("sector");
  const rows = [...sectors].sort((a, b) => sort === "move" ? (b.day_move_estimate ?? -Infinity) - (a.day_move_estimate ?? -Infinity) : (b.value ?? -Infinity) - (a.value ?? -Infinity));
  const grouped = new Map<string, number>();
  for (const holding of holdings) {
      if (holding.is_cash || !isKnown(holding.value) || holding.value <= 0) continue;
      const name = allocation === "sector" ? holding.sector || "Unclassified" : allocation === "currency" ? holding.market_currency || "Unknown quote currency" : holding.asset_type?.replace(/_/g, " ") || "Unclassified";
      grouped.set(name, (grouped.get(name) ?? 0) + holding.value);
  }
  const slices = [...grouped].map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value);
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const colors = [C.teal, C.cyan, C.kelp, C.seablue, C.amber, "#887393", C.coral, "#8d9a8e"];
  const option = { animation: false, color: colors, aria: { enabled: true, description: `Positive priced security exposure by ${allocation}. Values are listed beside the chart.` }, tooltip: { ...tooltipBase, trigger: "item", valueFormatter: (value: number) => nativeMoney(value, currency) }, series: [{ type: "pie", radius: ["60%", "82%"], center: ["50%", "50%"], label: { show: false }, labelLine: { show: false }, itemStyle: { borderWidth: 3, borderColor: "white" }, data: slices }] };
  return <div className="space-y-4">
    <InvestmentExposure accountIds={accountIds} currency={currency} revision={revision} />
    <Card><div className="flex flex-wrap items-center justify-between gap-3"><Heading level={2} variant="section">Priced holdings allocation</Heading><div className="flex rounded-lg bg-surface2 p-1" role="group" aria-label="Allocation breakdown">{([['sector', 'Sector'], ['asset', 'Asset class'], ['currency', 'Currency']] as const).map(([value, label]) => <button key={value} aria-pressed={allocation === value} className={`min-h-11 rounded-md px-3 text-xs ${allocation === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`} onClick={() => setAllocation(value)}>{label}</button>)}</div></div>
      {!slices.length ? <Empty>No supported positive holdings values are available.</Empty> : <div className="grid items-center gap-5 md:grid-cols-[220px_minmax(0,1fr)]"><LedgerChart option={option} notMerge style={{ height: 220, width: "100%", minWidth: 0 }} /><dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">{slices.map((slice, index) => <div key={slice.name} className="flex items-center justify-between gap-3 text-sm"><dt className="flex min-w-0 items-center gap-2"><span className="h-2 w-2 shrink-0 rounded-full" style={{ background: colors[index % colors.length] }} aria-hidden="true" /><span className="break-words capitalize">{slice.name}</span></dt><dd className="whitespace-nowrap text-right text-xs font-medium tabular-nums">{nativeMoney(slice.value, currency)}<span className="mt-1 block text-xs font-normal text-mist">{(slice.value / total * 100).toFixed(1)}% of priced holdings</span></dd></div>)}</dl></div>}
      <p className="text-xs text-mist">Positive priced security exposure in {currency}; cash and negative positions are separate. Currency groups describe the holdings’ quote currencies, not company revenue exposure.</p>
    </Card>
    <Card className="!p-0 overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"><div><Heading level={2} variant="section">Sector session moves</Heading><p className="mt-1 text-xs text-mist">Current holdings × latest session price changes · estimates</p></div><select aria-label="Sort sectors" className="input" value={sort} onChange={(event) => setSort(event.target.value)}><option value="value">Largest exposure</option><option value="move">Highest session move</option></select></div>
    {!rows.length ? <Empty>No sector exposure is available for this selection.</Empty> : <><div className="hidden grid-cols-[minmax(0,1fr)_9rem_9rem_6rem] gap-4 border-y border-line bg-surface2 px-4 py-2 text-xs text-mist sm:grid"><span>Sector / coverage</span><span className="text-right">Exposure · {currency}</span><span className="text-right">Session estimate</span><span className="text-right">Account weight</span></div><div className="divide-y divide-line">{rows.map((sector) => <button key={sector.name} className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-4 py-3 text-left transition-colors hover:bg-surface2/50 sm:grid-cols-[minmax(0,1fr)_9rem_9rem_6rem]" onClick={() => onChoose(sector.name)} aria-label={`View ${sector.name} holdings`}><span className="min-w-0"><span className="block text-sm font-medium">{sector.name}</span><span className="mt-1 block text-xs text-mist">{sector.priced_holdings} / {sector.holdings_count} with session data</span></span><span className="text-right text-sm font-medium tabular-nums">{nativeMoney(sector.value, currency)}<span className={`mt-1 block text-xs sm:hidden ${gainTone(sector.day_move_estimate)}`}>{nativeMoney(sector.day_move_estimate, currency)} session</span></span><span className={`hidden text-right text-sm font-medium tabular-nums sm:block ${gainTone(sector.day_move_estimate)}`}>{nativeMoney(sector.day_move_estimate, currency)}</span><span className="hidden text-right text-xs tabular-nums text-mist sm:block">{sector.weight_pct === null ? "—" : `${sector.weight_pct.toFixed(1)}%`}</span></button>)}</div></>}
    </Card>
    <p className="text-xs text-mist">Weights use the selected account value, including cash. Funds stay in a separate bucket; unclassified holdings remain visible. These are your holdings’ estimated moves, not sector-index returns.</p>
  </div>;
}
