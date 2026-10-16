import { useState, type FormEvent } from "react";
import InstitutionDirectory from "./InstitutionDirectory";
import { productRequest, type Connection, type ConnectionProvider } from "../productApi";
import { settingsInput } from "./WorkspaceSettings";
import { ErrorBox, Spinner, useAsync } from "./ui";

const hostedMethods = new Set(["lunchflow_oauth", "snaptrade_commercial"]);
const hostedConnection = (connection?: Connection) => Boolean(connection?.auth_method && hostedMethods.has(connection.auth_method));

const providerHelp: Record<string, { purpose: string; instructions: string; href: string }> = {
  lunchflow: {
    purpose: "Bank accounts, balances and transactions",
    instructions: "In Lunch Flow, open Destinations → Add Destination → API. Copy its API key and enable the accounts you want under Account Access.",
    href: "https://www.lunchflow.app/docs/guides/destinations/api",
  },
  snaptrade: {
    purpose: "Brokerage accounts, holdings and activity",
    instructions: "Connect your brokerage in the SnapTrade Dashboard, then create a Personal API key. Use its clientId and consumerKey below.",
    href: "https://docs.snaptrade.com/docs/getting-started#personal-quickstart",
  },
};

type SavedConnection = { connection: Connection; refreshed: boolean; error?: string };

function ConnectionForm({ providers, connection, onDone }: { providers: ConnectionProvider[]; connection?: Connection; onDone: (saved?: SavedConnection) => void }) {
  const [providerId, setProviderId] = useState(connection?.provider || providers[0]?.id || "");
  const [label, setLabel] = useState(connection?.label || "");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [replaceCredentials, setReplaceCredentials] = useState(!connection);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const provider = providers.find(item => item.id === providerId);
  const help = providerHelp[providerId];
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const saved = await productRequest<Connection>(`connections${connection ? `/${encodeURIComponent(connection.id)}` : ""}`, connection ? "PATCH" : "POST", {
        ...(!connection ? { provider: providerId } : {}), label: label.trim() || provider?.label,
        ...(replaceCredentials ? { credentials } : {}),
      });
      setCredentials({});
      if (connection) onDone({ connection: saved, refreshed: false });
      else {
        // Refresh verifies access and retrieves data in one provider operation.
        // If it fails, keep the saved connection available for editing/retry;
        // do not leave the form open and accidentally create a duplicate.
        try {
          const result = await productRequest<{ connection: Connection }>(`connections/${encodeURIComponent(saved.id)}/sync`, "POST");
          onDone({ connection: result.connection, refreshed: !result.connection.last_error, error: result.connection.last_error || undefined });
        } catch (cause) {
          onDone({ connection: saved, refreshed: false, error: (cause as Error).message });
        }
      }
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  return <form className="space-y-4 rounded-xl border border-line bg-white p-4 sm:p-5" onSubmit={submit}>
    <fieldset disabled={busy} className="min-w-0 space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-sm">Provider<select className={settingsInput} disabled={Boolean(connection)} value={providerId} onChange={event => { setProviderId(event.target.value); setCredentials({}); }}>{providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        <label className="text-sm">Connection name{!connection && <span className="ml-1 text-xs text-mist">(optional)</span>}<input className={settingsInput} aria-label="Connection name" required={Boolean(connection)} maxLength={100} placeholder={provider?.label || "Connected accounts"} value={label} onChange={event => setLabel(event.target.value)} /></label>
      </div>
      {connection && !hostedConnection(connection) && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={replaceCredentials} onChange={event => { setReplaceCredentials(event.target.checked); setCredentials({}); }} />Replace credentials</label>}
      {replaceCredentials && <div className="space-y-4">
        {help && <div className="space-y-2 rounded-lg bg-surface2 p-3 text-sm"><p className="font-medium">{help.purpose}</p><p className="text-mist">{help.instructions}</p><a className="inline-flex min-h-11 items-center text-teal underline underline-offset-4" href={help.href} target="_blank" rel="noopener noreferrer">Open {provider?.label} setup guide ↗</a></div>}
        {provider?.credential_fields.map(field => <label className="block text-sm" key={field.name}>{field.label}<input className={settingsInput} type={field.secret ? "password" : "text"} required maxLength={4096} autoComplete="off" spellCheck={false} autoCapitalize="none" value={credentials[field.name] || ""} onChange={event => setCredentials(previous => ({ ...previous, [field.name]: event.target.value }))} /></label>)}
        <p className="text-xs leading-relaxed text-mist">Credentials are encrypted on your server and are never returned to this page. {connection ? "Your recorded history stays attached to this connection." : "Connecting also checks access and refreshes your accounts."}</p>
      </div>}
    </fieldset>
    {error && <ErrorBox error={error} />}
    <div className="flex flex-wrap gap-2"><button className="btn-primary" disabled={busy || !provider}>{busy ? connection ? "Saving…" : "Connecting and refreshing…" : connection ? "Save connection" : "Connect and refresh"}</button><button className="btn" type="button" disabled={busy} onClick={() => onDone()}>Cancel</button></div>
    {busy && !connection && <p role="status" className="text-xs text-mist">Reading your provider’s available accounts. This can take a moment.</p>}
  </form>;
}

export default function ConnectionSettings() {
  const state = useAsync(signal => productRequest<{ providers: ConnectionProvider[]; connections: Connection[] }>("connections", "GET", undefined, signal));
  const authorization = useAsync(signal => productRequest<{ providers: { provider: string; label: string; auth_method: string }[] }>("connections/authorization/providers", "GET", undefined, signal));
  const [editing, setEditing] = useState<Connection | "new" | null>(null); const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const [nextProvider, setNextProvider] = useState<string | null>(null);
  const hostedProviders = authorization.data?.providers.filter(provider => hostedMethods.has(provider.auth_method)) || [];
  const authorizationAvailable = (connection: Connection) => !hostedConnection(connection) || hostedProviders.some(provider => provider.provider === connection.provider && provider.auth_method === connection.auth_method);
  async function authorize(provider: string, label: string, connectionId?: string) {
    setBusy(connectionId || provider); setError("");
    try {
      const result = await productRequest<{ authorization_url: string }>("connections/authorization/start", "POST", { provider, label, ...(connectionId ? { connection_id: connectionId } : {}) });
      const destination = new URL(result.authorization_url);
      if (destination.protocol !== "https:") throw new Error("The provider returned an invalid authorization link.");
      window.location.assign(destination.href);
    } catch (cause) { setError((cause as Error).message); setBusy(null); }
  }
  async function action(connection: Connection, operation: "validate" | "sync" | "disconnect" | "toggle") {
    setBusy(connection.id); setError(""); setNotice(""); setNextProvider(null);
    try {
      const result = await productRequest<Connection | { connection: Connection }>(`connections/${encodeURIComponent(connection.id)}${operation === "toggle" ? "" : `/${operation}`}`, operation === "toggle" ? "PATCH" : "POST", operation === "toggle" ? { enabled: !connection.enabled } : undefined);
      const updated = "connection" in result ? result.connection : result;
      if (updated.last_error) setError(updated.last_error);
      else {
        setNotice(operation === "validate" ? "Connection checked. Refresh data imports your accounts; allow one minute before the next provider read." : operation === "sync" ? "Connection refreshed." : operation === "disconnect" ? "Disconnected. Saved financial history is retained." : "Connection updated.");
        if (operation === "sync") setNextProvider(connection.provider);
      }
      setDisconnecting(null); state.refetch();
    } catch (cause) { setError((cause as Error).message); } finally { setBusy(null); }
  }
  if (state.error) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading connections…" />;
  return <div className="space-y-4">
    <InstitutionDirectory />
    <p className="text-sm leading-relaxed text-mist">Use Lunch Flow for banks and SnapTrade for investments. Connect either or both, and manage each connection here.</p>
    {error && <ErrorBox error={error} />}{notice && <p role="status" className="text-sm text-teal">{notice}</p>}
    {nextProvider && <div className="space-y-2 rounded-xl border border-teal/20 bg-teal/5 p-4"><p className="text-sm">{nextProvider === "lunchflow" ? "Review your bank accounts and link each to its saved history, or create an account for a new bank. If no accounts appear, check Account Access in Lunch Flow." : "Review your brokerage accounts, balances and holdings. If no accounts appear, check that your brokerage is connected in SnapTrade."}</p><a className="btn" href={nextProvider === "lunchflow" ? "#banking" : "#connected-portfolio"}>{nextProvider === "lunchflow" ? "Review bank accounts" : "Review investments"}</a></div>}
    {hostedProviders.length ? <div className="space-y-2"><p className="text-sm text-mist">Authorize access securely with your provider.</p><div className="flex flex-wrap gap-2">{hostedProviders.map(provider => <button key={provider.provider} className="btn" disabled={busy !== null || editing !== null} onClick={() => void authorize(provider.provider, provider.label)}>{busy === provider.provider ? "Opening provider…" : `Connect with ${provider.label}`}</button>)}</div></div> : null}
    {!editing && <button className="btn" onClick={() => setEditing("new")}>Add provider connection</button>}
    {editing && <ConnectionForm key={editing === "new" ? "new" : editing.id} providers={state.data.providers} connection={editing === "new" ? undefined : editing} onDone={saved => {
      setEditing(null); state.refetch();
      if (saved) {
        setError(saved.error ? `Connection saved, but the refresh needs attention. ${saved.error}` : "");
        setNotice(saved.error ? "" : saved.refreshed ? "Connected and refreshed. Review the data available from your provider." : "Connection saved.");
        setNextProvider(saved.refreshed ? saved.connection.provider : null);
      }
    }} />}
    <ul className="space-y-3">{state.data.connections.map(connection => <li key={connection.id} className="space-y-3 rounded-xl border border-line p-4">
      <div><p className="break-words text-sm font-medium">{connection.label}</p><p className="text-xs text-mist">{state.data?.providers.find(provider => provider.id === connection.provider)?.label || connection.provider} · {connection.status.replace(/_/g, " ")}{!connection.enabled && " · Paused"}</p></div>
      {connection.last_synced_at && <p className="text-xs text-mist">Last refreshed: {new Date(connection.last_synced_at).toLocaleString()}</p>}
      {connection.last_error && <p className="text-sm text-coral">{connection.last_error}</p>}
      {connection.source === "environment" && <p className="text-xs text-mist">Managed by your server configuration. Change these credentials in the installation settings.</p>}
      {!authorization.loading && !authorizationAvailable(connection) && <p className="text-xs text-mist">Provider authorization is unavailable on this installation. Your saved history remains available.</p>}
      <div className="flex flex-wrap gap-2">
        <button className="btn" disabled={busy !== null || editing !== null || !connection.enabled || connection.status === "disconnected" || !authorizationAvailable(connection)} onClick={() => void action(connection, "sync")}>{busy === connection.id ? "Working…" : "Refresh data"}</button>
        <button className="btn" disabled={busy !== null || editing !== null || !connection.enabled || connection.status === "disconnected" || !authorizationAvailable(connection)} onClick={() => void action(connection, "validate")}>Check connection</button>
        {connection.source === "saved" && <>{hostedConnection(connection) && <button className="btn" disabled={busy !== null || editing !== null || !authorizationAvailable(connection)} onClick={() => void authorize(connection.provider, connection.label, connection.id)}>Reconnect with provider</button>}<button className="btn" disabled={busy !== null || editing !== null} onClick={() => setEditing(connection)}>Edit connection</button><button className="btn" disabled={busy !== null || editing !== null || connection.status === "disconnected"} onClick={() => void action(connection, "toggle")}>{connection.enabled ? "Pause" : "Resume"}</button>{connection.status !== "disconnected" && <button className="btn" disabled={busy !== null || editing !== null} onClick={() => setDisconnecting(connection.id)}>Disconnect</button>}</>}
      </div>
      {disconnecting === connection.id && <div className="space-y-2 rounded-lg bg-mist/5 p-3"><p className="text-sm">Disconnect {connection.label}? Its saved credentials will be removed. Your recorded financial history stays available.</p><div className="flex flex-wrap gap-2"><button className="btn" disabled={busy !== null || editing !== null} onClick={() => void action(connection, "disconnect")}>Confirm disconnect</button><button className="btn" disabled={busy !== null || editing !== null} onClick={() => setDisconnecting(null)}>Keep connection</button></div></div>}
    </li>)}</ul>
    {!state.data.connections.length && <p className="text-sm text-mist">No provider connections yet. Add one above, or import a CSV into a manual account.</p>}
  </div>;
}
