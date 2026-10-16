import { Heading } from "./Heading";
import { useState } from "react";
import { api, type Summary } from "../api";
import { Async, Card, SectionTitle, useAsync } from "./ui";
import { NetChart, BalanceChart } from "./Charts";

export default function OverviewHistory({ accounts }: { accounts: Summary["accounts"] }) {
  const [interval, setInterval] = useState<"month" | "day">("month");
  const [scope, setScope] = useState("");
  const ts = useAsync(signal => api.timeseries(interval, scope || undefined, signal), [interval, scope]);
  return <div className="space-y-4">
      <Card>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <Heading level={2} variant="section">
            Income vs spending {interval === "day" ? "by day" : "by month"}
          </Heading>
          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Chart account"
              className="input min-w-0 py-1"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            >
              <option value="">All accounts</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name}
                </option>
              ))}
            </select>
            <div className="flex overflow-hidden rounded-lg border border-line/70">
              {(["month", "day"] as const).map((iv) => (
                <button
                  key={iv}
                  aria-pressed={interval === iv}
                  onClick={() => setInterval(iv)}
                  className={`px-3 py-1 text-sm capitalize transition-colors ${
                    interval === iv
                      ? "bg-teal/20 text-teal"
                      : "bg-surface2/40 text-mist hover:text-foam"
                  }`}
                >
                  {iv}
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="mb-3 text-xs text-mist">
          Cashflow shows recorded income and spending. Missing statements can make a
          month look quiet; cumulative net is cashflow over this imported
          history.
        </p>
        <Async state={ts}>{(t) => <NetChart net={t.net} />}</Async>
      </Card>

      <div className="space-y-5">
        <Card className="xl:col-span-3">
          <SectionTitle hint="saved balance points · gaps carried forward">
            Bank balance history
          </SectionTitle>
          <p className="mb-2 text-xs text-mist">
            GBP bank statement anchors and recorded transactions. Flat gaps carry
            forward a saved point; hover or tap for its date. Investment values are in Net assets.
          </p>
          <Async state={ts}>{(t) => <><BalanceChart ts={t} />{!!t.excluded_balance_accounts && <p className="mt-2 text-xs text-mist">{t.excluded_balance_accounts} investment or foreign-currency {t.excluded_balance_accounts === 1 ? "account is" : "accounts are"} excluded from this GBP bank history.</p>}</>}</Async>
        </Card>
      </div>
  </div>;
}
