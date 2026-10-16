import { isKnown } from "./connectedPortfolio";
import { institutionName } from "./institutions";
import type { AnalysedAccount, AnalysedHolding, PriceReturnPeriod } from "./investmentAnalysisApi";

export function signedPercent(value: number | null | undefined, decimals = 2): string {
  return isKnown(value) ? `${value > 0 ? "+" : ""}${value.toFixed(decimals)}%` : "—";
}

export function gainTone(value: number | null | undefined) {
  return !isKnown(value) || value === 0 ? "text-mist" : value > 0 ? "text-kelp" : "text-coral";
}

export function brokerageName(name: string): string {
  return institutionName(name);
}

export function groupBrokerageAccounts(accounts: AnalysedAccount[]) {
  const groups = new Map<string, AnalysedAccount[]>();
  for (const account of accounts) {
    const name = brokerageName(account.institution);
    groups.set(name, [...(groups.get(name) ?? []), account]);
  }
  return [...groups].map(([name, rows]) => ({ name, accounts: rows }));
}

// A zero is a reported balance; an absent balance belongs after known accounts.
export function rankInvestmentAccounts(accounts: AnalysedAccount[]) {
  const rank = (account: AnalysedAccount) => !isKnown(account.value) ? 2 : account.value === 0 ? 1 : 0;
  return [...accounts].sort((a, b) => rank(a) - rank(b) || Math.abs(b.value ?? 0) - Math.abs(a.value ?? 0) || a.name.localeCompare(b.name));
}

export function priceMovers(holdings: AnalysedHolding[], period: PriceReturnPeriod, direction: "up" | "down") {
  const unique = new Map<string, AnalysedHolding>();
  for (const holding of holdings) {
    const value = holding.returns[period];
    if (holding.is_cash || !isKnown(value) || (direction === "up" ? value <= 0 : value >= 0)) continue;
    const key = `${holding.instrument_id || holding.market_symbol || holding.id}|${holding.market_currency || holding.currency}`;
    if (!unique.has(key)) unique.set(key, holding);
  }
  return [...unique.values()].sort((a, b) => direction === "up" ? b.returns[period]! - a.returns[period]! : a.returns[period]! - b.returns[period]!).slice(0, 3);
}

export function defaultSpotlight(holdings: AnalysedHolding[]) {
  return [...holdings].filter((holding) => !holding.is_cash).sort((a, b) => {
    const aHasPrices = Object.values(a.returns).some(isKnown), bHasPrices = Object.values(b.returns).some(isKnown);
    return Number(bHasPrices) - Number(aHasPrices) || (b.value ?? -Infinity) - (a.value ?? -Infinity);
  })[0] ?? null;
}

export type PriceHistoryForComparison = { id: string; name: string; currency: string | null; points: { date: string; close: number | null }[]; asOf?: string | null; status?: string; sourceUrl?: string | null };
export function rebaseCommonHistory(series: PriceHistoryForComparison[]) {
  if (!series.length) return { dates: [] as string[], series: [] as { id: string; name: string; currency: string | null; values: number[]; asOf?: string | null; status?: string; sourceUrl?: string | null }[] };
  const maps = series.map((item) => new Map(item.points.filter((point) => isKnown(point.close) && point.close > 0).map((point) => [point.date.slice(0, 10), point.close!])));
  const dates = [...maps[0].keys()].filter((date) => maps.every((values) => values.has(date))).sort();
  if (dates.length < 2) return { dates, series: [] };
  return { dates, series: series.map((item, index) => ({ id: item.id, name: item.name, currency: item.currency, asOf: item.asOf, status: item.status, sourceUrl: item.sourceUrl, values: dates.map((date) => (maps[index].get(date)! / maps[index].get(dates[0])! - 1) * 100) })) };
}
