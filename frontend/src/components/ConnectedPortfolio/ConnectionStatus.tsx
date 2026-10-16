import { Heading } from "../Heading";
import InstitutionLogo from "../InstitutionLogo";
import type { BrokerageConnection, ConnectedAccount, PortfolioSection } from "../../snaptradeApi";
import { portfolioDate } from "../../connectedPortfolio";
import { Card, Empty, Pill, SectionTitle } from "../ui";
import InvestmentCoverage from "./InvestmentCoverage";

export default function ConnectionStatus({ accounts, connections, selectedAccount, currency, revision }: {
  accounts: ConnectedAccount[]; connections: PortfolioSection<BrokerageConnection>; selectedAccount: string; currency: string; revision: string;
}) {
  const linkedIds = new Set(accounts.map((a) => a.connection_id));
  const rows = selectedAccount === "all" ? connections.data : connections.data.filter((c) => linkedIds.has(c.id));
  return <div className="space-y-4">
    <Card>
      <SectionTitle hint="Read-only in Am I Broke?">Brokerage connections</SectionTitle>
      <p className="text-sm leading-relaxed text-mist">SnapTrade is the source of truth for current balances and holdings. Am I Broke? retrieves current data from your connected accounts and saves each successful observation for account history.</p>
      <p className="mt-3 text-sm leading-relaxed text-mist">Broker transactions follow SnapTrade’s daily sync, separately from balance and holdings updates. The refresh schedule and last successful retrieval are shown above.</p>
      {(connections.status === "error" || connections.status === "unsupported") && <p role="status" className="mt-4 rounded-lg border border-amber/30 bg-amber/5 p-3 text-sm text-amber">{connections.error || "Connection status is not available right now. Account data may still be available."}</p>}
      {connections.truncated && <p className="mt-3 text-sm text-amber">Only part of the connection list is available.</p>}
      {!rows.length ? <Empty>No matching connection details were reported. Account data and connection status may have different availability.</Empty> : <div className="mt-5 grid gap-3 md:grid-cols-2">{rows.map((connection) => <section key={connection.id} className="min-w-0 rounded-xl border border-line p-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex min-w-0 items-center gap-3"><InstitutionLogo name={connection.institution} /><p className="eyebrow break-words">{connection.institution}</p></div><Pill tone={connection.disabled === true ? "coral" : connection.disabled === false ? "kelp" : "mute"}>{connection.disabled === true ? "Connection disabled" : connection.disabled === false ? "Connection enabled" : "Status unknown"}</Pill></div>
        <Heading level={3} variant="subsection" className="mt-2 break-words">{connection.name || connection.institution}</Heading>
        <dl className="mt-4 space-y-3 text-xs"><div className="flex flex-wrap justify-between gap-2"><dt className="text-mist">Connected</dt><dd>{portfolioDate(connection.created_at)}</dd></div><div className="flex flex-wrap justify-between gap-2"><dt className="text-mist">Provider permission</dt><dd>{connection.is_read_only === true ? "Read-only" : connection.is_read_only === false ? "Broader than read-only" : "Not reported"}</dd></div><div className="flex flex-wrap justify-between gap-2"><dt className="text-mist">Accounts in this view</dt><dd>{accounts.filter((a) => a.connection_id === connection.id).length}</dd></div></dl>
        {connection.disabled === true && <p className="mt-4 text-xs text-amber">Review this connection in SnapTrade Personal. It may need to be reconnected there.</p>}
        {connection.is_read_only === false && <p className="mt-4 text-xs text-mist">This connection permits more in SnapTrade. Am I Broke? exposes only read operations.</p>}
      </section>)}</div>}
    </Card>
    <InvestmentCoverage accountIds={accounts.map(account => account.id)} currency={currency} revision={revision} />
  </div>;
}
