import { Heading } from "./Heading";
import { useEffect, useMemo, useState } from "react";
import LedgerChart from "./LedgerChart";
import { api, type AnalyticsQuery } from "../api";
import {
  getSpendingExplorer,
  spendBreakdown,
  readExplorerPreferences,
  saveExplorerPreferences,
} from "../spendingExplorer";
import { Card, ErrorBox, Metric, Spinner, useAsync } from "./ui";
import { money, moneyShort } from "../format";
import { C, axisBase, tooltipBase } from "../theme";
import { chartPeriodLabel } from "../chartData";
import { categoryColor } from "../bankingCharts";


export default function SpendingExplorer({
  query: externalQuery,
}: {
  query?: AnalyticsQuery;
}) {
  const preferenceKey = externalQuery ? "spending" : "overview";
  const [saved] = useState(() => readExplorerPreferences(preferenceKey));
  const [period, setPeriod] = useState(saved.period),
    [account, setAccount] = useState(saved.account);
  const [from, setFrom] = useState(saved.from),
    [to, setTo] = useState(saved.to);
  const [selected, setSelected] = useState<string[] | null>(saved.selected),
    [vendorSearch, setVendorSearch] = useState(saved.vendorSearch),
    [limit, setLimit] = useState(12);
  useEffect(
    () =>
      saveExplorerPreferences(preferenceKey, {
        period,
        account,
        from,
        to,
        selected,
        vendorSearch,
      }),
    [preferenceKey, period, account, from, to, selected, vendorSearch],
  );
  const [chartMode, setChartMode] = useState<"combined" | "categories">(
    saved.selected === null ? "combined" : "categories",
  );
  const [granularity, setGranularity] = useState<"month" | "week">("month");
  const accounts = useAsync(signal => api.summary(signal), [], { enabled: !externalQuery });
  const end = accounts.data?.date_range[1]?.slice(0, 10);
  const presetFrom =
    end && period === "year"
      ? `${end.slice(0, 4)}-01-01`
      : end && period === "12"
        ? `${Number(end.slice(0, 4)) - (Number(end.slice(5, 7)) === 12 ? 0 : 1)}-${String((Number(end.slice(5, 7)) % 12) + 1).padStart(2, "0")}-01`
        : undefined;
  const query = externalQuery ?? {
    account: account || undefined,
    date_from: period === "custom" ? from || undefined : presetFrom,
    date_to:
      period === "custom"
        ? to || undefined
        : period === "all"
          ? undefined
          : end,
  };
  const key = JSON.stringify(query),
    invalid = !!(
      query.date_from &&
      query.date_to &&
      query.date_from > query.date_to
    );
  const result = useAsync(
    async signal => ({ ...(await getSpendingExplorer({ ...query }, signal)), key }),
    [key],
    { enabled: !invalid && (Boolean(externalQuery) || Boolean(accounts.data)) },
  );
  const data = result.data?.key === key ? result.data : null;
  const totals = useMemo(
    () => spendBreakdown(data?.cells ?? [], selected),
    [data, selected],
  );
  const names = totals.categories.map((c) => c.category);
  const displayed =
    selected === null ? names : selected.filter((c) => names.includes(c));
  const vendors = totals.vendors.filter((v) =>
    v.merchant.toLowerCase().includes(vendorSearch.toLowerCase()),
  );
  function toggle(name: string) {
    setChartMode("categories");
    setSelected((old) =>
      old === null
        ? [name]
        : old.includes(name)
          ? old.filter((n) => n !== name)
          : [...old, name],
    );
    setLimit(12);
  }
  const pie = {
    animation: false,
    aria: { enabled: true, description: "Positive net spending by category. Refund-only categories remain in the adjacent list. Category buttons provide the same selection as the chart." },
    color: names.map(categoryColor),
    tooltip: {
      ...tooltipBase,
      trigger: "item",
      valueFormatter: (v: number) => money(v),
    },
    series: [
      {
        type: "pie",
        radius: ["48%", "75%"],
        label: { show: false },
        selectedMode: "multiple",
        data: totals.categories
          .filter((c) => c.net > 0)
          .map((c) => ({
            name: c.category,
            value: c.net,
            selected: selected?.includes(c.category) ?? false,
            itemStyle: {
              color: categoryColor(c.category),
              opacity:
                selected === null || selected.includes(c.category) ? 1 : 0.25,
            },
          })),
      },
    ],
  };
  const periods = granularity === "week" ? data?.weeks ?? [] : data?.months ?? [];
  const trendCells = granularity === "week"
    ? (data?.weekly_cells ?? []).map(c => ({ ...c, month: c.week }))
    : data?.cells ?? [];
  const coverage = granularity === "week" ? data?.weekly_coverage : data?.coverage;
  const trend = {
    animation: false,
    aria: { enabled: true, description: `Recorded net spending by ${granularity} in pounds for the selected categories. Gaps have no observations and incomplete coverage. A low value can reflect missing statements.` },
    color: names.map(categoryColor),
    tooltip: {
      ...tooltipBase,
      trigger: "axis",
      valueFormatter: (v: number) => money(v),
    },
    legend: { type: "scroll", top: 0, textStyle: { color: C.mist } },
    grid: { left: 55, right: 15, top: 45, bottom: 55 },
    xAxis: { ...axisBase, type: "category", data: periods, axisLabel: { ...axisBase.axisLabel, formatter: chartPeriodLabel } },
    yAxis: { ...axisBase, type: "value", axisLabel: { ...axisBase.axisLabel, formatter: moneyShort } },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 15, bottom: 8 }],
    series: (chartMode === "combined" ? ["Selected total"] : displayed).map(
      (name) => ({
        name,
        type: "line",
        itemStyle: {
          color:
            chartMode === "combined"
              ? C.teal
              : categoryColor(name),
        },
        showSymbol: true,
        symbol: "circle",
        symbolSize: 4,
        connectNulls: false,
        data:
          periods.map((m) =>
            trendCells.some((c) => c.month === m) ||
            coverage?.[m]?.coverage_complete
              ? trendCells
                  .filter(
                    (c) =>
                      c.month === m &&
                      (chartMode === "combined"
                        ? displayed.includes(c.category)
                        : c.category === name),
                  )
                  .reduce((sum, c) => sum + c.net, 0)
              : null,
          ) ?? [],
      }),
    ),
  };
  return (
    <section aria-label="Spending explorer" className="space-y-4">
      <Card className="space-y-4">
        <div>
          <p className="eyebrow">Follow the money</p>
          <Heading level={2} variant="section" className="mt-1">Explore spending</Heading>
          <p className="mt-2 text-sm text-mist">
            Choose a category—or several—to compare weekly or monthly spending and see
            which merchants make up the total.
          </p>
        </div>
        {!externalQuery ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <label className="field-label">
              Timeframe
              <select
                className="input"
                aria-label="Explorer timeframe"
                value={period}
                onChange={(e) => setPeriod(e.target.value)}
              >
                <option value="all">All imported history</option>
                <option value="year">Latest imported year</option>
                <option value="12">Last 12 imported months</option>
                <option value="custom">Custom dates</option>
              </select>
            </label>
            <label className="field-label">
              Account
              <select
                className="input"
                aria-label="Explorer account"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
              >
                <option value="">All accounts</option>
                {accounts.data?.accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.display_name}
                  </option>
                ))}
              </select>
            </label>
            {period === "custom" && (
              <>
                <label className="field-label">
                  From
                  <input
                    aria-label="Explorer from date"
                    className="input"
                    type="date"
                    value={from}
                    onChange={(e) => setFrom(e.target.value)}
                  />
                </label>
                <label className="field-label">
                  To
                  <input
                    aria-label="Explorer to date"
                    className="input"
                    type="date"
                    value={to}
                    onChange={(e) => setTo(e.target.value)}
                  />
                </label>
              </>
            )}
          </div>
        ) : (
          <p className="text-xs text-mist">
            Uses the account, dates and outlier filter selected above.
          </p>
        )}
        {accounts.error && !externalQuery && (
          <ErrorBox error={accounts.error} retry={accounts.refetch} />
        )}
        {invalid ? (
          <p role="alert" className="text-sm text-coral">
            Choose an end date on or after the start date.
          </p>
        ) : result.error ? (
          <ErrorBox error={result.error} retry={result.refetch} />
        ) : !data ? (
          <Spinner label="Updating spending breakdown…" />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-mist">
              <span>
                {data.date_from ?? "No start date"} →{" "}
                {data.date_to ?? "No end date"} · recorded payments, not a
                complete spending history
              </span>
              <button
                className="btn"
                aria-pressed={selected === null}
                onClick={() => {
                  setSelected(null);
                  setChartMode("combined");
                  setVendorSearch("");
                  setLimit(12);
                }}
              >
                All categories
              </button>
            </div>
            {names.length === 0 ? (
              <p className="py-8 text-center text-sm text-mist">
                No spending or refunds in this period. Try another date range or
                account.
              </p>
            ) : (
              <div className="grid items-center gap-5 lg:grid-cols-[minmax(240px,1fr)_2fr]">
                <div className="min-w-0">
                  <LedgerChart
                    option={pie}
                    style={{ height: 260 }}
                    notMerge
                    onEvents={{
                      click: (event: { name: string }) => toggle(event.name),
                    }}
                  />
                  <p className="text-center text-xs text-mist">
                    Click a slice to select. Refund-only categories appear in
                    the list.
                  </p>
                </div>
                <div>
                  <p role="status" className="mb-3 text-sm font-medium">
                    {selected === null
                      ? "All categories"
                      : selected.length
                        ? `${displayed.length} categories selected: ${displayed.join(", ")}`
                        : "No categories selected"}
                  </p>
                  <div className="grid max-h-80 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
                    {totals.categories.map((c) => (
                      <button
                        key={c.category}
                        aria-pressed={selected?.includes(c.category) ?? false}
                        className={`flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-xs transition-colors ${selected?.includes(c.category) ? "border-teal bg-teal/5" : "border-line hover:bg-surface2"}`}
                        onClick={() => toggle(c.category)}
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span
                            className="h-2 w-2 shrink-0 rounded-full"
                            style={{ background: categoryColor(c.category) }}
                          />
                          {c.category}
                        </span>
                        <span className="shrink-0 tabular-nums">
                          {money(c.net)}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </Card>
      {data && !invalid && !result.error && names.length > 0 && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric
              explanation="Purchases minus refunds across the selected categories and date/account scope. Transfers and saved exclusions are omitted."
              label="Selected net spending"
              value={money(totals.net)}
              detail="Purchases less refunds"
            />
            <Metric
              explanation="Outgoing household expenses before refunds, within the selected categories. The count includes purchase and refund records."
              label="Purchases"
              value={money(totals.gross)}
              detail={`${totals.n} payment and refund records`}
            />
            <Metric
              explanation="Recognised refunds and reimbursements credited in this period. They can relate to purchases in an earlier period."
              label="Refunds"
              value={money(totals.refunds)}
              detail="Returned money offsets spending"
            />
          </div>
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Heading level={3} variant="subsection">
                {granularity === "week" ? "Weekly" : "Monthly"} spending · selected categories
              </Heading>
              <div className="flex flex-wrap items-center gap-3">
                <div className="segmented-control" role="group" aria-label="Spending chart interval">
                  {([['week', 'Weeks'], ['month', 'Months']] as const).map(([value, label]) => (
                    <button key={value} className={granularity === value ? "segment active" : "segment"}
                      aria-pressed={granularity === value} onClick={() => setGranularity(value)}>{label}</button>
                  ))}
                </div>
                <div
                  className="segmented-control"
                  role="group"
                  aria-label="Spending chart display"
                >
                  <button
                    className={
                      chartMode === "combined" ? "segment active" : "segment"
                    }
                    aria-pressed={chartMode === "combined"}
                    onClick={() => setChartMode("combined")}
                  >
                    Combined total
                  </button>
                  <button
                    className={
                      chartMode === "categories" ? "segment active" : "segment"
                    }
                    aria-pressed={chartMode === "categories"}
                    onClick={() => setChartMode("categories")}
                  >
                    Compare categories
                  </button>
                </div>
              </div>
            </div>
            <p className="mb-4 mt-1 text-xs text-mist">
              {granularity === "week" && "Weeks start on Monday; boundary weeks include only the selected dates. "}
              Recorded net amounts. Gaps have no observations and incomplete
              coverage; low points can also reflect missing statements.
            </p>
            {displayed.length ? (
              <LedgerChart key={granularity} option={trend} style={{ height: 320 }} notMerge />
            ) : (
              <p className="py-10 text-center text-mist">
                Select a category above to see its trend.
              </p>
            )}
          </Card>
          <Card>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div>
                <Heading level={3} variant="subsection">Merchant breakdown</Heading>
                <p className="mt-1 text-xs text-mist">
                  {vendors.length} matching merchants · selected categories · sorted by net spending
                </p>
              </div>
              <input
                className="input w-full sm:w-64"
                aria-label="Search explorer merchants"
                placeholder="Find a merchant…"
                value={vendorSearch}
                onChange={(e) => {
                  setVendorSearch(e.target.value);
                  setLimit(12);
                }}
              />
            </div>
            {vendors.length ? (
              <ul className="divide-y divide-line">
                {vendors.slice(0, limit).map((v) => (
                  <li
                    key={v.merchant}
                    className="flex items-start justify-between gap-3 py-3 text-sm"
                  >
                    <div className="min-w-0">
                      <p className="break-words font-medium">{v.merchant}</p>
                      <p className="mt-1 text-xs text-mist">
                        {v.n} {v.n === 1 ? "payment" : "payments"}
                        {v.refunds > 0 ? ` · ${money(v.refunds)} refunded` : ""}
                      </p>
                    </div>
                    <p className="shrink-0 font-semibold tabular-nums">
                      {money(v.net)}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="py-6 text-center text-sm text-mist">
                No merchants match this selection.
              </p>
            )}
            {vendors.length > limit && (
              <button
                className="btn mt-4 w-full"
                onClick={() => setLimit((n) => n + 20)}
              >
                Show more merchants ({vendors.length - limit} more)
              </button>
            )}
          </Card>
        </>
      )}
    </section>
  );
}
