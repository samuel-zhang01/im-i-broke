import { Heading } from "../Heading";
import { DataTable } from "../DataTable";
import { useEffect, useState } from "react";
import LedgerChart from "../LedgerChart";
import { calendarMonthLabel, calendarOffset, compactCalendarChange, fetchInvestmentCalendar, type CalendarSource, type InvestmentCalendarData } from "../../calendarApi";
import { isKnown, nativeMoney, portfolioDate } from "../../connectedPortfolio";
import { gainTone, signedPercent } from "../../investmentAnalysis";
import { C, axisBase, tooltipBase } from "../../theme";
import { Card, Empty, Spinner } from "../ui";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const narrowCalendarChange = (value: number | null | undefined) => !isKnown(value) || Math.abs(value) < 1000 ? compactCalendarChange(value) : `${value > 0 ? "+" : ""}${new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 0 }).format(value)}`;
const cellTone = (change: number | null) => !isKnown(change) ? "border-line bg-surface2/25 text-mist" : change > 0 ? "border-kelp/15 bg-kelp/10 text-kelp" : change < 0 ? "border-coral/15 bg-coral/10 text-coral" : "border-line bg-surface2 text-mist";

export default function InvestmentCalendar({ accountIds, currency, revision }: { accountIds: string[]; currency: string; revision: string }) {
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [source, setSource] = useState<CalendarSource>("estimate");
  const [view, setView] = useState<"days" | "months">("days");
  const [chart, setChart] = useState<"value" | "change">("value");
  const [comparison, setComparison] = useState<"observed" | "daily">("observed");
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [result, setResult] = useState<{ key: string; data: InvestmentCalendarData } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const key = JSON.stringify([accountIds, currency, year, month, source, revision]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(null); setSelectedDay(null);
    void fetchInvestmentCalendar(accountIds, currency, year, month, source, controller.signal).then((data) => {
      if (!controller.signal.aborted) setResult({ key, data });
    }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "The value calendar is unavailable."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // The key includes the complete scope and revision; old responses cannot
    // render under a newly selected account, date or reporting currency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const data = result?.key === key ? result.data : null;
  const summary = view === "months" ? data?.year_summary : data?.summary;
  const periodName = view === "months" ? String(year) : calendarMonthLabel(year, month);
  const years = [...new Set([year, now.getFullYear(), ...(data?.available_years ?? [])])].sort((a, b) => b - a);
  const selected = data?.days.find((day) => day.date === selectedDay);
  const points = data?.points ?? [];
  const periods = data?.months ?? [];
  const changes = data?.days.map((day) => comparison === "observed" ? day.observation_change : day.change) ?? [];
  const selectedChange = selected ? comparison === "observed" ? selected.observation_change : selected.change : null;
  const selectedStart = selected ? comparison === "observed" ? selected.observation_start_value : selected.start_value : null;
  const observedMode = view === "days" && comparison === "observed";
  const positives = view === "months" ? periods.filter((period) => isKnown(period.change) && period.change > 0).length : observedMode ? changes.filter((value) => isKnown(value) && value > 0).length : summary?.positive_days ?? 0;
  const negatives = view === "months" ? periods.filter((period) => isKnown(period.change) && period.change < 0).length : observedMode ? changes.filter((value) => isKnown(value) && value < 0).length : summary?.negative_days ?? 0;
  const chartDates = view === "months" ? MONTHS : (chart === "value" ? points.map((p) => p.date) : data?.days.map((p) => p.date) ?? []);
  const chartValues = view === "months" ? periods.map((p) => p.change) : chart === "value" ? points.map((p) => p.value) : changes;
  const bars = view === "months" || chart === "change";
  const option = {
    animation: false, textStyle: { fontFamily: "system-ui, sans-serif" },
    aria: { enabled: true, description: `${periodName} account ${bars ? "value changes" : "values"} in ${currency}. Includes cash flows; this is not a personal profit chart. Gaps mean unavailable data.` },
    tooltip: { ...tooltipBase, trigger: "axis", renderMode: "richText", formatter: (params: { dataIndex: number }[]) => {
      const index = params[0]?.dataIndex;
      if (index === undefined) return "";
      const interval = view === "days" && bars && observedMode ? data?.days[index] : null;
      return `${view === "months" ? `${MONTHS[index]} ${year}` : portfolioDate(chartDates[index])}\n${nativeMoney(chartValues[index], currency)}\n${bars ? "Value change · includes cash flows" : "Account value"}${interval?.observation_days ? `\n${interval.observation_days}-day interval from ${portfolioDate(interval.observation_start_date)}` : ""}`;
    } },
    grid: { left: 55, right: 12, top: 20, bottom: 32 },
    xAxis: view === "months" ? { ...axisBase, type: "category", data: chartDates, boundaryGap: true } : { ...axisBase, type: "time",
      axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => new Date(value).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }), hideOverlap: true } },
    yAxis: { ...axisBase, type: "value", scale: !bars, splitNumber: 4,
      axisLabel: { ...axisBase.axisLabel, formatter: (value: number) => new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 1 }).format(value) } },
    series: [{ type: bars ? "bar" : "line", smooth: false, connectNulls: false, showSymbol: true, symbolSize: 5,
      barMaxWidth: 20, lineStyle: { width: 2, color: C.teal }, itemStyle: { color: C.teal },
      areaStyle: bars ? undefined : { color: "rgba(175,67,33,0.04)" },
      data: chartValues.map((value, index) => {
        const datum = view === "months" ? value : [Date.parse(`${chartDates[index].slice(0, 10)}T00:00:00Z`), value];
        return bars ? { value: datum, itemStyle: { color: isKnown(value) && value < 0 ? C.coral : C.kelp, borderRadius: [3, 3, 0, 0] } } : datum;
      }),
    }],
  };
  function moveMonth(direction: number) {
    const target = new Date(Date.UTC(year, month - 1 + direction, 1));
    if (target.getUTCFullYear() >= 1970 && target.getUTCFullYear() <= now.getFullYear() + 1) {
      setYear(target.getUTCFullYear()); setMonth(target.getUTCMonth() + 1);
    }
  }
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><Heading level={2} variant="section">Value calendar</Heading><p className="mt-1 text-xs text-mist">Account-value movement · includes transfers and cash flows</p></div>
      <div className="flex max-w-full flex-wrap items-center gap-2">
        <label><span className="sr-only">Calendar source</span><select className="input max-w-full text-xs" aria-label="Calendar source" value={source} onChange={(event) => setSource(event.target.value as CalendarSource)}><option value="estimate">Estimated history</option><option value="recorded">Recorded snapshots</option></select></label>
        <div className="flex rounded-lg bg-surface2 p-1" role="group" aria-label="Calendar view">{(["days", "months"] as const).map((value) => <button key={value} className={`min-h-11 rounded-md px-3 text-xs ${view === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`} aria-pressed={view === value} onClick={() => setView(value)}>{value === "days" ? "Daily" : "Monthly"}</button>)}</div>
      </div>
    </div>
    <Card className="!p-0 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-5">
        <div className="flex items-center gap-2"><button className="btn min-h-11 min-w-10 px-2" aria-label={view === "days" ? "Previous month" : "Previous year"} onClick={() => view === "days" ? moveMonth(-1) : setYear(Math.max(1970, year - 1))}>‹</button><Heading level={3} variant="subsection" className="min-w-[120px] text-center sm:min-w-[155px]">{periodName}</Heading><button className="btn min-h-11 min-w-10 px-2" aria-label={view === "days" ? "Next month" : "Next year"} disabled={view === "months" && year >= now.getFullYear() + 1} onClick={() => view === "days" ? moveMonth(1) : setYear(Math.min(now.getFullYear() + 1, year + 1))}>›</button></div>
        <div className="flex flex-wrap gap-2">{view === "days" && <label><span className="sr-only">Calendar comparison</span><select className="input text-xs" aria-label="Calendar comparison" value={comparison} onChange={(event) => setComparison(event.target.value as "observed" | "daily")}><option value="observed">Between observations</option><option value="daily">Day-to-day</option></select></label>}<label><span className="sr-only">Calendar year</span><select className="input text-xs" aria-label="Calendar year" value={year} onChange={(event) => setYear(Number(event.target.value))}>{years.map((value) => <option key={value} value={value}>{value}</option>)}</select></label></div>
      </div>
      {loading ? <div className="px-5"><Spinner label="Loading saved value history…" /></div> : error ? <p role="status" className="px-5 py-10 text-sm text-amber">{error}</p> : data && <>
        <div className="grid gap-5 border-b border-line px-4 py-5 sm:grid-cols-[1fr_auto] sm:px-5">
          <div><p className="text-xs text-mist">{view === "months" ? "Year" : "Month"} value change · {currency}</p><p className={`mt-1 text-3xl font-semibold tracking-tight tabular-nums ${gainTone(summary?.change)}`}>{nativeMoney(summary?.change, currency)}</p><p className="mt-2 text-xs text-mist">{summary?.complete ? `${signedPercent(summary.change_pct)} value change · through ${portfolioDate(summary.end_date)}` : `${summary?.observed_days ?? 0} comparable days · incomplete period`}</p></div>
          <dl className="grid grid-cols-3 gap-3 text-xs sm:flex sm:flex-wrap sm:items-end sm:gap-5"><div><dt className="min-h-8 text-mist sm:min-h-0">Positive {view === "months" ? "months" : observedMode ? "intervals" : "days"}</dt><dd className="mt-1 text-lg font-semibold text-kelp tabular-nums">{positives}</dd></div><div><dt className="min-h-8 text-mist sm:min-h-0">Negative {view === "months" ? "months" : observedMode ? "intervals" : "days"}</dt><dd className="mt-1 text-lg font-semibold text-coral tabular-nums">{negatives}</dd></div><div><dt className="min-h-8 text-mist sm:min-h-0">{view === "months" ? "Comparable months" : observedMode ? "Observed dates" : "Daily coverage"}</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{view === "months" ? periods.filter((period) => isKnown(period.change)).length : observedMode ? data.days.filter((day) => isKnown(day.end_value)).length : summary?.observed_days ?? 0}<span className="text-xs font-normal text-mist">{view === "months" ? " / 12 months" : ` / ${summary?.expected_days ?? 0} days`}</span></dd></div></dl>
        </div>
        <div className="grid lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div className="min-w-0 p-3 sm:p-5">
            {view === "days" ? <><div className="mb-2 grid grid-cols-7 text-center text-[10px] font-medium text-mist sm:text-xs">{WEEKDAYS.map((day) => <span key={day}>{day}</span>)}</div><div className="grid grid-cols-7 gap-1 sm:gap-2">
              {Array.from({ length: calendarOffset(year, month) }, (_, index) => <div key={`space-${index}`} aria-hidden="true" />)}
              {data.days.map((day, index) => <button key={day.date} disabled={day.reason === "future"} aria-pressed={selectedDay === day.date} aria-label={`${portfolioDate(day.date)}: ${isKnown(changes[index]) ? `${nativeMoney(changes[index], currency)} value change, including cash flows${observedMode && day.observation_days ? ` over ${day.observation_days} days` : ""}` : "change unavailable"}`} title={isKnown(changes[index]) ? nativeMoney(changes[index], currency) : "No comparable observation"} onClick={() => setSelectedDay(day.date)} className={`flex min-h-[58px] min-w-0 flex-col justify-between rounded-lg border p-1 text-left transition-colors min-[360px]:p-1.5 sm:min-h-[74px] sm:p-2 ${cellTone(changes[index])} ${selectedDay === day.date ? "ring-2 ring-teal ring-offset-1" : "hover:ring-1 hover:ring-mist/30"} disabled:opacity-35`}><span className="flex w-full items-center justify-between gap-1 text-[10px] sm:text-xs"><span>{Number(day.date.slice(8))}</span>{observedMode && day.observation_days !== null && day.observation_days > 1 && <span className="text-[8px] opacity-75 sm:text-[10px]">{day.observation_days}d</span>}</span><span className="-mx-0.5 truncate text-[10px] font-semibold tabular-nums min-[360px]:mx-0 sm:text-xs"><span className="min-[360px]:hidden">{narrowCalendarChange(changes[index])}</span><span className="hidden min-[360px]:inline">{compactCalendarChange(changes[index])}</span></span></button>)}
            </div></> : <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">{data.months.map((period, index) => <button key={index} aria-label={`${MONTHS[index]} ${year}: ${isKnown(period.change) ? `${nativeMoney(period.change, currency)} value change` : "incomplete coverage"}. Open daily calendar.`} onClick={() => { setMonth(index + 1); setView("days"); }} className={`flex min-h-[86px] min-w-0 flex-col justify-between rounded-xl border p-3 text-left ${cellTone(period.change)} hover:ring-1 hover:ring-mist/30`}><span className="text-xs">{MONTHS[index]}</span><span className="truncate text-sm font-semibold tabular-nums">{compactCalendarChange(period.change)}</span><span className="text-[10px] opacity-75">{period.observed_days}/{period.expected_days} days</span></button>)}</div>}
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-mist"><span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-kelp/40" />Value increased</span><span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-coral/40" />Value decreased</span><span>— No comparable data</span></div>
            {selected && view === "days" && <div className="mt-4 rounded-xl border border-line bg-surface2/35 p-3 text-xs" role="status"><div className="flex flex-wrap justify-between gap-2"><p className="font-semibold">{portfolioDate(selected.date)}</p><p className={`font-semibold tabular-nums ${gainTone(selectedChange)}`}>{nativeMoney(selectedChange, currency)}</p></div><dl className="mt-3 grid grid-cols-2 gap-3"><div><dt className="text-mist">{observedMode && selected.observation_start_date ? portfolioDate(selected.observation_start_date) : "Previous day"}</dt><dd className="mt-1 tabular-nums">{nativeMoney(selectedStart, currency)}</dd></div><div><dt className="text-mist">Account value</dt><dd className="mt-1 tabular-nums">{nativeMoney(selected.end_value, currency)}</dd></div></dl><p className="mt-3 text-xs text-mist">{isKnown(selectedChange) ? `${observedMode && selected.observation_days && selected.observation_days > 1 ? `${selected.observation_days}-day interval. ` : ""}Change includes cash flows. Personal profit is unavailable.` : `${selected.known_accounts}/${selected.selected_accounts} accounts known on this date. Both comparison dates need complete coverage.`}</p></div>}
          </div>
          <div className="min-w-0 border-t border-line p-3 sm:p-5 lg:border-l lg:border-t-0">
            <div className="flex flex-wrap items-center justify-between gap-2"><Heading level={4} variant="subsection">{view === "months" ? "Monthly value change" : chart === "value" ? "Account value" : observedMode ? "Between-observation change" : "Daily value change"} · {currency}</Heading>{view === "days" && <div role="group" aria-label="Calendar chart measure" className="flex rounded-lg bg-surface2 p-1">{(["value", "change"] as const).map((value) => <button key={value} aria-pressed={chart === value} onClick={() => setChart(value)} className={`min-h-11 rounded-md px-2 text-xs ${chart === value ? "bg-white font-semibold shadow-sm" : "text-mist"}`}>{value === "value" ? "Value" : "Change"}</button>)}</div>}</div>
            {chartValues.some(isKnown) ? <LedgerChart option={option} notMerge style={{ height: 245, width: "100%", minWidth: 0 }} onEvents={bars ? { click: (event: { dataIndex: number }) => { if (view === "months") { setMonth(event.dataIndex + 1); setView("days"); } else setSelectedDay(chartDates[event.dataIndex]); } } : undefined} /> : <div className="flex min-h-[245px] items-center justify-center"><Empty>No comparable {view === "months" ? "monthly" : "daily"} history in this period.</Empty></div>}
            <p className="text-xs leading-relaxed text-mist">{source === "estimate" ? "Estimated account values from SnapTrade." : "Snapshots saved by Am I Broke?."} Gaps stay unfilled. Account-value change includes deposits, withdrawals and FX.</p>
          </div>
        </div>
        <details className="border-t border-line px-4 text-xs text-mist sm:px-5"><summary className="cursor-pointer py-3">Calculation &amp; observations</summary><div className="space-y-2 pb-4">{data.methodology.map((line, index) => <p key={index}>{line}</p>)}<div className="max-h-60 overflow-auto pt-2"><DataTable className="w-full text-left text-xs"><thead><tr><th className="py-2 font-medium">Date</th><th className="py-2 text-right font-medium">Value · {currency}</th><th className="py-2 text-right font-medium">{comparison === "observed" ? "Interval change" : "Daily change"}</th></tr></thead><tbody>{data.days.filter((day) => day.reason !== "future").map((day) => { const change = comparison === "observed" ? day.observation_change : day.change; return <tr key={day.date} className="border-t border-line"><td className="py-2">{portfolioDate(day.date)}</td><td className="py-2 text-right tabular-nums">{nativeMoney(day.end_value, currency)}</td><td className={`py-2 text-right tabular-nums ${gainTone(change)}`}>{nativeMoney(change, currency)}{comparison === "observed" && day.observation_start_date && <span className="block text-[10px] font-normal text-mist">From {portfolioDate(day.observation_start_date)} · {day.observation_days} days</span>}</td></tr>; })}</tbody></DataTable></div></div></details>
      </>}
    </Card>
  </div>;
}
