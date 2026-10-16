import { Heading } from "../Heading";
import { useEffect, useState } from "react";
import { fetchInvestmentExposure, type InvestmentExposureData } from "../../investmentExposureApi";
import { isKnown, nativeMoney, portfolioDate } from "../../connectedPortfolio";
import { signedPercent } from "../../investmentAnalysis";
import { Card, Empty, Spinner } from "../ui";

const percent = (value: number | null) => isKnown(value) ? `${value.toFixed(1)}%` : "—";

export default function InvestmentExposure({ accountIds, currency, revision }: { accountIds: string[]; currency: string; revision: string }) {
  const [result, setResult] = useState<{ key: string; data: InvestmentExposureData } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(10);
  const key = JSON.stringify([accountIds, currency, revision]);
  useEffect(() => {
    const controller = new AbortController();
    setError(null); setLoading(true); setVisible(10);
    void fetchInvestmentExposure(accountIds, currency, controller.signal).then((data) => {
      if (!controller.signal.aborted) setResult({ key, data });
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Security exposure is unavailable.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // The key includes the selected accounts, currency and refresh revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, retry]);
  const data = result?.key === key ? result.data : null;
  return <Card><section aria-label="Combined security exposure" aria-busy={loading}>
    <div className="flex flex-wrap items-start justify-between gap-2"><div><Heading level={2} variant="section">Combined security exposure</Heading><p className="mt-1 text-xs leading-relaxed text-mist">Matching instruments across your selected accounts, with cash excluded.</p></div>{data?.paper && <span className="text-xs text-amber">Paper accounts</span>}</div>
    {error && <p role="status" className="mt-3 text-xs text-amber">{error} <button className="min-h-11 px-1 underline" onClick={() => setRetry((value) => value + 1)}>Retry exposure</button></p>}
    {!data ? loading ? <Spinner label="Loading security exposure…" /> : <Empty>Exposure calculations are unavailable.</Empty> : <>
      <p className="mt-3 text-xs text-mist">Known gross securities · <strong className="font-medium text-foam">{nativeMoney(data.summary.gross_security_value, currency)}</strong> · {data.summary.known_holdings} / {data.summary.holdings} holdings valued</p>
      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        <div className="rounded-lg bg-surface2 p-3"><dt className="text-xs text-mist">Largest instrument</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{percent(data.summary.largest_weight_pct)}</dd><p className="mt-1 text-xs text-mist">of known gross securities</p></div>
        <div className="rounded-lg bg-surface2 p-3"><dt className="text-xs text-mist">Top five instruments</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{percent(data.summary.top5_weight_pct)}</dd><p className="mt-1 text-xs text-mist">of known gross securities</p></div>
        <div className="rounded-lg bg-surface2 p-3"><dt className="text-xs text-mist">Effective instrument count</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{isKnown(data.summary.effective_instruments) ? data.summary.effective_instruments.toFixed(2) : "—"}</dd><p className="mt-1 text-xs text-mist">Same concentration as equally weighted holdings</p></div>
      </dl>
      {data.warnings.map((warning) => <p key={warning} className="mt-3 text-xs leading-relaxed text-amber">{warning}</p>)}
      <p className="mt-3 text-xs leading-relaxed text-mist">The denominator includes known absolute holding values and excludes cash and unavailable holdings. It is not total account value. Funds remain single holdings, without looking through to their constituents.</p>
      <details className="mt-4 border-t border-line"><summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">Combined holdings · {data.instruments.length} instruments</summary>
        {!data.instruments.length ? <Empty>No verified combined holdings are available.</Empty> : <>
          <div className="divide-y divide-line">{data.instruments.slice(0, visible).map((instrument) => <div key={instrument.instrument_id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2 py-3">
            <div className="min-w-0"><p className="break-words text-sm font-semibold">{instrument.symbol}</p><p className="mt-1 break-words text-xs text-mist">{instrument.name}</p><p className="mt-1 text-[10px] text-mist">{instrument.exchange} · {instrument.listing_currency} · {instrument.identity_verified ? "Listing identity checked" : "Identity unavailable"}</p></div>
            <div className="text-right text-xs tabular-nums"><p className="font-medium">{nativeMoney(instrument.gross_value, currency)}</p><p className="mt-1 text-mist">{percent(instrument.weight_pct)}</p>{instrument.missing_values > 0 && <p className="mt-1 text-amber">Known subtotal</p>}</div>
            <ul className="col-span-2 space-y-1 text-xs text-mist">{instrument.accounts.map((account) => <li key={account.id} className="flex flex-wrap justify-between gap-x-3"><span className="break-words">{account.name} · {account.institution}</span><span className="tabular-nums">{nativeMoney(account.gross_value, currency)}</span></li>)}</ul>
          </div>)}</div>
          {visible < data.instruments.length && <button className="btn my-3 min-h-11" onClick={() => setVisible((count) => count + 20)}>Show more combined holdings</button>}
        </>}
      </details>
      <div className="border-t border-line pt-3"><Heading level={3} variant="subsection">Account holdings overlap</Heading><p className="mt-1 text-xs leading-relaxed text-mist">Compares each account’s normalized gross holding weights. Shared tickers alone do not count as a match.</p>
        {!data.account_overlap.length ? <p className="py-3 text-xs text-mist">Select at least two accounts with known securities to compare overlap. Empty accounts have no percentage overlap.</p> : <ul className="mt-2 divide-y divide-line">{data.account_overlap.map((pair) => <li key={`${pair.first_account_id}/${pair.second_account_id}`} className="flex flex-wrap items-center justify-between gap-2 py-3 text-xs"><div className="min-w-0"><p className="break-words">{pair.first_account_name} / {pair.second_account_name}</p><p className="mt-1 text-xs text-mist">{pair.reason ?? `${pair.shared_instruments} shared instruments`}</p></div><strong className="tabular-nums">{percent(pair.overlap_pct)}</strong></li>)}</ul>}
      </div>
      <details className="mt-3 border-t border-line"><summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">What if the largest holdings fall 10%?</summary><p className="text-xs leading-relaxed text-mist">Static scenarios: the largest instrument, or the five largest, fall 10%. All other prices, quantities and FX stay fixed. These estimates use known gross securities and are not forecasts.</p>
        <dl className="my-3 grid gap-3 sm:grid-cols-2">{([['Largest instrument falls', data.scenarios.largest_value_change, data.scenarios.largest_gross_impact_pct], ['Top five instruments fall', data.scenarios.top5_value_change, data.scenarios.top5_gross_impact_pct]] as const).map(([label, amount, impact]) => <div key={label} className="rounded-lg bg-surface2 p-3"><dt className="text-xs text-mist">{label}</dt><dd className="mt-1 text-base font-medium tabular-nums">{nativeMoney(amount, currency)} <span className="text-xs">({signedPercent(impact)})</span></dd><p className="mt-1 text-xs text-mist">Change versus known gross securities</p></div>)}</dl>
        {!isKnown(data.scenarios.largest_gross_impact_pct) && <p className="mb-3 text-xs text-mist">A complete identity basis and positive gross exposure without short positions are needed for these scenarios.</p>}
      </details>
      <details className="border-t border-line text-xs text-mist"><summary className="min-h-11 cursor-pointer py-3">Exposure calculation details</summary><div className="space-y-2 pb-2"><p>HHI: {isKnown(data.summary.hhi_fraction) ? data.summary.hhi_fraction.toFixed(4) : "—"}. Effective count = 1 / HHI. Concentration does not measure correlation or forecast risk.</p><p>Saved observation: {portfolioDate(data.retrieved_at, true)}.</p>{data.methodology.map((note) => <p key={note}>{note}</p>)}</div></details>
    </>}
  </section></Card>;
}
