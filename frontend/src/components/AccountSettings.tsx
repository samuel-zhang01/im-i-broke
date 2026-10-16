import { useEffect, useState, type FormEvent } from "react";
import { fetchWorkspaceCalendar, fetchWorkspacePreferences, productRequest, type WorkspaceAccount } from "../productApi";
import { CurrencyInput, settingsInput } from "./WorkspaceSettings";
import { ErrorBox, Spinner, useAsync } from "./ui";
import HistoryCoverageSettings from "./HistoryCoverageSettings";
import { nativeMoney } from "../banking";

const accountTypes = ["current", "savings", "credit", "cash", "investment"];
const accountTypeLabel = (type: string | null | undefined) => type === "credit" ? "Credit card" : type ? type[0].toUpperCase() + type.slice(1) : "Unknown type";
type AccountValues = Pick<WorkspaceAccount, "account_type" | "institution" | "currency" | "include_in_reports" | "include_in_planning" | "archived" | "ownership_share" | "opened_on" | "closed_on"> & { display_name: string; aliases: string[] };

function AccountForm({ account, initialCurrency, onDone, onBusyChange }: { account?: WorkspaceAccount; initialCurrency: string; onDone: (saved?: WorkspaceAccount) => void; onBusyChange: (busy: boolean) => void }) {
  const [values, setValues] = useState<AccountValues>({ display_name: account?.display_name || "", account_type: account?.account_type || "cash",
    institution: account?.institution || "Manual", currency: account?.currency || initialCurrency, aliases: account?.aliases || [],
    include_in_reports: account?.include_in_reports ?? true, include_in_planning: account?.include_in_planning ?? true,
    archived: account?.archived ?? false, ownership_share: account?.ownership_share ?? 1, opened_on: account?.opened_on || null, closed_on: account?.closed_on || null });
  const [aliasText, setAliasText] = useState(values.aliases.join("\n"));
  const [preview, setPreview] = useState<{ before: WorkspaceAccount; after: WorkspaceAccount; blockers: string[] } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const patch = { ...values, aliases: aliasText.split("\n").map(alias => alias.trim()).filter(Boolean) };
  function change<K extends keyof AccountValues>(key: K, value: AccountValues[K]) { setValues(previous => ({ ...previous, [key]: value })); setPreview(null); }
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); onBusyChange(true); setError("");
    try {
      if (account && !preview) setPreview(await productRequest(`workspace/accounts/${encodeURIComponent(account.id)}/preview`, "POST", patch));
      else {
        const saved = await productRequest<WorkspaceAccount>(`workspace/accounts${account ? `/${encodeURIComponent(account.id)}` : ""}`, account ? "PATCH" : "POST", account ? patch : {
          display_name: values.display_name, account_type: values.account_type, institution: values.institution, currency: values.currency, opened_on: values.opened_on,
        }); onDone(saved);
      }
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); onBusyChange(false); }
  }
  const changes = preview ? Object.keys(patch).filter(key => JSON.stringify(preview.before[key as keyof WorkspaceAccount]) !== JSON.stringify(preview.after[key as keyof WorkspaceAccount])) : [];
  return <form onSubmit={submit} className="space-y-4 rounded-xl border border-line p-4">
    <fieldset disabled={busy} className="min-w-0 space-y-4">
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="text-sm">Account name<input className={settingsInput} required maxLength={200} placeholder="e.g. Everyday account" value={values.display_name} onChange={event => change("display_name", event.target.value)} /></label>
      <label className="text-sm">Institution<input className={settingsInput} required maxLength={200} value={values.institution} onChange={event => change("institution", event.target.value)} /></label>
      <label className="text-sm">Account type<select className={settingsInput} value={values.account_type} onChange={event => change("account_type", event.target.value)}>{[...new Set([...accountTypes, values.account_type])].map(type => <option key={type} value={type}>{accountTypeLabel(type)}</option>)}</select></label>
      <label className="text-sm">Account currency<CurrencyInput value={values.currency} onChange={value => change("currency", value)} /></label>
      <label className="text-sm">Opened on<input className={settingsInput} type="date" value={values.opened_on || ""} onChange={event => change("opened_on", event.target.value || null)} /></label>
      {account && <label className="text-sm">Closed on<input className={settingsInput} type="date" value={values.closed_on || ""} onChange={event => change("closed_on", event.target.value || null)} /></label>}
    </div>
    {!account && <p className="text-xs leading-relaxed text-mist">Use your own bank name and the account’s original currency. This creates a manual account; connecting a live bank feed is a separate step.</p>}
    {values.account_type === "credit" && <p className="text-xs leading-relaxed text-mist">Record money you owe as a negative balance. A credit limit is not a balance. For connected cards that report available credit, configure the confirmed limit under Credit card balances.</p>}
    {account && <>
      <label className="block text-sm">Institution aliases, one per line<textarea className={settingsInput} rows={2} value={aliasText} onChange={event => { setAliasText(event.target.value); setPreview(null); }} /></label>
      <label className="block max-w-xs text-sm">Your ownership share (%)<input className={settingsInput} type="number" min={0.01} max={100} step={0.01} required value={Number((values.ownership_share * 100).toFixed(2))} onChange={event => change("ownership_share", Number(event.target.value) / 100)} /></label>
      <div className="space-y-2">{(["include_in_reports", "include_in_planning", "archived"] as const).map((key, index) => <label key={key} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={values[key]} onChange={event => change(key, event.target.checked)} />{["Include in workspace reports", "Include in planning", "Archive account"][index]}</label>)}</div>
      <p className="text-xs text-mist">These settings preserve your transaction history. Existing financial history prevents currency changes or switching between cash, credit and investment types.</p>
    </>}
    </fieldset>
    {preview && <div className="space-y-2 rounded-lg bg-mist/5 p-3" role="status"><p className="text-sm font-medium">Review account changes</p>
      {changes.length ? <ul className="list-inside list-disc text-sm">{changes.map(key => <li key={key}>{key.replace(/_/g, " ")}: {String(preview.before[key as keyof WorkspaceAccount] ?? "Not set")} → {String(preview.after[key as keyof WorkspaceAccount] ?? "Not set")}</li>)}</ul> : <p className="text-sm">No changes.</p>}
      {preview.blockers.map(blocker => <p key={blocker} className="text-sm text-coral">{blocker}</p>)}
    </div>}
    {error && <ErrorBox error={error} />}<div className="flex flex-wrap gap-2"><button className="btn" disabled={busy || Boolean(preview?.blockers.length)}>{busy ? "Please wait…" : account ? preview ? "Save account changes" : "Review account changes" : "Create manual account"}</button><button type="button" className="btn" disabled={busy} onClick={() => onDone()}>Cancel</button></div>
  </form>;
}

function NewAccountForm({ onDone, onBusyChange }: { onDone: (saved?: WorkspaceAccount) => void; onBusyChange: (busy: boolean) => void }) {
  const preferences = useAsync(signal => fetchWorkspacePreferences(signal));
  const [initialCurrency, setInitialCurrency] = useState<string | null>(null);
  const currency = preferences.data?.reporting_currency;
  const validCurrency = typeof currency === "string" && /^[A-Z]{3}$/.test(currency) ? currency : null;
  useEffect(() => {
    if (initialCurrency === null && validCurrency) setInitialCurrency(validCurrency);
  }, [initialCurrency, validCurrency]);
  // Capture the default once for this form. Later refreshes must not replace a
  // currency the person has chosen or discard their unsaved account details.
  if (initialCurrency) return <AccountForm onBusyChange={onBusyChange} initialCurrency={initialCurrency} onDone={onDone} />;
  const error = preferences.error || (preferences.data && !validCurrency ? "Your workspace currency could not be loaded. Please try again." : null);
  return <div className="space-y-3 rounded-xl border border-line p-4">
    {error ? <ErrorBox error={error} retry={preferences.refetch} /> : <Spinner label="Loading your account defaults…" />}
    <button type="button" className="btn" onClick={() => onDone()}>Cancel</button>
  </div>;
}

function BalanceForm({ account, onDone, onBusyChange }: { account: WorkspaceAccount; onDone: (saved?: boolean) => void; onBusyChange: (busy: boolean) => void }) {
  const [amount, setAmount] = useState(""); const [date, setDate] = useState("");
  const calendar = useAsync(fetchWorkspaceCalendar);
  const [initialDate, setInitialDate] = useState<string | null>(null);
  const today = calendar.data?.today;
  const validDate = typeof today === "string" && /^\d{4}-\d{2}-\d{2}$/.test(today) && Number.isFinite(Date.parse(`${today}T00:00:00Z`)) ? today : null;
  useEffect(() => {
    if (initialDate === null && validDate) { setDate(validDate); setInitialDate(validDate); }
  }, [initialDate, validDate]);
  const calendarError = calendar.error || (calendar.data && !validDate ? "The workspace date could not be loaded. Please try again." : null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); onBusyChange(true); setError("");
    try { await productRequest(`workspace/accounts/${encodeURIComponent(account.id)}/balances`, "POST", { amount: Number(amount), currency: account.currency, as_of: date }); onDone(true); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); onBusyChange(false); }
  }
  return <form className="space-y-3 rounded-xl border border-line p-4" onSubmit={submit}>
    <p className="text-sm font-medium">Record a balance for {account.display_name}</p><p className="text-xs text-mist">{account.account_type === "credit" ? "Enter the amount owed as a negative balance, for example −250 when you owe 250. Do not enter your credit limit or available credit." : "Enter debt as a negative balance."} This saves a balance for the chosen date and leaves transaction history unchanged.</p>
    {calendarError && <ErrorBox error={calendarError} retry={calendar.refetch} />}
    {!initialDate && !calendarError && <Spinner label="Loading workspace date…" />}
    <fieldset disabled={busy || !initialDate} className="grid min-w-0 gap-3 sm:grid-cols-2"><label className="text-sm">Balance ({account.currency})<input className={settingsInput} required type="number" step="any" value={amount} onChange={event => setAmount(event.target.value)} /></label><label className="text-sm">Balance date<input className={settingsInput} required type="date" value={date} onChange={event => setDate(event.target.value)} /></label></fieldset>
    {error && <ErrorBox error={error} />}<div className="flex gap-2"><button className="btn" disabled={busy || !initialDate}>{busy ? "Saving…" : "Save balance"}</button><button className="btn" type="button" disabled={busy} onClick={() => onDone()}>Cancel</button></div>
  </form>;
}

export default function AccountSettings({ onImport }: { onImport?: () => void }) {
  const state = useAsync(signal => productRequest<{ accounts: WorkspaceAccount[] }>("workspace/accounts", "GET", undefined, signal));
  const [editing, setEditing] = useState<WorkspaceAccount | "new" | null>(null); const [balance, setBalance] = useState<WorkspaceAccount | null>(null);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState(""); const [coverage, setCoverage] = useState<WorkspaceAccount | null>(null);
  const [created, setCreated] = useState<WorkspaceAccount | null>(null);
  const [savedBalanceName, setSavedBalanceName] = useState("");
  if (state.error && !state.data) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading account settings…" />;
  const visibleAccounts = state.data.accounts.filter(account => `${account.display_name} ${account.institution} ${accountTypeLabel(account.account_type)} ${account.currency}`.toLowerCase().includes(query.trim().toLowerCase()));
  return <div className="space-y-4">
    <p className="text-sm text-mist">Keep account names, ownership and report choices independent of imported statements and connected banks.</p>
    {state.error && <><ErrorBox error={state.error} retry={state.refetch} /><p role="status" className="text-sm text-mist">The account list could not be refreshed. Your unsaved changes are still here.</p></>}
    {!editing && !balance && <button className="btn" onClick={() => { setEditing("new"); setCoverage(null); setCreated(null); setSavedBalanceName(""); }}>Add manual account</button>}
    {editing === "new" ? <NewAccountForm onBusyChange={setBusy} onDone={saved => { setEditing(null); if (saved) { setCreated(saved); setQuery(""); } state.refetch(); }} /> : editing && <AccountForm onBusyChange={setBusy} key={editing.id} account={editing} initialCurrency={editing.currency} onDone={() => { setEditing(null); state.refetch(); }} />}
    {created && !editing && !balance && <div className="space-y-3 rounded-xl border border-teal/30 bg-teal/5 p-4">
      <p role="status" className="break-words text-sm"><span className="font-medium">{created.display_name} was added.</span> Record a balance to see its value in Net assets. A CSV adds transactions; it does not set today’s balance.</p>
      <div className="flex flex-wrap gap-2"><button type="button" className="btn-primary" disabled={busy} onClick={() => { setBalance(created); setCoverage(null); setSavedBalanceName(""); }}>Record first balance</button>{onImport && <button type="button" className="btn" disabled={busy} onClick={onImport}>Import transactions</button>}</div>
    </div>}
    {balance && <BalanceForm onBusyChange={setBusy} key={balance.id} account={balance} onDone={saved => { if (saved) { setSavedBalanceName(balance.display_name || balance.institution); setCreated(null); } setBalance(null); state.refetch(); }} />}
    {savedBalanceName && !editing && !balance && <div className="space-y-3 rounded-xl border border-teal/30 bg-teal/5 p-4"><p role="status" className="break-words text-sm">Balance saved for {savedBalanceName}. View Net assets to see your available account balances together.</p><a className="btn-primary inline-flex" href="#net-worth">View Net assets</a></div>}
    {coverage && <HistoryCoverageSettings key={coverage.id} accountId={coverage.id} accountName={coverage.display_name || coverage.institution} />}
    <label className="block text-sm">Find an account<input className={settingsInput} type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <ul className="max-h-[32rem] space-y-3 overflow-y-auto">{visibleAccounts.map(account => <li key={account.id} className="space-y-2 rounded-xl border border-line p-3">
      <p className="break-words text-sm font-medium">{account.display_name || account.institution}{account.archived && <span className="ml-2 font-normal text-mist">Archived</span>}</p>
      <p className="break-words text-xs text-mist">{account.institution} · {accountTypeLabel(account.account_type)} · {account.currency} · {account.transaction_count} transactions · {Math.round(account.ownership_share * 100)}% ownership</p>
      {account.manual_balance && <p className="break-words text-xs text-mist">Latest manual balance: <span className="font-medium tabular-nums">{nativeMoney(account.manual_balance.amount, account.manual_balance.currency)}</span> · {account.manual_balance.currency} · {account.manual_balance.as_of}</p>}
      {account.connections.length > 0 && <p className="break-words text-xs text-mist">Connected: {account.connections.map(connection => connection.name).join(", ")}</p>}
      <div className="flex flex-wrap gap-2"><button className="btn" disabled={busy} aria-label={`Edit account ${account.display_name || account.institution}`} onClick={() => { setEditing(account); setBalance(null); setCoverage(null); setCreated(null); setSavedBalanceName(""); }}>Edit</button>{account.origin === "manual" && <button className="btn" disabled={busy} aria-label={`Record balance for ${account.display_name}`} onClick={() => { setBalance(account); setEditing(null); setCoverage(null); setCreated(null); setSavedBalanceName(""); }}>Record balance</button>}<button className="btn" disabled={busy} aria-label={`History coverage for ${account.display_name || account.institution}`} onClick={() => { setCoverage(current => current?.id === account.id ? null : account); setEditing(null); setBalance(null); setCreated(null); setSavedBalanceName(""); }}>History coverage</button></div>
    </li>)}</ul>
    {!state.data.accounts.length && <p className="text-sm text-mist">Create your first manual account or connect a bank to get started.</p>}
    {state.data.accounts.length > 0 && !visibleAccounts.length && <div role="status" className="space-y-2 text-sm text-mist"><p>No accounts match your search. Try an account name, bank, type or currency.</p><button type="button" className="btn" onClick={() => setQuery("")}>Clear account search</button></div>}
  </div>;
}
