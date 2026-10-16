import { useState } from "react";
import { productRequest } from "../productApi";
import { ErrorBox, Spinner, useAsync } from "./ui";

type Session = { id: string; created_at: number; last_active_at: number; remembered: boolean; current: boolean };
export default function SessionSettings() {
  const state = useAsync(signal => productRequest<{ sessions: Session[] }>("auth/sessions", "GET", undefined, signal));
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  async function revokeOthers() {
    setBusy(true); setError("");
    try { await productRequest("auth/sessions/revoke-others", "POST"); setNotice("Other sessions have been signed out."); state.refetch(); }
    catch (cause) { setError((cause as Error).message); } finally { setBusy(false); }
  }
  if (state.error) return <ErrorBox error={state.error} retry={state.refetch} />;
  if (!state.data) return <Spinner label="Loading signed-in sessions…" />;
  return <div className="mt-3 space-y-3">
    <p className="text-sm text-mist">Review active sign-ins. Signing out other sessions keeps this browser signed in.</p>
    <ul className="space-y-2">{state.data.sessions.map(session => <li key={session.id} className="rounded-lg border border-line p-3"><p className="text-sm font-medium">{session.current ? "This session" : "Another session"}{session.remembered ? " · Remembered" : ""}</p><p className="text-xs text-mist">Signed in {new Date(session.created_at * 1000).toLocaleString()} · Last active {new Date(session.last_active_at * 1000).toLocaleString()}</p></li>)}</ul>
    <button className="btn" disabled={busy || !state.data.sessions.some(session => !session.current)} onClick={() => void revokeOthers()}>{busy ? "Signing out…" : "Sign out other sessions"}</button>
    {error && <ErrorBox error={error} />}{notice && <p role="status" className="text-sm text-teal">{notice}</p>}
  </div>;
}
