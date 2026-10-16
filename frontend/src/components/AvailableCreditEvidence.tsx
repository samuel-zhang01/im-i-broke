import { bankTimestamp, nativeMoney } from "../banking";
import type { BalanceValuation } from "../wealthApi";

export function AvailableCreditEvidence({ valuation, currency }: { valuation: BalanceValuation; currency: string | null }) {
  if (valuation.basis !== "available_credit") return null;
  return <div className="space-y-2 text-xs leading-relaxed" aria-label="Available credit calculation">
    <p className="font-medium tabular-nums">{nativeMoney(valuation.credit_limit ?? null, currency ?? undefined)} credit limit − {nativeMoney(valuation.available_credit ?? null, currency ?? undefined)} available credit = {nativeMoney(valuation.effective_value === null ? null : -valuation.effective_value, currency ?? undefined)} credit used.</p>
    <p>Available credit comes from the bank feed. Credit limit confirmed by you{valuation.credit_limit_confirmed_at ? ` on ${bankTimestamp(valuation.credit_limit_confirmed_at)}` : ""}.</p>
    <p>Available credit may already reflect pending charges; they are not added again.</p>
  </div>;
}
