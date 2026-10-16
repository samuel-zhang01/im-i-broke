import type { BankingData, BankAccount, LocalBankAccount } from "./bankingApi";
import { institutionName } from "./institutions";

export type BankRow = {
  key: string; name: string; institution: string; currency: string;
  provider?: BankAccount; local?: LocalBankAccount;
};
export function bankName(raw: string): string {
  return institutionName(raw);
}
export function bankRows(data: BankingData): BankRow[] {
  const mapped = new Set(data.accounts.filter(a => !a.excluded).map(a => a.account_id).filter(Boolean));
  const rows: BankRow[] = data.accounts.filter(a => !a.excluded).map(provider => {
    const local = data.local_accounts.find(a => a.id === provider.account_id);
    return { key: `bank:${provider.id}`, name: local?.display_name || provider.name, institution: bankName(provider.institution_name), currency: provider.balance?.currency || provider.currency || local?.currency || "Unknown", provider, local };
  });
  for (const local of data.local_accounts) {
    if (mapped.has(local.id)) continue;
    // Investments already have dedicated account and holdings views.
    if (["investment", "investments", "brokerage"].includes(local.account_type)) continue;
    rows.push({ key: `local:${local.id}`, name: local.display_name, institution: bankName(local.bank), currency: local.currency || "GBP", local });
  }
  return rows.sort((a, b) => a.institution.localeCompare(b.institution) || a.name.localeCompare(b.name) || a.currency.localeCompare(b.currency));
}
// Match the application's backend minor-unit policy; browser ICU currency
// defaults differ for some supported codes (for example IQD and AFN).
const threeDecimalCurrencies = new Set(["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"]);
const zeroDecimalCurrencies = new Set(["BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"]);
export function nativeCurrencyPrecision(currency: string): number {
  return threeDecimalCurrencies.has(currency) ? 3 : zeroDecimalCurrencies.has(currency) ? 0 : 2;
}
export function nativeMoney(amount: number | null | undefined, currency = "GBP"): string {
  if (amount == null || !Number.isFinite(amount)) return "—";
  const safe = /^[A-Z]{3}$/.test(currency) ? currency : "GBP";
  return new Intl.NumberFormat("en-GB", { style: "currency", currency: safe, currencyDisplay: "symbol", minimumFractionDigits: nativeCurrencyPrecision(safe), maximumFractionDigits: nativeCurrencyPrecision(safe) }).format(amount);
}
export function bankTimestamp(value: string | null | undefined): string {
  if (!value) return "Not fetched yet";
  const instant = new Date(value.includes("T") || value.includes(" ") ? `${value.replace(" ", "T")}${/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? "" : "Z"}` : `${value}T12:00:00`);
  if (!Number.isFinite(instant.getTime())) return "Date unavailable";
  return instant.toLocaleString("en-GB", { day: "numeric", month: "short", ...(value.length > 10 ? { hour: "2-digit", minute: "2-digit" } : { year: "numeric" }) });
}
export function isOldBankData(value: string | null | undefined, now = Date.now()): boolean {
  if (!value) return true;
  const normalized = value.replace(" ", "T");
  const time = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(normalized) ? normalized : `${normalized}Z`);
  return !Number.isFinite(time) || now - time > 36 * 60 * 60 * 1000;
}
export function accountBalance(row: BankRow): number | null {
  return row.provider ? row.provider.balance?.amount ?? null : row.local?.balance ?? null;
}
export function bankTotals(rows: BankRow[]): Record<string, number> {
  const result: Record<string, number> = {};
  for (const row of rows) {
    // Group totals are connected bank snapshots only. Statement dates differ.
    if (!row.provider?.balance || row.provider.present === false || row.provider.balance.amount === null || !Number.isFinite(row.provider.balance.amount)) continue;
    const currency = row.provider.balance.currency;
    result[currency] = (result[currency] || 0) + row.provider.balance.amount;
  }
  return result;
}
export function bankBalanceTotals(rows: BankRow[]): Record<string, { assets: number; liabilities: number; net: number }> {
  const result: Record<string, { assets: number; liabilities: number; net: number }> = {};
  for (const row of rows) {
    if (!row.provider?.balance || row.provider.present === false || row.provider.balance.amount === null || !Number.isFinite(row.provider.balance.amount)) continue;
    const { currency, amount } = row.provider.balance;
    const total = result[currency] ?? { assets: 0, liabilities: 0, net: 0 };
    total.assets += Math.max(0, amount);
    total.liabilities += Math.max(0, -amount);
    total.net += amount;
    result[currency] = total;
  }
  return result;
}
const eventName = "ledger-open-banking";
export function openBankAccount(accountId: string) {
  window.dispatchEvent(new CustomEvent(eventName, { detail: accountId }));
}
export function watchBankingNavigation(open: (id: string) => void) {
  const listener = (event: Event) => {
    const id = (event as CustomEvent<unknown>).detail;
    if (typeof id === "string" && id.length > 0 && id.length < 300) open(id);
  };
  window.addEventListener(eventName, listener);
  return () => window.removeEventListener(eventName, listener);
}
