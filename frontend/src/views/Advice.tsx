import { Heading } from "../components/Heading";
import { api } from "../api";
import { Async, Card, Pill, useAsync } from "../components/ui";
import { money } from "../format";
import { openFinancePage } from "../navigation";

const ICON: Record<string, string> = {
  subscriptions: "↻",
  price_change: "↗",
  fees: "£",
};

export default function Advice() {
  const advice = useAsync(() => api.advice());
  return (
    <Async state={advice} empty={<Card>No review suggestions were detected in the imported transactions.</Card>}>
      {(tips) => {
        return (
          <div className="space-y-4">
            <Card className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-lg font-semibold text-foam">Spending to review</div>
                <p className="mt-1 text-sm text-mist">Estimates show amounts to review. Actual savings depend on what you can change.</p>
              </div>
              <Pill tone="teal">{tips.length} suggestions</Pill>
            </Card>

            <div className="space-y-3">
              {tips.map((t, i) => (
                <Card key={i} className="flex gap-4">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-teal/15 text-lg font-semibold text-teal">
                    {ICON[t.type] ?? "•"}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <Heading level={3} variant="subsection" className="text-foam">{t.title}</Heading>
                      {t.potential_monthly_saving > 0 && (
                        <Pill tone="kelp">review {money(t.potential_monthly_saving)}/mo</Pill>
                      )}
                    </div>
                    <p className="mt-1 text-sm text-mist">{t.detail}</p>
                    {t.items && t.items.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {t.items.map((it) => (
                          <button key={it.merchant} onClick={() => openFinancePage(t.type === "fees" ? "transactions" : "recurring", undefined, it.merchant)}
                            className="min-h-11 rounded-md border border-line/60 bg-deep/50 px-3 py-2 text-left text-xs text-teal hover:border-teal/40">
                            {it.merchant} · <span className="text-coral">{money(it.monthly)}</span>
                          </button>
                        ))}
                      </div>
                    )}
                    <button className="mt-3 min-h-11 text-xs font-medium text-teal underline underline-offset-4" onClick={() => openFinancePage(t.type === "fees" ? "transactions" : "recurring")}>{t.type === "fees" ? "Inspect recorded transactions" : "Inspect regular payment evidence"} →</button>
                  </div>
                </Card>
              ))}
            </div>
          </div>
        );
      }}
    </Async>
  );
}
