import { DataTable } from "../components/DataTable";
import { Disclosure } from "../components/Disclosure";
import { useEffect, useRef, useState } from "react";
import { api, type AnalyticsQuery } from "../api";
import {
  Async,
  Card,
  ErrorBox,
  Metric,
  Pill,
  SectionTitle,
  useAsync,
} from "../components/ui";
import {
  AccountBars,
  NetChart,
} from "../components/Charts";
import SpendingExplorer from "../components/SpendingExplorer";
import { money } from "../format";
import { openFinancePage } from "../navigation";

const defaults: AnalyticsQuery = {
  date_from: `${new Date().getFullYear()}-01-01`,
  exclude_outliers: false,
  outlier_z: 3.5,
};
export default function Analytics() {
  const [query, setQuery] = useState<AnalyticsQuery>(defaults);
  const [section, setSection] = useState<"breakdown" | "cashflow" | "accounts">("breakdown");
  const [ready, setReady] = useState(false),
    [saveError, setSaveError] = useState("");
  const accounts = useAsync(signal => api.summary(signal));
  const saves = useRef(Promise.resolve());
  const saveVersion = useRef(0);
  useEffect(() => {
    let active = true;
    api
      .preferences<{ query?: AnalyticsQuery }>("spending")
      .then((p) => {
        if (active) setQuery(p.value.query ?? defaults);
      })
      .catch((e) => {
        if (active) setSaveError(`Could not load saved filters. ${String(e)}`);
      })
      .finally(() => {
        if (active) setReady(true);
      });
    return () => {
      active = false;
    };
  }, []);
  const data = useAsync(
    async signal => ({ ...await api.analytics(query, signal), appliedQuery: query }),
    [JSON.stringify(query)],
    { enabled: ready },
  );
  function update(patch: Partial<AnalyticsQuery>) {
    const version = ++saveVersion.current;
    const next = { ...query, ...patch };
    setQuery(next);
    setSaveError("");
    saves.current = saves.current
      .catch(() => {})
      .then(async () => {
        await api.savePreferences("spending", { query: next });
      })
      .catch((e) => {
        // An older failed save must not label newer, successfully saved filters
        // as unsaved. Writes remain queued so the latest selection wins on disk.
        if (version === saveVersion.current) setSaveError(
          `Filters changed here but could not be saved. ${String(e)}`,
        );
      });
  }
  return (
    <div className="space-y-5">
      <Disclosure title={<>Filters · {query.account ? accounts.data?.accounts.find(a => a.id === query.account)?.display_name || "Selected account" : "All accounts"} · {query.date_from || "All history"}{query.date_to ? ` – ${query.date_to}` : " onwards"}</>}>
        <div className="space-y-4">
        <div className="grid grid-cols-2 items-end gap-3 sm:grid-cols-4 xl:grid-cols-[minmax(220px,1fr)_auto_auto_auto_auto]">
          <label className="field-label col-span-2 xl:col-span-1">
            Account
            <select
              aria-label="Spending account"
              className="input mt-1 block"
              value={query.account ?? ""}
              disabled={!ready}
              onChange={(e) => update({ account: e.target.value || undefined })}
            >
              <option value="">All accounts</option>
              {accounts.data?.accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="field-label">
            From
            <input
              aria-label="Spending from date"
              type="date"
              className="input mt-1 block"
              max={query.date_to}
              value={query.date_from ?? ""}
              disabled={!ready}
              onChange={(e) =>
                update({ date_from: e.target.value || undefined })
              }
            />
          </label>
          <label className="field-label">
            To
            <input
              aria-label="Spending to date"
              type="date"
              className="input mt-1 block"
              min={query.date_from}
              value={query.date_to ?? ""}
              disabled={!ready}
              onChange={(e) => update({ date_to: e.target.value || undefined })}
            />
          </label>
          <button
            className="btn"
            disabled={!ready}
            onClick={() =>
              update({ date_from: defaults.date_from, date_to: undefined })
            }
          >
            This year
          </button>
          <button
            className="btn"
            disabled={!ready}
            onClick={() => update({ date_from: undefined, date_to: undefined })}
          >
            All history
          </button>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
          <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={!!query.exclude_outliers}
              disabled={!ready}
              onChange={(e) => update({ exclude_outliers: e.target.checked })}
            />
            Hide amount outliers from this view
          </label>
          <label className="flex items-center gap-2 text-xs text-mist">
            Sensitivity
            <select
              aria-label="Outlier sensitivity"
              className="input"
              value={query.outlier_z ?? 3.5}
              disabled={!ready}
              onChange={(e) => update({ outlier_z: Number(e.target.value) })}
            >
              <option value={2.5}>More flags</option>
              <option value={3.5}>Balanced</option>
              <option value={5}>Fewer flags</option>
            </select>
          </label>
        </div>
        <p className="text-xs text-mist">
          Filters are saved in your database. Hiding a cost changes this
          analysis, not your actual balance or the transaction record.
        </p>
        </div>
      </Disclosure>
      {saveError && <ErrorBox error={saveError} />}
      <Async state={{ ...data, loading: !ready || data.loading }}>
        {(d) => {
          const s = d.stats;
          let cumulative = 0;
          return (
            <>
              <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
                <Metric
                  label={
                    d.appliedQuery.exclude_outliers
                      ? "Adjusted spending"
                      : "Recorded spending"
                  }
                  value={money(s.total_spend)}
                  detail={`${money(s.gross_spend)} outgoing − ${money(s.refunds)} refunds`}
                  explanation={d.definitions.spending}
                />
                <Metric
                  label="Typical complete month"
                  value={money(s.typical_monthly_spend)}
                  detail={`${s.complete_months} complete months in the selection`}
                  explanation={d.definitions.typical_monthly_spend}
                />
                <Metric
                  label="Income"
                  value={money(s.total_income)}
                  detail={`${s.months} calendar months selected`}
                  explanation={d.definitions.income}
                  tone="text-kelp"
                />
                <Metric
                  label={
                    d.appliedQuery.exclude_outliers
                      ? "Adjusted cash surplus"
                      : "Recorded cash surplus"
                  }
                  value={money(s.total_net)}
                  detail={
                    s.savings_rate == null
                      ? "Savings rate unavailable"
                      : `${(s.savings_rate * 100).toFixed(1)}% of included income`
                  }
                  explanation={`${d.definitions.net} ${d.definitions.savings_rate}`}
                  tone={s.total_net >= 0 ? "text-kelp" : "text-coral"}
                />
              </div>
              {d.appliedQuery.exclude_outliers && (
                <Card className="border-amber/40">
                  <p className="text-sm font-semibold">
                    {s.excluded_outlier_count} flagged payments hidden ·{" "}
                    {money(s.excluded_outlier_spend)}
                  </p>
                  <p className="mt-1 text-xs text-mist">
                    Spending before this filter: {money(s.recorded_spend)}.
                    These may be real costs; the difference is not extra money
                    available to save.
                  </p>
                </Card>
              )}
              <Disclosure title={`What the recorded data shows (${d.insights.length})`}><div className="grid gap-3 lg:grid-cols-2">
                {d.insights.map((i, n) => (
                  <div
                    key={n}
                    className="rounded-lg border-l-2 border-line bg-surface2/65 px-4 py-3"
                  >
                    <p className="text-sm font-semibold">{i.title}</p>
                    <p className="mt-1 text-xs leading-relaxed text-mist">
                      {i.detail}
                    </p>
                  </div>
                ))}
              </div></Disclosure>
              <div className="segmented-control" role="group" aria-label="Spending sections">
                {([['breakdown', 'Categories & merchants'], ['cashflow', 'Cashflow & history'], ['accounts', 'Accounts & data']] as const).map(([id, label]) => <button key={id} aria-pressed={section === id} className={section === id ? 'segment active' : 'segment'} onClick={() => setSection(id)}>{label}</button>)}
              </div>
              {!s.complete_months && <p className="rounded-lg border border-line bg-white px-4 py-3 text-xs leading-relaxed text-mist">Recorded totals are available, but no selected month has verified statement coverage across every account. Bank feeds do not establish complete historical coverage. <button className="min-h-11 font-medium text-teal underline underline-offset-2" onClick={() => { setSection('accounts'); }}>Inspect accounts & data</button></p>}
              {section === 'breakdown' && <div className="space-y-4">
                <SpendingExplorer query={d.appliedQuery} />
              </div>}
              {section === 'cashflow' && <div className="space-y-4">
              <div>
                <Card>
                  <SectionTitle
                    hint={`${d.comparison.previous_month ?? "—"} → ${d.comparison.current_month ?? "—"}`}
                  >
                    What changed
                  </SectionTitle>
                  <p className="mb-3 text-xs text-mist">
                    {d.comparison.comparable
                      ? "Both months have complete bank/card coverage."
                      : "Coverage is incomplete: these differences are descriptive, not a like-for-like trend."}{" "}
                    {d.comparison.explanation}
                  </p>
                  {d.drivers.slice(0, 8).map((r) => (
                    <div
                      key={r.category}
                      className="flex items-center justify-between gap-4 border-t border-line py-2.5 text-sm"
                    >
                      <span>
                        {r.category}
                        <span className="mt-0.5 block text-xs text-mist">
                          {money(r.previous_spend)} → {money(r.spend)}
                        </span>
                      </span>
                      <span
                        className={`tabular-nums ${r.change_amount > 0 ? "text-coral" : "text-kelp"}`}
                      >
                        {r.change_amount > 0 ? "+" : ""}
                        {money(r.change_amount)}
                      </span>
                    </div>
                  ))}
                  {!d.drivers.length && (
                    <p className="text-sm text-mist">
                      Select at least two months to compare categories.
                    </p>
                  )}
                </Card>

              </div>
              <Card>
                <SectionTitle hint="same filters as every total below">
                  Month by month
                </SectionTitle>
                <NetChart
                  net={d.monthly.map((m) => ({
                    period: m.m,
                    income: m.income,
                    spending: -m.spend,
                    net: m.net,
                    cumulative: (cumulative += m.net),
                  }))}
                />
                <p className="text-xs text-mist">
                  Cashflow shows income and net spending. Cumulative net adds
                  the included surplus over the selected period. Missing statements
                  can leave incomplete months.
                </p>
              </Card>
              <Card className="overflow-hidden p-0">
                <div className="p-4">
                  <SectionTitle hint="refunds reduce spending">
                    Monthly ledger
                  </SectionTitle>
                </div>
                <div className="overflow-x-auto">
                  <DataTable className="w-full text-sm">
                    <thead className="bg-surface2 text-right text-xs text-mist">
                      <tr>
                        {[
                          "Month / coverage",
                          "Income",
                          "Gross spending",
                          "Refunds",
                          "Net spending",
                          "Surplus",
                        ].map((h, i) => (
                          <th
                            key={h}
                            className={`whitespace-nowrap px-4 py-3 font-medium ${i === 0 ? "text-left" : ""}`}
                          >
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {[...d.monthly].reverse().map((m) => (
                        <tr className="border-t border-line" key={m.m}>
                          <td className="px-4 py-3">
                            <p className="font-semibold">{m.m}</p>
                            <details className="mt-1 text-xs text-mist">
                              <summary className="cursor-pointer">
                                <Pill
                                  tone={m.coverage_complete ? "kelp" : "amber"}
                                >
                                  {m.coverage_complete
                                    ? "Complete"
                                    : "Partial / unverified"}
                                </Pill>
                              </summary>
                              <p className="mt-2 min-w-48 max-w-xs">
                                {m.coverage_label}
                                {m.missing_accounts.length > 0
                                  ? ` · ${m.missing_accounts.join(", ")}`
                                  : ""}
                              </p>
                            </details>
                          </td>
                          {[
                            m.income,
                            m.gross_spend,
                            m.refunds,
                            m.spend,
                            m.net,
                          ].map((v, i) => (
                            <td
                              key={i}
                              className={`whitespace-nowrap px-4 py-3 text-right tabular-nums ${i === 4 ? (v >= 0 ? "text-kelp" : "text-coral") : ""}`}
                            >
                              {money(v)}
                              {i === 3 && m.excluded_outlier_spend > 0 && (
                                <span className="block text-xs text-mist">
                                  {money(m.excluded_outlier_spend)} hidden
                                </span>
                              )}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </DataTable>
                </div>
                {!d.monthly.length && (
                  <p className="p-8 text-center text-sm text-mist">
                    No cash flows in this selection.
                  </p>
                )}
              </Card>
              </div>}
              {section === 'accounts' && <div className="space-y-4">
              <Card>
                <SectionTitle hint="money in and out are shown separately">
                  What is excluded
                </SectionTitle>
                <p className="mb-4 text-xs leading-relaxed text-mist">
                  {d.definitions.exclusions}
                </p>
                <div className="overflow-x-auto">
                  <DataTable className="w-full text-sm">
                    <thead className="text-left text-xs text-mist">
                      <tr>
                        <th className="py-2 font-medium">Reason</th>
                        <th className="px-3 py-2 text-right font-medium">
                          Rows
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          Money out
                        </th>
                        <th className="px-3 py-2 text-right font-medium">
                          Money in
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.exclusions.map((e) => (
                        <tr key={e.reason} className="border-t border-line">
                          <td className="py-3">
                            {e.reason.replace(/_/g, " ")}
                          </td>
                          <td className="px-3 text-right tabular-nums">
                            {e.count}
                          </td>
                          <td className="whitespace-nowrap px-3 text-right tabular-nums">
                            {money(e.outflow)}
                          </td>
                          <td className="whitespace-nowrap px-3 text-right tabular-nums">
                            {money(e.inflow)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </DataTable>
                </div>
                <details className="mt-5 text-xs leading-relaxed text-mist">
                  <summary className="cursor-pointer font-semibold text-teal">
                    How outliers are identified · {s.outlier_candidates}{" "}
                    candidates
                  </summary>
                  <p className="mt-2">{d.definitions.outliers}</p>
                </details>
              </Card>
              <Card>
                <SectionTitle>Cash flow by account</SectionTitle>
                <AccountBars data={d.by_account} />
              </Card>
                <div className="flex flex-wrap gap-2"><button className="btn" onClick={() => openFinancePage('banking')}>Inspect bank connections</button><button className="btn" onClick={() => openFinancePage('merchants')}>Review uncategorised names</button></div>
              </div>}
            </>
          );
        }}
      </Async>
    </div>
  );
}
