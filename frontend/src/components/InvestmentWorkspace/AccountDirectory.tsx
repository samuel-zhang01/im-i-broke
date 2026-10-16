import { Heading } from "../Heading";
import type { AnalysedAccount } from "../../investmentAnalysisApi";
import { gainTone, groupBrokerageAccounts, rankInvestmentAccounts } from "../../investmentAnalysis";
import { nativeMoney, portfolioDate } from "../../connectedPortfolio";
import { Card, Empty, Pill } from "../ui";

export default function AccountDirectory({ accounts, currency, onOpen }: { accounts: AnalysedAccount[]; currency: string; onOpen: (id: string) => void }) {
  const groups = groupBrokerageAccounts(rankInvestmentAccounts(accounts.filter((account) => account.selected)));
  if (!groups.length) return <Card><Empty>No accounts match this selection.</Empty></Card>;
  return <div className="space-y-6">{groups.map((group) => <section key={group.name}>
    <div className="mb-3 flex items-baseline justify-between gap-3"><Heading level={2} variant="section">{group.name}</Heading><span className="text-xs text-mist">{group.accounts.length} separate account{group.accounts.length === 1 ? "" : "s"}</span></div>
    <div className="grid gap-3 md:grid-cols-2">{group.accounts.map((account) => <Card key={account.id} className="flex flex-col p-5">
      <div className="flex items-start justify-between gap-3"><div className="min-w-0"><Heading level={3} variant="subsection" className="break-words">{account.name}</Heading><p className="mt-1 text-xs text-mist">{account.type || "Brokerage account"}{account.number_suffix ? ` · ••${account.number_suffix}` : ""}</p></div>{account.is_paper ? <Pill tone="blue">Paper</Pill> : account.stale ? <Pill tone="amber">Older snapshot</Pill> : <Pill tone="mute">Read-only</Pill>}</div>
      <p className="mt-3 text-[28px] font-semibold tracking-tight tabular-nums">{nativeMoney(account.value, currency)}</p>
      {account.currency !== currency && <p className="mt-1 text-xs text-mist">{nativeMoney(account.native_value, account.currency)} native account value</p>}
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-line pt-3 text-xs"><div><dt className="text-mist">Open position P/L</dt><dd className={`mt-1 font-medium tabular-nums ${gainTone(account.unrealized_pnl)}`}>{nativeMoney(account.unrealized_pnl, currency)}</dd></div><div><dt className="text-mist">Latest session · estimate</dt><dd className={`mt-1 font-medium tabular-nums ${gainTone(account.day_move_estimate)}`}>{nativeMoney(account.day_move_estimate, currency)}</dd></div><div><dt className="text-mist">Cash included in value</dt><dd className="mt-1 font-medium tabular-nums">{nativeMoney(account.cash, currency)}</dd></div><div><dt className="text-mist">Holdings</dt><dd className="mt-1 font-medium">{["error", "unsupported"].includes(account.positions_status) ? "Not available" : account.holdings_count}</dd></div></dl>
      <div className="mt-auto flex flex-wrap items-end justify-between gap-3 pt-3"><p className="text-xs text-mist">Holdings {portfolioDate(account.holdings_as_of, true)}</p><button className="btn" onClick={() => onOpen(account.id)}>View performance <span aria-hidden="true">↗</span></button></div>
    </Card>)}</div>
  </section>)}</div>;
}
