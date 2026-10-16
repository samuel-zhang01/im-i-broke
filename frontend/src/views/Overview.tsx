import { Heading } from "../components/Heading";
import { lazy, Suspense } from "react";
import { api, type Summary } from "../api";
import {
  Async,
  Card,
  SectionTitle,
  useAsync,
  Pill,
  Metric,
  ErrorBox,
  DeferredSection,
  Spinner,
} from "../components/ui";
import { money, shortDate } from "../format";
import { accountColor } from "../theme";
import { fetchWealth } from "../wealthApi";
import { nativeMoney } from "../connectedPortfolio";
import { openBankAccount } from "../banking";
import { openFinancePage } from "../navigation";

const SpendingExplorer = lazy(() => import("../components/SpendingExplorer"));
const OverviewHistory = lazy(() => import("../components/OverviewHistory"));

function AccountCard({ s, onOpenInvestments }: { s: Summary; onOpenInvestments?: () => void }) {
  const banks = s.accounts.filter(account => account.account_type !== "investment");
  return (
    <Card>
      <SectionTitle hint={`${banks.length} bank accounts`}>
        Bank balances
      </SectionTitle>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {banks.slice(0, 6).map((a, i) => (
          <button
            key={a.id}
            onClick={() => openBankAccount(a.id)}
            className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line/50 bg-deep/40 px-3 py-2.5 text-left transition hover:border-teal/50 hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal"
          >
            <div className="flex min-w-0 flex-1 basis-36 items-center gap-3">
              <span
                className="h-8 w-1.5 shrink-0 rounded-full"
                style={{ background: accountColor(a.display_name, i) }}
              />
              <div className="min-w-0">
                <div className="break-words text-sm font-medium text-foam">
                  {a.display_name}
                </div>
                <div className="text-xs text-mist">
                  {a.account_type} ·{" "}
                  {a.balance_source === "lunchflow" ? (a.balance_stale ? "saved bank feed" : "connected") : a.reconciled === true
                    ? "reconciled"
                    : a.reconciled === false
                      ? "check statement"
                      : "unverified"}
                </div>
              </div>
            </div>
            <div className="ml-auto shrink-0 text-right tabular-nums">
              <div className="text-sm font-semibold text-foam">
                {nativeMoney(a.native_balance !== undefined ? a.native_balance : a.balance, a.currency || "GBP")}
              </div>
              <div className="text-xs text-mist">
                {a.balance_source === "lunchflow" ? "bank balance · fetched" : a.account_type === "credit"
                  ? "card position"
                  : "statement balance"}
                {a.balance_asof && (
                  <span className="block">{shortDate(a.balance_asof)}</span>
                )}
              </div>
            </div>
          </button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-3 border-t border-line pt-3"><button className="min-h-11 text-xs font-medium text-teal" onClick={() => openFinancePage("banking")}>View all banks &amp; accounts →</button>{onOpenInvestments && <button className="min-h-11 text-xs font-medium text-teal" onClick={onOpenInvestments}>View investments →</button>}</div>
    </Card>
  );
}

export default function Overview({ onOpenNetWorth, onOpenInvestments }: { onOpenNetWorth?: () => void; onOpenInvestments?: () => void } = {}) {
  const summary = useAsync(signal => api.summary(signal));
  const wealth = useAsync(signal => fetchWealth({ currency: "GBP", range: "all", accountIds: null }, signal));
  const spending = useAsync(signal => api.analytics({}, signal));
  const latest = spending.data?.monthly[spending.data.monthly.length - 1];
  const typical = spending.data?.stats.typical_monthly_spend;
  const lastComplete = [...(spending.data?.monthly ?? [])]
    .reverse()
    .find((m) => m.coverage_complete);

  return (
    <div className="space-y-5">
      <Async state={summary}>
        {(s) => (
          <>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="eyebrow">Your money, in context</p>
                <Heading level={2} variant="intro" className="mt-2">
                  The latest picture
                </Heading>
                <p className="mt-2 text-sm text-mist">
                  Recorded activity through {shortDate(s.date_range[1])}. Open an
                  account to explore its balance and available history.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">{wealth.data?.summary.partial && <Pill tone="amber">Some wealth data is incomplete</Pill>}{onOpenNetWorth && <button className="btn" onClick={onOpenNetWorth}>Open net assets <span aria-hidden="true">↗</span></button>}</div>
            </div>
            {spending.error && (
              <ErrorBox error={spending.error} retry={spending.refetch} />
            )}
            {wealth.error && <ErrorBox error={wealth.error} retry={wealth.refetch} />}
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Metric
                label={
                  wealth.data?.summary.partial ? "Known net assets · partial" : "Net assets"
                }
                value={money(wealth.data?.summary.net_worth)}
                tone="text-teal"
                detail={wealth.data ? `${money(wealth.data.summary.assets)} positive balances less ${money(wealth.data.summary.liabilities)} debt and negative balances` : "Loading bank and connected investment values"}
                explanation="Uses the same default accounts and latest local values as Net assets, including saved connected investment values. Foreign amounts use dated reference exchange rates where available. Sources can have different dates; incomplete, estimated or old values are labelled in the detailed view. Investment values may already include margin borrowing."
              />
              <Metric
                label={
                  latest
                    ? `${latest.m} recorded spending`
                    : "Latest recorded spending"
                }
                value={money(latest?.spend)}
                tone="text-coral"
                detail={
                  latest
                    ? latest.coverage_complete
                      ? "All relevant statements cover this month"
                      : "Partial month · missing statement coverage"
                    : "Waiting for monthly data"
                }
                explanation="Purchases less refunds in the latest month with imported activity. Own transfers, investment trades and saved exclusions are removed. A partial month is the amount recorded so far; it should not be compared with a full month."
              />
              <Metric
                label="Typical complete month"
                value={money(typical)}
                detail={
                  spending.data
                    ? `${spending.data.stats.complete_months} fully covered months available`
                    : "Checking statement coverage"
                }
                explanation="Median monthly spending using months with reconciled statement coverage across the relevant accounts. The median is the middle month, so a single expensive month has less influence. No complete months means no supported baseline."
              />
              <Metric
                label={
                  lastComplete
                    ? `${lastComplete.m} net cashflow`
                    : "Last complete month net"
                }
                value={money(lastComplete?.net)}
                tone={
                  lastComplete && lastComplete.net < 0
                    ? "text-coral"
                    : "text-kelp"
                }
                detail={
                  lastComplete
                    ? `${money(lastComplete.income)} income less ${money(lastComplete.spend)} spending`
                    : "Needs a fully covered month"
                }
                explanation="Income minus spending during the most recent fully covered month. This is observed cashflow, including investment income where present; it is not a promise of future savings. Planning separately avoids counting investment income again as a contribution."
              />
            </div>
            {latest && !latest.coverage_complete && (
              <div className="rounded-lg border border-amber/25 bg-amber/5 px-4 py-3 text-xs leading-relaxed text-mist">
                <strong className="text-foam">About {latest.m}:</strong>{" "}
                {latest.coverage_label}.{" "}
                {latest.missing_accounts.length > 0 &&
                  `Incomplete coverage: ${latest.missing_accounts.join(", ")}.`}{" "}
                Use the complete-month baseline for planning.
              </div>
            )}
            <AccountCard s={s} onOpenInvestments={onOpenInvestments} />
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-white px-4 py-3">
              <span className="mr-2 text-xs font-medium text-mist">Explore your activity</span>
              <button className="btn text-xs" onClick={() => openFinancePage("recurring")}>{s.recurring_count} recurring</button>
              <button className="btn text-xs" onClick={() => openFinancePage("anomalies")}>{s.anomaly_count} review flags</button>
              <button className="btn text-xs" onClick={() => openFinancePage("transfers")}>{s.transfer_count} transfer groups</button>
            </div>
          </>
        )}
      </Async>

      {summary.data && <>
      <DeferredSection label="Cashflow and balance history" minHeight={360}>
        <Suspense fallback={<Spinner label="Opening account history…" />}><OverviewHistory accounts={summary.data?.accounts || []} /></Suspense>
      </DeferredSection>
      <details className="rounded-xl border border-line bg-white px-4">
      <summary className="font-medium">Spending breakdown</summary>
      <DeferredSection label="Spending breakdown" minHeight={240}>
        <Suspense fallback={<Spinner label="Opening spending breakdown…" />}><SpendingExplorer /></Suspense>
      </DeferredSection>
      </details>
      </>}
    </div>
  );
}
