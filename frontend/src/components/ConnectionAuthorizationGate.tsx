import { useEffect, useState, type ReactNode } from "react";
import { getPendingAuthorization, clearPendingAuthorization } from "../connectionAuthorization";
import { productRequest } from "../productApi";
import { Heading } from "./Heading";
import { Card, Spinner } from "./ui";

// React's development remount must never exchange a one-use authorization twice.
let completion: Promise<unknown> | null = null;
export default function ConnectionAuthorizationGate({ children }: { children: ReactNode }) {
  const [pending] = useState(getPendingAuthorization);
  const [finished, setFinished] = useState(false); const [busy, setBusy] = useState(Boolean(pending && !pending.error));
  const [error, setError] = useState(pending?.error || "");
  useEffect(() => {
    if (!pending || pending.error) return;
    let mounted = true;
    completion ??= productRequest("connections/authorization/complete", "POST", pending);
    completion.then(() => { if (mounted) setBusy(false); }).catch(cause => { if (mounted) { setError((cause as Error).message); setBusy(false); } });
    return () => { mounted = false; };
  }, [pending]);
  if (!pending || finished) return <>{children}</>;
  return <main className="flex min-h-screen items-center justify-center p-5"><Card className="w-full max-w-lg space-y-4">
    <Heading level={1} variant="page">{busy ? "Finishing your connection" : error ? "Connection needs attention" : "Your provider is connected"}</Heading>
    {busy ? <Spinner label="Confirming your provider authorization…" /> : <>
      <p role={error ? "alert" : "status"} className="text-sm">{error || "Your account authorization has been saved. Open Provider connections in Settings to review access and refresh your data."}</p>
      <button className="btn" onClick={() => { clearPendingAuthorization(); completion = null; window.history.replaceState(null, "", "/#settings"); setFinished(true); }}>Open Settings</button>
    </>}
  </Card></main>;
}
