import { DataTable, TableMeta } from "../DataTable";
import { useState } from "react";
import type { ConnectedAccount } from "../../snaptradeApi";
import { accountLabel, nativeMoney, portfolioDate, quantity } from "../../connectedPortfolio";
import { Card, Empty, SectionTitle } from "../ui";
import { SectionWarnings, StatusPill, TableRegion, usePage } from "./common";

function dateOrder(value: string | null) { return value ? Date.parse(value) || 0 : 0; }

export function PortfolioActivity({ accounts }: { accounts: ConnectedAccount[] }) {
  const [search, setSearch] = useState("");
  const scope = JSON.stringify(accounts.map((account) => account.id));
  const [filter, setFilter] = useState({ scope, value: "all" });
  if (filter.scope !== scope) setFilter({ scope, value: "all" });
  const all = accounts.flatMap((account) => account.activities.data.map((activity) => ({ ...activity, account })));
  const types = [...new Set(all.map((row) => row.type))].sort();
  const type = filter.scope === scope && types.includes(filter.value) ? filter.value : "all";
  const term = search.trim().toLowerCase();
  const rows = all.filter((activity) => (type === "all" || activity.type === type) && `${activity.symbol || ""} ${activity.description} ${activity.type} ${activity.account.name}`.toLowerCase().includes(term)).sort((a, b) => dateOrder(b.date) - dateOrder(a.date));
  const page = usePage(rows, `${search}|${type}|${accounts.map((a) => a.id).join()}`);
  return <Card className="space-y-4">
    <SectionTitle hint="Most recent first">Account activity</SectionTitle>
    <p className="text-sm text-mist">Trades, dividends, deposits, withdrawals and other activity from the last 90 days, up to 200 records per account. Available history varies by connection.</p>
    <SectionWarnings accounts={accounts} section="activities" label="Activities" />
    <div className="flex flex-wrap gap-3"><label className="field-label flex-1 basis-52">Search activity<input type="search" className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Symbol or description" /></label><label className="field-label">Activity type<select className="input" value={type} onChange={(e) => setFilter({ scope, value: e.target.value })}><option value="all">All types</option>{types.map((t) => <option key={t}>{t}</option>)}</select></label></div>
    {!rows.length ? <Empty>{search || type !== "all" ? "No activity matches these filters." : "No activity was returned for this selection. A connected account may have a shorter history than its statements."}</Empty> : <TableRegion label="Connected account activity"><DataTable className={`min-w-[800px]`}><thead><tr><th scope="col">Date</th><th scope="col">Activity / account</th><th scope="col">Symbol</th><th scope="col" className="text-right">Quantity</th><th scope="col" className="text-right">Amount</th></tr></thead><tbody>{page.rows.map((activity) => <tr key={`${activity.account.id}-${activity.id}`}>
      <td className="whitespace-nowrap text-xs text-mist">{portfolioDate(activity.date)}</td><td className="max-w-80"><div className="flex flex-wrap items-baseline gap-x-2"><span className="font-medium">{activity.type.replace(/_/g, " ")}</span><span className="text-xs text-mist">{accountLabel(activity.account)}</span></div><TableMeta>{activity.description || "No description reported"}</TableMeta></td><td>{activity.symbol || "—"}</td><td className="text-right tabular-nums">{quantity(activity.quantity)}</td><td className="whitespace-nowrap text-right tabular-nums">{nativeMoney(activity.amount, activity.currency)}</td>
    </tr>)}</tbody></DataTable></TableRegion>}
    {page.controls}
    <p className="text-xs text-mist">Amounts use each activity’s reported currency and sign. This activity is not imported into Am I Broke?’s statement records.</p>
  </Card>;
}

export function PortfolioOrders({ accounts }: { accounts: ConnectedAccount[] }) {
  const [search, setSearch] = useState("");
  const scope = JSON.stringify(accounts.map((account) => account.id));
  const [filter, setFilter] = useState({ scope, value: "all" });
  if (filter.scope !== scope) setFilter({ scope, value: "all" });
  const all = accounts.flatMap((account) => account.orders.data.map((order) => ({ ...order, account })));
  const statuses = [...new Set(all.map((row) => row.status).filter((s): s is string => !!s))].sort();
  const status = filter.scope === scope && statuses.includes(filter.value) ? filter.value : "all";
  const term = search.trim().toLowerCase();
  const rows = all.filter((order) => (status === "all" || order.status === status) && `${order.symbol} ${order.action || ""} ${order.account.name}`.toLowerCase().includes(term)).sort((a, b) => dateOrder(b.placed_at) - dateOrder(a.placed_at));
  const page = usePage(rows, `${search}|${status}|${accounts.map((a) => a.id).join()}`);
  return <Card className="space-y-4">
    <SectionTitle hint="View only">Recent orders</SectionTitle>
    <p className="text-sm text-mist">See the state of orders placed through your brokerage. Filled orders can also appear in Activity; these are separate views of the same events.</p>
    <SectionWarnings accounts={accounts} section="orders" label="Orders" />
    <div className="flex flex-wrap gap-3"><label className="field-label flex-1 basis-52">Search orders<input type="search" className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Symbol, action or account" /></label><label className="field-label">Order status<select className="input" value={status} onChange={(e) => setFilter({ scope, value: e.target.value })}><option value="all">All statuses</option>{statuses.map((s) => <option key={s}>{s}</option>)}</select></label></div>
    {!rows.length ? <Empty>{search || status !== "all" ? "No orders match these filters." : "No recent orders were returned for this selection."}</Empty> : <TableRegion label="Recent brokerage orders"><DataTable className={`min-w-[1020px]`}><thead><tr><th scope="col">Placed</th><th scope="col">Order / account</th><th scope="col">Status</th><th scope="col" className="text-right">Filled / quantity</th><th scope="col" className="text-right">Limit price</th><th scope="col" className="text-right">Execution price</th></tr></thead><tbody>{page.rows.map((order) => <tr key={`${order.account.id}-${order.id}`}>
      <td className="whitespace-nowrap text-xs text-mist">{portfolioDate(order.placed_at, true)}</td><td className="max-w-60"><div className="flex flex-wrap items-baseline gap-x-2"><span className="font-semibold">{order.symbol || "Unreported symbol"}</span><span className="text-xs text-mist">{order.action || "Action unknown"} · {order.type || "Type unknown"}</span></div><TableMeta>{accountLabel(order.account)}</TableMeta></td><td><StatusPill value={order.status} /></td><td className="text-right tabular-nums">{quantity(order.filled_quantity)} / {quantity(order.quantity)}</td><td className="whitespace-nowrap text-right tabular-nums">{nativeMoney(order.limit_price, order.currency)}</td><td className="whitespace-nowrap text-right tabular-nums">{nativeMoney(order.execution_price, order.currency)}</td>
    </tr>)}</tbody></DataTable></TableRegion>}
    {page.controls}
    <p className="text-xs text-mist">Recent orders often cover only the last 24 hours, depending on the brokerage. Use your brokerage app to place, amend or cancel an order.</p>
  </Card>;
}
