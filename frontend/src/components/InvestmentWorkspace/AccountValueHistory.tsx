import { Heading } from "../Heading";
import { useEffect, useState } from "react";
import { fetchWealth, type WealthData } from "../../wealthApi";
import type { InvestmentRange } from "../../investmentAnalysisApi";
import { Card } from "../ui";
import { AccountHistorySkeleton } from "../Skeleton";
import CombinedValueChart from "./CombinedValueChart";
import InvestmentHistoryCoverage from "./InvestmentHistoryCoverage";

export default function AccountValueHistory({ accountIds, currency, range, revision, showRecent, onShowRecentChange }: { accountIds: string[]; currency: string; range: InvestmentRange; revision: string; showRecent: boolean; onShowRecentChange: (value: boolean) => void }) {
  const [result, setResult] = useState<{ key: string; data: WealthData } | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  const key = `${accountIds.join("|")}|${currency}|${range}`;
  const error = failure?.key === key ? failure.message : null;
  useEffect(() => {
    const controller = new AbortController();
    let busy = false;
    async function read() {
      if (busy || controller.signal.aborted || document.visibilityState !== "visible") return;
      busy = true;
      setFailure(null);
      try {
        const data = await fetchWealth({ accountIds: accountIds.map(id => `connected:${id}`), currency, range }, controller.signal);
        if (!controller.signal.aborted) { setResult({ key, data }); setFailure(null); }
      } catch (reason) {
        if (!controller.signal.aborted) setFailure({ key, message: reason instanceof Error ? reason.message : "Account history is unavailable." });
      } finally { busy = false; }
    }
    void read();
    const timer = window.setInterval(() => { void read(); }, 60_000);
    const visible = () => { if (document.visibilityState === "visible") void read(); };
    document.addEventListener("visibilitychange", visible);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
    // Keep history visible during a same-scope refresh. Changed filters still
    // load independently so a previous account's values are never shown.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, revision, retry]);
  const data = result?.key === key ? result.data : null;
  return <Card className="!p-0 overflow-hidden">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 pt-4 sm:px-5">
      <div><Heading level={2} variant="section">Asset history <span className="ml-1 text-xs font-normal text-mist">{currency}</span></Heading><p className="mt-1 text-xs text-mist">Selected investments and brokerage cash. Net assets also includes bank balances and subtracts card debt.</p></div>
      <label className="flex min-h-11 cursor-pointer items-center gap-2 text-xs"><input type="checkbox" checked={showRecent} onChange={event => onShowRecentChange(event.target.checked)} className="h-4 w-4 accent-teal" />Show recent prices</label>
    </div>
    {error && <p role="status" className="px-5 py-3 text-xs text-amber">{error}{data ? " Saved history remains visible." : ""} <button className="min-h-11 underline" onClick={() => setRetry(value => value + 1)}>Retry history</button></p>}
    {data ? <CombinedValueChart key={`${accountIds.join("|")}|${currency}|${range}`} accountIds={accountIds} currency={currency} revision={revision} range={range} historyData={data} showRecent={showRecent} notes={<InvestmentHistoryCoverage data={data} />} /> : !error && <AccountHistorySkeleton />}
  </Card>;
}
