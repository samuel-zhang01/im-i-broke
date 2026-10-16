import type { ConnectedAccount } from "./snaptradeApi";

export function isKnown(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function nativeMoney(value: number | null | undefined, currency: string | null): string {
  if (!isKnown(value)) return "—";
  if (currency && /^[A-Z]{3}$/.test(currency)) {
    // The en-GB locale keeps dollars disambiguated (US$, SGD, HK$) and
    // matches the pound/euro formatting used throughout the rest of Am I Broke?.
    return new Intl.NumberFormat("en-GB", { style: "currency", currency, currencyDisplay: "symbol", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  }
  const amount = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  return `Unknown currency ${amount}`;
}

export function quantity(value: number | null): string {
  return isKnown(value) ? new Intl.NumberFormat("en-GB", { maximumFractionDigits: 8 }).format(value) : "—";
}

export function portfolioDate(value: string | null, time = false): string {
  if (!value) return "Not reported";
  const parsed = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00Z` : value);
  if (Number.isNaN(parsed.getTime())) return "Not reported";
  return parsed.toLocaleString("en-GB", time
    ? { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }
    : { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function accountLabel(account: Pick<ConnectedAccount, "name" | "institution" | "number_suffix">): string {
  const name = account.name || account.institution;
  // Broker labels often already end in the masked account number.
  const suffix = account.number_suffix;
  return `${name}${suffix && !name.replace(/[^a-zA-Z0-9]/g, "").endsWith(suffix) ? ` · ••${suffix}` : ""}`;
}

export type CurrencyTotal = { currency: string; total: number | null; known: number; missing: number; accounts: ConnectedAccount[] };
export function accountCurrencyTotals(accounts: ConnectedAccount[]): CurrencyTotal[] {
  const groups = new Map<string, CurrencyTotal>();
  for (const account of accounts) {
    if (!account.currency) continue;
    const group = groups.get(account.currency) ?? { currency: account.currency, total: null, known: 0, missing: 0, accounts: [] };
    group.accounts.push(account);
    if (isKnown(account.total_value)) {
      group.total = (group.total ?? 0) + account.total_value;
      group.known += 1;
    } else group.missing += 1;
    groups.set(account.currency, group);
  }
  return [...groups.values()].sort((a, b) => a.currency.localeCompare(b.currency));
}

export function balanceCurrencyTotals(accounts: ConnectedAccount[], field: "cash" | "buying_power") {
  const groups = new Map<string, { currency: string; total: number | null; missing: number }>();
  for (const account of accounts) {
    for (const balance of account.balances.data) {
      if (!balance.currency) continue;
      const group = groups.get(balance.currency) ?? { currency: balance.currency, total: null, missing: 0 };
      if (isKnown(balance[field])) group.total = (group.total ?? 0) + balance[field];
      else group.missing += 1;
      groups.set(balance.currency, group);
    }
  }
  return [...groups.values()].sort((a, b) => a.currency.localeCompare(b.currency));
}

export type AccountSectionName = "balances" | "positions" | "orders" | "activities";
export function sectionIssues(accounts: ConnectedAccount[], key: AccountSectionName) {
  return accounts.filter((account) => ["error", "unsupported"].includes(account[key].status) || account[key].truncated || account[key].stale);
}

export function hasOldBrokerageData(accounts: ConnectedAccount[], now: number): boolean {
  return accounts.some((account) => {
    const stamp = account.holdings_synced_at ? Date.parse(account.holdings_synced_at) : NaN;
    return Number.isFinite(stamp) && now - stamp > 36 * 60 * 60 * 1000;
  });
}

export function compareOptionalNumbers(a: number | null, b: number | null, direction: "asc" | "desc") {
  if (!isKnown(a)) return isKnown(b) ? 1 : 0;
  if (!isKnown(b)) return -1;
  return direction === "asc" ? a - b : b - a;
}
