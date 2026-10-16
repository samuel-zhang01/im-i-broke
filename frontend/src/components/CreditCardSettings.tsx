import { useState } from "react";
import { fetchBanking } from "../bankingApi";
import { BankBalanceRuleSettings } from "./BankBalanceRuleSettings";
import { settingsInput } from "./WorkspaceSettings";
import { ErrorBox, Spinner, useAsync } from "./ui";

export default function CreditCardSettings({ onManualAccounts }: { onManualAccounts?: () => void }) {
  const state = useAsync(signal => fetchBanking(signal));
  const [selectedId, setSelectedId] = useState("");
  if (state.error) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading credit card settings…" />;
  const localCards = new Map(state.data.local_accounts.filter(account => account.account_type === "credit").map(account => [account.id, account]));
  const cards = state.data.accounts.filter(account => account.account_id && localCards.has(account.account_id) && !account.excluded);
  const selected = cards.find(account => account.id === selectedId);
  return <div className="space-y-4">
    <p className="text-sm leading-relaxed text-mist">Set each connected card separately. Check whether its bank feed reports the account balance or available credit; this can differ between banks and providers.</p>
    {cards.length ? <>
      <label className="block text-sm">Connected credit card<select className={settingsInput} value={selected ? selectedId : ""} onChange={event => setSelectedId(event.target.value)}>
        <option value="">Choose a card</option>
        {cards.map(account => <option key={account.id} value={account.id}>{localCards.get(account.account_id!)?.display_name || account.name} · {account.institution_name} · {account.currency || "Currency not confirmed"}</option>)}
      </select></label>
      {selected && <div className="space-y-3">
        <p className="break-words text-sm font-medium">{selected.institution_name} · {selected.name}</p>
        {selected.currency ? <BankBalanceRuleSettings key={selected.id} account={selected} currency={selected.currency} onUpdated={state.refetch} initiallyOpen /> : <p role="status" className="text-sm text-mist">Confirm this card’s currency in Banking before setting its credit limit.</p>}
      </div>}
    </> : <p className="text-sm text-mist">No connected credit cards are ready to configure. Connect a provider, then link its card to a credit account in Banking.</p>}
    <p className="text-xs leading-relaxed text-mist">For a manual card, choose Credit card in Account settings and record the amount owed as a negative balance. No bank login, card number or security code is needed.</p>
    {onManualAccounts && <button type="button" className="btn" onClick={onManualAccounts}>Manage manual card balances</button>}
    <a className="inline-flex min-h-11 items-center text-sm font-medium text-teal underline underline-offset-4" href="#banking">Open Banking to review connected accounts →</a>
  </div>;
}
