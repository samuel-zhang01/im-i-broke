import { Heading } from "../components/Heading";
import { Disclosure } from "../components/Disclosure";
import MobileDisclosure from "../components/MobileDisclosure";
import { DataTable } from "../components/DataTable";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import LedgerChart from "../components/LedgerChart";
import {
  api,
  type Planning as Plan,
  type PlanningQuery,
  type SavedScenario,
} from "../api";
import {
  Async,
  Card,
  Metric,
  Pill,
  SectionTitle,
  useAsync,
} from "../components/ui";
import { money, moneyShort, shortDate } from "../format";
import { C, axisBase, tooltipBase } from "../theme";

const DEFAULTS: PlanningQuery = {
  years: 10,
  annual_return: 5,
  inflation: 2,
  target: 100000,
  baseline_method: "mean",
  exclude_outliers: false,
  one_off_amount: 0,
  one_off_month: 1,
};
const COLORS = [C.amber, C.teal, C.seablue];

function formFields(q: PlanningQuery) {
  return {
    years: String(q.years),
    annual_return: String(q.annual_return),
    inflation: String(q.inflation),
    target: String(q.target),
    monthly_contribution:
      q.monthly_contribution == null ? "" : String(q.monthly_contribution),
    initial_wealth: q.initial_wealth == null ? "" : String(q.initial_wealth),
    one_off_amount: String(q.one_off_amount ?? 0),
    one_off_month: String(q.one_off_month ?? 1),
  };
}

function Projection({ plan }: { plan: Plan }) {
  const [real, setReal] = useState(true);
  const scenarios = plan.scenarios.filter((s) => s.points.length > 0);
  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <SectionTitle
          hint={`${plan.assumptions.years} years · contributions at month end`}
        >
          Wealth scenarios
        </SectionTitle>
        <div className="flex rounded-lg border border-line/70 p-0.5">
          {[
            { value: true, label: "Today’s money" },
            { value: false, label: "Future pounds" },
          ].map((o) => (
            <button
              key={o.label}
              aria-pressed={real === o.value}
              onClick={() => setReal(o.value)}
              className={`min-h-11 rounded-md px-3 py-1.5 text-xs ${real === o.value ? "bg-teal/15 text-teal" : "text-mist"}`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>
      {scenarios.length === 0 ? (
        <div className="py-12 text-center text-sm text-mist">
          A reliable monthly baseline is unavailable. Enter a monthly
          contribution above to explore a scenario.
        </div>
      ) : (
        <>
          <LedgerChart
            notMerge
            style={{ height: 340, width: "100%", minWidth: 0 }}
            option={{
              backgroundColor: "transparent",
              animation: false,
              aria: { enabled: true, description: `Illustrative wealth scenarios over ${plan.assumptions.years} years, in ${real ? "today's purchasing power" : "future pounds"}. Each line assumes a constant annual return; these are not predictions or probability bounds.` },
              color: COLORS,
              tooltip: {
                ...tooltipBase,
                trigger: "axis",
                valueFormatter: (v: number) => money(v),
              },
              legend: { top: 14, left: 0, right: 0, textStyle: { color: C.mist, fontSize: 11 }, type: "scroll", tooltip: { show: true } },
              grid: {
                left: 12,
                right: 16,
                top: 60,
                bottom: 30,
                containLabel: true,
              },
              xAxis: {
                ...axisBase,
                type: "category",
                boundaryGap: false,
                data: scenarios[0].points.map((p) =>
                  p.year === 0 ? "Start" : `Year ${p.year}`,
                ),
              },
              yAxis: {
                ...axisBase,
                type: "value",
                axisLabel: { ...axisBase.axisLabel, formatter: moneyShort },
              },
              series: scenarios.map((s) => ({
                name: `${s.name} · ${s.annual_return}%`,
                type: "line",
                symbol: "none",
                smooth: false,
                connectNulls: false,
                lineStyle: { width: 2.5 },
                data: s.points.map((p) => (real ? p.real : p.nominal)),
              })),
            }}
          />
          <div className="grid gap-3 sm:grid-cols-3">
            {scenarios.map((s, i) => {
              const last = s.points[s.points.length - 1];
              const months = s.target_month;
              return (
                <div
                  key={s.name}
                  className="rounded-xl border border-line/60 bg-deep/40 p-3"
                >
                  <div
                    className="text-xs font-medium"
                    style={{ color: COLORS[i % COLORS.length] }}
                  >
                    {s.name} · {s.annual_return}% annually
                  </div>
                  <div className="mt-2 text-xl font-semibold tabular-nums">
                    {money(real ? last.real : last.nominal)}
                  </div>
                  <div className="mt-1 text-xs text-mist">
                    {real ? "In today’s purchasing power" : "In future pounds"}
                  </div>
                  <div className="mt-2 text-xs text-mist">
                    {money(plan.assumptions.target)} nominal target:{" "}
                    {months == null
                      ? "not reached in this horizon"
                      : months === 0
                        ? "already reached"
                        : `${Math.floor(months / 12)}y ${months % 12}m`}
                  </div>
                  {months != null && !s.ending_above_target && (
                    <p className="mt-2 text-xs text-coral">
                      Ends below the target after withdrawals or negative
                      returns.
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
      {!!plan.assumptions.one_off_amount && (
        <p className="mt-4 rounded-lg border border-line bg-deep p-3 text-xs leading-relaxed">
          Includes {money(plan.assumptions.one_off_amount)} of planned spending
          in month {plan.assumptions.one_off_month}. The chart shows annual
          balances, so an expense within the year may not appear as a separate
          dip.
        </p>
      )}
      <p className="mt-4 text-xs leading-relaxed text-mist">Illustrations using constant returns. Cash, debt and investments behave differently.</p>
      <Disclosure title="Scenario assumptions & limits" variant="plain" className="mt-2">
        <p className="text-xs leading-relaxed text-mist">These are illustrations, not predictions or probabilities. Returns apply to the whole starting amount. Contributions stay fixed in pounds. Today’s money removes assumed inflation. The target date is the first crossing; a later withdrawal can bring wealth below it.</p>
      </Disclosure>
    </Card>
  );
}

export default function Planning() {
  const [query, setQuery] = useState<PlanningQuery>(DEFAULTS);
  const [fields, setFields] = useState(formFields(DEFAULTS));
  const [method, setMethod] = useState<"mean" | "median">("mean");
  const [outliers, setOutliers] = useState(false);
  const [scenarioName, setScenarioName] = useState("");
  const [selectedId, setSelectedId] = useState<number | undefined>();
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [saveError, setSaveError] = useState("");
  const saved = useAsync(signal => api.scenarios(signal), []);
  const plan = useAsync(signal => api.planning(query, signal), [query]);
  const formRef = useRef<HTMLFormElement>(null);
  const planningChecks = useRef<HTMLDetailsElement>(null);
  const planningChecksSummary = useRef<HTMLElement>(null);
  const planningWarnings = plan.data?.insights.filter(item => item.severity === "warning") ?? [];
  const [invalid, setInvalid] = useState(false);
  const draft = useMemo<PlanningQuery>(() => ({
      years: Number(fields.years),
      annual_return: Number(fields.annual_return),
      inflation: Number(fields.inflation),
      target: Number(fields.target),
      monthly_contribution:
        fields.monthly_contribution.trim() === ""
          ? undefined
          : Number(fields.monthly_contribution),
      initial_wealth:
        fields.initial_wealth.trim() === ""
          ? undefined
          : Number(fields.initial_wealth),
      baseline_method: method,
      exclude_outliers: outliers,
      one_off_amount: Number(fields.one_off_amount),
      one_off_month: Number(fields.one_off_month),
    }), [fields, method, outliers]);
  const dirty = Object.entries(draft).some(([key, value]) => value !== query[key as keyof PlanningQuery]);
  useEffect(() => {
    const valid = formRef.current?.checkValidity() ?? false;
    setInvalid(!valid);
    if (!valid || !dirty) return;
    const timer = setTimeout(() => { setQuery(draft); setSaveMessage(""); }, 400);
    return () => clearTimeout(timer);
  }, [draft, dirty]);
  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setQuery(draft);
    setSaveMessage("");
    if (!dirty) plan.refetch();
  }
  function loadScenario(scenario: SavedScenario) {
    const parameters = { ...DEFAULTS, ...scenario.parameters };
    setQuery(parameters);
    setFields(formFields(parameters));
    setMethod(parameters.baseline_method ?? "mean");
    setOutliers(parameters.exclude_outliers ?? false);
    setSelectedId(scenario.id);
    setScenarioName(scenario.name);
    setSaveMessage(`Loaded ${scenario.name}.`);
    setSaveError("");
  }
  async function saveScenario(asNew = false) {
    setSaving(true);
    setSaveError("");
    setSaveMessage("");
    try {
      const result = await api.saveScenario(
        scenarioName.trim(),
        query,
        asNew ? undefined : selectedId,
      );
      setSelectedId(result.id);
      setScenarioName(result.name);
      saved.refetch();
      setSaveMessage(`Saved ${result.name} to this database.`);
    } catch (error) {
      setSaveError(String(error));
    } finally {
      setSaving(false);
    }
  }
  async function removeScenario(scenario: SavedScenario) {
    setSaving(true);
    setSaveError("");
    setSaveMessage("");
    try {
      await api.deleteScenario(scenario.id);
      saved.refetch();
      if (selectedId === scenario.id) setSelectedId(undefined);
      setSaveMessage(
        `Deleted saved scenario ${scenario.name}. Your current assumptions remain available.`,
      );
    } catch (error) {
      setSaveError(String(error));
    } finally {
      setSaving(false);
    }
  }
  function reviewPlanningChecks() {
    if (!planningChecks.current) return;
    planningChecks.current.open = true;
    planningChecksSummary.current?.focus({ preventScroll: true });
    planningChecksSummary.current?.scrollIntoView({ block: "start", behavior: "auto" });
  }
  return (
    <div className="space-y-5">
      <div className="max-w-3xl">
        <p className="eyebrow hidden sm:block">Planning</p>
        <Heading level={2} variant="intro" className="hidden mt-2 text-foam sm:block">
          Make a plan you can explain.
        </Heading>
        <p className="mt-2 text-sm leading-relaxed text-mist">
          Explore saving scenarios using your recorded finances and editable assumptions.
        </p>
      </div>
      <MobileDisclosure label="Scenario assumptions" summary={`${fields.years} years · ${fields.annual_return}% annual growth${invalid ? " · check assumptions" : dirty ? " · changes pending" : ""}`}><Card>
        <SectionTitle hint="editable assumptions · annual rates">
          Build a scenario
        </SectionTitle>
        <form ref={formRef} onSubmit={apply} className="space-y-3">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {(
              [
                {
                  key: "initial_wealth",
                  label: "Starting wealth (£)",
                  hint: "Blank uses known wealth",
                  min: -1e9,
                  max: 1e9,
                  step: "0.01",
                  required: false,
                },
                {
                  key: "monthly_contribution",
                  label: "Monthly saving / withdrawal (£)",
                  hint: "Blank uses observed surplus; negative means withdrawal",
                  min: -1000000,
                  max: 1000000,
                  step: "0.01",
                  required: false,
                },
                {
                  key: "years",
                  label: "Projection years",
                  hint: "Time horizon",
                  min: 1,
                  max: 50,
                  step: "1",
                  required: true,
                },
                {
                  key: "annual_return",
                  label: "Annual growth (%)",
                  hint: "Effective annual rate before inflation",
                  min: -50,
                  max: 50,
                  step: "0.1",
                  required: true,
                },
                {
                  key: "inflation",
                  label: "Annual inflation (%)",
                  hint: "Used to calculate today’s purchasing power",
                  min: 0,
                  max: 20,
                  step: "0.1",
                  required: true,
                },
                {
                  key: "target",
                  label: "Wealth target (£)",
                  hint: "A target in future pounds",
                  min: 0.01,
                  max: 1e10,
                  step: "0.01",
                  required: true,
                },
              ] as const
            ).map((f) => (
              <label key={f.key} className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-foam">{f.label}</span>
                <input
                  className="input w-full"
                  type="number"
                  min={f.min}
                  max={f.max}
                  step={f.step}
                  required={f.required}
                  value={fields[f.key]}
                  placeholder={
                    f.key === "initial_wealth"
                      ? money(plan.data?.wealth.known_net_worth)
                      : f.key === "monthly_contribution"
                        ? money(plan.data?.baseline.selected_contribution)
                        : undefined
                  }
                  onChange={(e) =>
                    setFields((prev) => ({
                      ...prev,
                      [f.key]: e.target.value,
                      ...(f.key === "years" && Number(e.target.value) >= 1
                        ? {
                            one_off_month: String(
                              Math.min(
                                Number(prev.one_off_month),
                                Number(e.target.value) * 12,
                              ),
                            ),
                          }
                        : {}),
                    }))
                  }
                />
                <span className="text-xs leading-relaxed text-mist">
                  {f.hint}
                </span>
              </label>
            ))}
          </div>
          <Disclosure title="Automatic contribution settings"><div className="grid gap-4 lg:grid-cols-2">
            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium">
                Automatic monthly contribution
              </span>
              <select
                aria-label="Contribution baseline"
                className="input"
                value={method}
                onChange={(event) =>
                  setMethod(event.target.value as "mean" | "median")
                }
              >
                <option value="mean">Mean of covered monthly surpluses</option>
                <option value="median">
                  Median of covered monthly surpluses
                </option>
              </select>
              <span className="text-xs leading-relaxed text-mist">
                Mean averages covered monthly surpluses; median takes the middle
                value and reduces windfall effects. A manual amount overrides this.
              </span>
            </label>
            <div>
              <label className="flex items-start gap-2 text-sm">
                <input
                  className="mt-1 accent-teal"
                  type="checkbox"
                  checked={outliers}
                  onChange={(event) => setOutliers(event.target.checked)}
                />
                <span>Explore a baseline without flagged outliers</span>
              </label>
              <p className="mt-2 text-xs leading-relaxed text-mist">
                Sets flagged charges aside for this hypothetical baseline. They
                may be real costs. Refunds and unresolved duplicates remain included;
                cash buffers use recorded spending.
              </p>
            </div>
          </div></Disclosure>
          <Disclosure
            title="Add a planned one-off expense"
            open={Number(fields.one_off_amount) > 0 || undefined}
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="field-label">
                Expense amount (£)
                <input
                  className="input"
                  type="number"
                  min="0"
                  max="1000000000"
                  step="0.01"
                  required
                  value={fields.one_off_amount}
                  onChange={(event) =>
                    setFields((prev) => ({
                      ...prev,
                      one_off_amount: event.target.value,
                    }))
                  }
                />
              </label>
              <label className="field-label">
                Expense in month
                <input
                  className="input"
                  type="number"
                  min="1"
                  max={Number(fields.years) * 12 || 600}
                  step="1"
                  required
                  value={fields.one_off_month}
                  onChange={(event) =>
                    setFields((prev) => ({
                      ...prev,
                      one_off_month: event.target.value,
                    }))
                  }
                />
              </label>
            </div>
            <p className="mt-3 text-xs leading-relaxed text-mist">
              Month 1 is the first month from the scenario start. The expense is
              a fixed amount withdrawn after that month's contribution and
              growth. Set the amount to zero to remove it.
            </p>
          </Disclosure>
          <div className="flex flex-wrap items-center gap-3">
            <button
              className="btn-primary"
              disabled={plan.loading}
              type="submit"
            >
              {plan.loading ? "Calculating…" : "Update now"}
            </button>
            <span className="text-xs text-mist">
              {invalid ? "Complete valid values to update the chart. Previous assumptions remain shown." : dirty ? "Updating after you finish typing…" : "Chart updates automatically as you change the assumptions."}
            </span>
          </div>
        </form>
      </Card></MobileDisclosure>
      <Disclosure title={<>Save or load a scenario <span className="ml-2 text-xs font-normal text-mist">{saved.data?.length ?? 0} saved locally</span></>}><div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="field-label w-full sm:w-auto sm:flex-1">
            Scenario name
            <input
              className="input w-full"
              maxLength={100}
              value={scenarioName}
              placeholder="e.g. House deposit"
              onChange={(event) => setScenarioName(event.target.value)}
            />
          </label>
          <button
            className="btn"
            disabled={
              saving || invalid || dirty || plan.loading || !!plan.error || !scenarioName.trim()
            }
            onClick={() => saveScenario()}
          >
            {saving ? "Saving…" : selectedId ? "Update saved scenario" : "Save shown scenario"}
          </button>
          {selectedId && (
            <button
              className="btn"
              disabled={
                saving || invalid || dirty || plan.loading || !!plan.error || !scenarioName.trim()
              }
              onClick={() => saveScenario(true)}
            >
              Save as new
            </button>
          )}
        </div>
        <p className="mt-2 text-xs text-mist">
          Saves the assumptions used in the chart once calculation finishes.
          Automatic amounts are recalculated from your records when a saved
          scenario is loaded.
        </p>
        {saveError && (
          <p role="alert" className="mt-3 text-sm text-coral">
            {saveError}
          </p>
        )}
        {saveMessage && (
          <p role="status" className="mt-3 text-sm text-kelp">
            {saveMessage}
          </p>
        )}
        {saved.error && (
          <p role="alert" className="mt-3 text-sm text-coral">
            Saved plans could not load.{" "}
            <button className="underline" onClick={saved.refetch}>
              Retry
            </button>
          </p>
        )}
        {saved.loading && (
          <p role="status" className="mt-3 text-xs text-mist">
            Loading saved scenarios…
          </p>
        )}
        {!saved.loading && !saved.error && saved.data?.length === 0 && (
          <p className="mt-3 text-xs text-mist">No saved scenarios yet.</p>
        )}
        {!!saved.data?.length && (
          <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {saved.data.map((scenario) => (
              <div
                key={scenario.id}
                className={`flex min-w-0 items-start gap-2 rounded-lg border p-3 ${selectedId === scenario.id ? "border-teal bg-teal/5" : "border-line"}`}
              >
                <button
                  className="min-w-0 flex-1 text-left"
                  onClick={() => loadScenario(scenario)}
                  disabled={saving}
                  aria-label={`Load scenario ${scenario.name}`}
                >
                  <span className="block truncate text-sm font-medium">
                    {scenario.name}
                  </span>
                  <span className="mt-1 block text-xs text-mist">
                    {scenario.parameters.years} years ·{" "}
                    {scenario.parameters.annual_return}% growth ·{" "}
                    {scenario.parameters.monthly_contribution == null
                      ? `${scenario.parameters.baseline_method ?? "mean"} baseline`
                      : `${money(scenario.parameters.monthly_contribution)}/month`}
                  </span>
                </button>
                <button
                  className="min-h-11 px-2 text-sm text-mist underline underline-offset-2 hover:text-coral"
                  disabled={saving}
                  aria-label={`Delete scenario ${scenario.name}`}
                  onClick={() => removeScenario(scenario)}
                >
                  Delete
                </button>
              </div>
            ))}
          </div>
        )}
      </div></Disclosure>
      <Async state={plan}>
        {(d) => (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Pill tone={d.wealth.is_partial ? "amber" : "teal"}>
                {d.wealth.is_partial
                  ? "Partial financial picture"
                  : "Imported account coverage"}
              </Pill>
              <span className="text-xs text-mist">
                Reviewed {shortDate(d.as_of)}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              <Metric
                label="Known wealth"
                value={money(d.wealth.known_net_worth)}
                detail={`${money(d.wealth.cash_assets + d.wealth.investment_cash)} cash + ${money(d.wealth.holdings_value)} holdings − ${money(d.wealth.liabilities)} debt`}
                explanation="Latest signed account balances, foreign investment cash converted at cached market FX, plus priced holdings. Credit debt reduces wealth. Accounts can have different statement dates; missing pensions, property and loans are not estimated. Joint accounts count in full."
                tone="text-teal"
              />
              <Metric
                label={`${d.baseline.baseline_method === "median" ? "Median" : "Mean"} monthly surplus`}
                value={money(d.baseline.selected_contribution)}
                detail={`${d.baseline.months} covered months · ${d.baseline.exclude_outliers ? "outliers set aside" : "recorded spending"}`}
                explanation="Income minus spending for each complete eligible month. The mean averages those monthly surpluses; the median takes the middle value. Transfers and investment income are removed. A manual contribution changes the scenario, not this observed baseline."
                tone={
                  d.baseline.selected_contribution != null &&
                  d.baseline.selected_contribution < 0
                    ? "text-coral"
                    : "text-kelp"
                }
              />
              <Metric
                label="Cash runway"
                value={
                  d.resilience.runway_months == null
                    ? "—"
                    : `${d.resilience.runway_months.toFixed(1)} months`
                }
                detail="Bank cash less debt ÷ recorded monthly spending"
                explanation="Available bank cash after all recorded debt, floored at zero, divided by mean spending from covered months. Investment cash and holdings are excluded. This uses spending before the optional outlier filter and assumes no further income."
              />
              <Metric
                label="Six-month reserve"
                value={money(d.resilience.six_month_buffer)}
                detail={`Three months: ${money(d.resilience.three_month_buffer)}`}
                explanation="Six times mean recorded spending from covered months, before the optional outlier filter. This is a reference amount based on all spending, not a claim that every expense is essential or recurring."
              />
            </div>
            {planningWarnings.length > 0 && <div role="status" className="rounded-lg border border-amber/30 bg-amber/5 px-4 py-3">
              <p className="text-sm font-medium text-amber">{planningWarnings.length} planning {planningWarnings.length === 1 ? "check needs" : "checks need"} review</p>
              <p className="mt-1 text-xs leading-relaxed text-mist">{planningWarnings.map(item => item.title).join(" · ")}</p>
              <button className="btn mt-2" onClick={reviewPlanningChecks}>Review planning checks</button>
            </div>}
            <p className="text-xs text-mist">
              Scenario starting wealth: {money(d.assumptions.initial_wealth)} ·
              monthly contribution: {money(d.assumptions.monthly_contribution)}{" "}
              · {d.assumptions.contribution_source.replace(/_/g, " ")}
            </p>
            <Projection plan={d} />
            {d.insights.length > 0 && <Disclosure ref={planningChecks} summaryRef={planningChecksSummary} title={`Planning checks & insights (${d.insights.length})`}>
              <div className="grid gap-4 md:grid-cols-2">
                {d.insights.map((item, i) => <section key={i} className={`border-l-2 pl-3 ${item.severity === "warning" ? "border-amber/40" : "border-line"}`}>
                  <Heading level={3} variant="subsection" className={item.severity === "warning" ? "text-amber" : "text-foam"}>{item.title}</Heading>
                  <p className="mt-1 text-xs leading-relaxed text-mist">{item.detail}</p>
                </section>)}
              </div>
            </Disclosure>}
            <div className="grid items-start gap-5 lg:grid-cols-2">
              <Disclosure title={<>Baseline behind the maths <span className="ml-2 text-xs font-normal text-mist">{d.baseline.months} complete months</span></>}>
                <dl className="space-y-3 text-sm">
                  {[
                    ["Average eligible income", money(d.baseline.avg_income)],
                    [
                      "Average recorded spending",
                      money(d.baseline.recorded_avg_spending),
                    ],
                    [
                      "Recorded mean surplus",
                      money(d.baseline.recorded_avg_surplus),
                    ],
                    ["Selected mean surplus", money(d.baseline.avg_surplus)],
                    [
                      "Selected median surplus",
                      money(d.baseline.median_surplus),
                    ],
                    [
                      "Selected savings rate",
                      d.baseline.savings_rate == null
                        ? "—"
                        : `${(d.baseline.savings_rate * 100).toFixed(1)}%`,
                    ],
                    [
                      "Recurring monthly estimate",
                      money(d.resilience.recurring_monthly),
                    ],
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-3">
                      <dt className="text-mist">{label}</dt>
                      <dd className="tabular-nums">{value}</dd>
                    </div>
                  ))}
                </dl>
                <p className="mt-4 text-xs leading-relaxed text-mist">
                  Months {d.baseline.period_start ?? "—"} –{" "}
                  {d.baseline.period_end ?? "—"}. Eligible income excludes
                  transfers, investment income and recognised bank interest to
                  avoid counting returns twice. Recurring costs are already part
                  of spending; they are not subtracted again.
                </p>
                {d.baseline.exclude_outliers && (
                  <p className="mt-3 rounded-lg bg-amber/10 p-3 text-xs text-amber">
                    {d.baseline.excluded_outlier_count} charges totalling{" "}
                    {money(d.baseline.excluded_outlier_spend)} are set aside in
                    the selected baseline. This does not create real savings.
                  </p>
                )}
                {d.baseline.monthly.length > 0 && (
                  <details className="mt-4 text-xs">
                    <summary className="min-h-11 cursor-pointer py-3 text-teal">
                      Inspect every month in the baseline
                    </summary>
                    <div className="mt-3 overflow-x-auto">
                      <DataTable className="w-full text-right tabular-nums">
                        <thead>
                          <tr className="border-b border-line text-mist">
                            <th className="py-2 pr-3 text-left font-medium">
                              Month
                            </th>
                            <th className="px-2 py-2 font-medium">Income</th>
                            <th className="px-2 py-2 font-medium">
                              Recorded spend
                            </th>
                            <th className="px-2 py-2 font-medium">
                              Selected surplus
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {d.baseline.monthly.map((month) => (
                            <tr
                              key={month.month}
                              className="border-b border-line/50"
                            >
                              <td className="whitespace-nowrap py-2 pr-3 text-left">
                                {month.month}
                              </td>
                              <td className="whitespace-nowrap px-2 py-2">
                                {money(month.income)}
                              </td>
                              <td className="whitespace-nowrap px-2 py-2">
                                {money(month.recorded_spending)}
                              </td>
                              <td
                                className={`whitespace-nowrap px-2 py-2 ${month.surplus < 0 ? "text-coral" : "text-kelp"}`}
                              >
                                {money(month.surplus)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </DataTable>
                    </div>
                  </details>
                )}
                {d.baseline.excluded_months.length > 0 && (
                  <details className="mt-4 text-xs text-mist">
                    <summary className="min-h-11 cursor-pointer py-3 text-teal">
                      {d.baseline.excluded_months.length} months excluded from
                      the baseline
                    </summary>
                    <ul className="mt-2 max-h-44 space-y-1 overflow-y-auto">
                      {d.baseline.excluded_months.map((m) => (
                        <li key={m.month}>
                          {m.month}: {m.reason}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </Disclosure>
              <Disclosure title="Statement checks">
                <div className="flex flex-wrap gap-2">
                  <Pill tone="kelp">
                    {d.quality.verified_statements} reconciled
                  </Pill>
                  <Pill tone={d.quality.failed_statements ? "coral" : "mute"}>
                    {d.quality.failed_statements} failed
                  </Pill>
                  <Pill tone="amber">
                    {d.quality.unverified_statements} unverified
                  </Pill>
                </div>
                <p className="mt-4 text-sm text-mist">
                  {d.quality.uncategorized_count} uncategorized transactions
                  across all imported history account for{" "}
                  {money(d.quality.uncategorized_spend)} in spending.{" "}
                  {d.quality.undated_transactions} transactions have no date.
                </p>
                <p className="mt-3 text-xs leading-relaxed text-mist">
                  Reconciliation checks statement arithmetic. It does not
                  establish that all accounts or date ranges have been imported.
                </p>
              </Disclosure>
            </div>
            <Card className="overflow-hidden p-0">
              <div className="p-4 pb-1">
                <SectionTitle hint="latest imported observation per account">
                  What is included in wealth
                </SectionTitle>
              </div>
              <div className="overflow-x-auto">
                <DataTable className="w-full text-sm">
                  <thead className="bg-surface2/70 text-left text-xs uppercase tracking-wider text-mist">
                    <tr>
                      {[
                        "Account",
                        "As of",
                        "Age",
                        "Check",
                        "Wealth contribution",
                      ].map((h) => (
                        <th
                          key={h}
                          className="whitespace-nowrap px-4 py-3 font-medium last:text-right"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {d.wealth.accounts.map((a) => (
                      <tr key={a.id} className="border-t border-line/40">
                        <td className="px-4 py-3">
                          <div className="font-medium">{a.name}</div>
                          <div className="text-xs capitalize text-mist">
                            {a.type}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-mist">
                          {shortDate(a.balance_asof)}
                        </td>
                        <td className="px-4 py-3 text-mist">
                          {a.age_days == null ? "—" : `${a.age_days}d`}
                        </td>
                        <td className="px-4 py-3">
                          <Pill tone={a.reconciled === true ? "kelp" : "amber"}>
                            {a.reconciled === true
                              ? "Reconciled"
                              : a.reconciled === false
                                ? "Review"
                                : "Unverified"}
                          </Pill>
                        </td>
                        <td
                          className={`whitespace-nowrap px-4 py-3 text-right tabular-nums ${a.balance != null && a.balance < 0 ? "text-coral" : "text-foam"}`}
                        >
                          {money(a.balance)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </DataTable>
              </div>
              {d.wealth.accounts.length === 0 && (
                <p className="p-6 text-center text-sm text-mist">
                  Connect your accounts in Banks & accounts to see them here.
                </p>
              )}
              <p className="p-4 text-xs text-mist">
                Investment account rows show cash only; priced holdings are
                added separately. Unknown balances are excluded.
              </p>
            </Card>
            <Disclosure title="How the calculations work">
                <ul className="list-disc space-y-2 pl-5 text-xs leading-relaxed text-mist">
                  {d.methodology.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
            </Disclosure>
          </>
        )}
      </Async>
    </div>
  );
}
