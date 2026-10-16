import { useEffect, useState } from "react";
import { setBankBalanceRule, type BankAccount } from "../bankingApi";
import { ErrorBox } from "./ui";
import { nativeCurrencyPrecision } from "../banking";

export function BankBalanceRuleSettings({ account, currency, onUpdated, initiallyOpen = false }: { account: BankAccount; currency: string; onUpdated: () => void; initiallyOpen?: boolean }) {
  const [mode, setMode] = useState<"available_credit" | "reported">(account.balance_rule?.balance_type === "available_credit" ? "available_credit" : "reported");
  const [limit, setLimit] = useState(account.balance_rule?.balance_type === "available_credit" ? String(account.balance_rule.credit_limit) : "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setMode(account.balance_rule?.balance_type === "available_credit" ? "available_credit" : "reported");
    setLimit(account.balance_rule?.balance_type === "available_credit" ? String(account.balance_rule.credit_limit) : "");
  }, [account.id, account.balance_rule?.balance_type, account.balance_rule?.credit_limit, account.balance_rule?.confirmed_at]);
  const minorUnit = String(10 ** -nativeCurrencyPrecision(currency));
  const validLimit = limit.trim() !== "" && Number.isFinite(Number(limit)) && Number(limit) > 0;
  async function save() {
    if (saving || mode === "available_credit" && !validLimit) return;
    setSaving(true); setError(null); setSaved(false);
    try {
      await setBankBalanceRule(account.id, mode === "available_credit" ? { balance_type: mode, credit_limit: Number(limit), currency } : { balance_type: mode });
      setSaved(true); onUpdated();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "The balance setting could not be saved."); }
    finally { setSaving(false); }
  }
  return <details open={initiallyOpen || undefined} className="rounded-xl border border-line bg-white px-4 text-xs sm:px-5">
    <summary className="cursor-pointer py-4 font-medium">Credit card balance settings</summary>
    <form className="space-y-3 pb-4" onSubmit={event => { event.preventDefault(); void save(); }}>
      <p className="leading-relaxed text-mist">Choose what this account’s bank feed reports. Available credit is the amount you can still spend, not the amount you owe. Confirm the limit from your bank before using it here.</p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="field-label min-w-0 flex-1 basis-64">Bank feed balance type<select className="input w-full" value={mode} disabled={saving} onChange={event => { setMode(event.target.value as typeof mode); setSaved(false); }}><option value="reported">Account balance</option><option value="available_credit">Available credit</option></select></label>
        {mode === "available_credit" && <label className="field-label min-w-0 flex-1 basis-40">Confirmed credit limit · {currency}<input className="input w-full" type="number" inputMode="decimal" min={minorUnit} step={minorUnit} required value={limit} disabled={saving} onChange={event => { setLimit(event.target.value); setSaved(false); }} /></label>}
        <button className="btn-primary" type="submit" disabled={saving || mode === "available_credit" && !validLimit}>{saving ? "Saving…" : "Save balance setting"}</button>
      </div>
      {mode === "available_credit" && account.balance_rule?.legacy_fractional_limit && <p role="status" className="text-amber">Your saved {currency} limit includes fractional units. Confirm and save your bank’s limit in whole {currency} units. Historical observations stay unchanged.</p>}
      {mode === "available_credit" && <p className="text-mist">Credit used = your confirmed credit limit − available credit returned by the bank.</p>}
      <p className="leading-relaxed text-mist">This setting applies only to this connected card and its currency. Update it when the bank changes your limit. Saved source balances and historical valuations are preserved.</p>
      {error && <ErrorBox error={error} />}{saved && <p role="status" className="text-kelp">Balance setting saved.</p>}
    </form>
  </details>;
}
