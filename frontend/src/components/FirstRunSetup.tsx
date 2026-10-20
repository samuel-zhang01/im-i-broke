import { Heading } from "./Heading";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { QRCodeSVG } from "qrcode.react";
import { authRequest } from "../authRequest";
import { ErrorBox, Spinner } from "./ui";

type Challenge = { challenge_id: string; secret: string; provisioning_uri: string; expires_at: string | number };

async function setupRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await authRequest(`/api/setup/${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error("Couldn’t reach setup. Check your connection and try again."); }
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Setup could not finish. Please check your details and try again.");
  return result as T;
}

export default function FirstRunSetup({ onComplete, onRefresh }: {
  onComplete: (codes: string[]) => Promise<void>; onRefresh: () => void;
}) {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [statusError, setStatusError] = useState("");
  const [token, setToken] = useState("");
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    let mounted = true;
    setStatusError(""); setAvailable(null);
    setupRequest<{ available: boolean; configured: boolean }>("status")
      .then(result => { if (mounted) { setAvailable(result.available && !result.configured); if (result.configured) onRefresh(); } })
      .catch(cause => { if (mounted) setStatusError((cause as Error).message); });
    return () => { mounted = false; };
  }, [attempt]);
  useEffect(() => { heading.current?.focus(); }, [challenge]);

  async function submit(event: FormEvent) {
    event.preventDefault(); setError(""); setNotice("");
    if (challenge && password !== confirmation) { setError("The passwords do not match."); return; }
    setBusy(true);
    try {
      if (!challenge) setChallenge(await setupRequest<Challenge>("begin", { token }));
      else {
        const result = await setupRequest<{ ok: boolean; recovery_codes: string[] }>("enroll", {
          token, challenge_id: challenge.challenge_id, password, code,
        });
        setToken(""); setPassword(""); setConfirmation(""); setCode(""); setChallenge(null);
        await onComplete(result.recovery_codes);
      }
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  async function copyKey() {
    try { await navigator.clipboard.writeText(challenge!.secret); setNotice("Setup key copied. Paste it into your authenticator."); }
    catch { setNotice("Copy is unavailable here. Select the setup key below and copy it manually."); }
  }

  if (statusError) return <ErrorBox error={statusError} retry={() => setAttempt(value => value + 1)} />;
  if (available === null) return <Spinner label="Checking setup availability…" minHeight={100} />;
  if (!available) return <div className="space-y-4 text-sm text-mist">
    <p>Browser setup isn’t ready yet. Ask the person who installed this workspace to check its status.</p>
    <details><summary className="cursor-pointer py-2 font-medium text-foam">I installed this workspace</summary><p className="mt-2">In your installation folder, run <code>python3 manage.py check</code> to check readiness, then <code>python3 manage.py token</code> to retrieve the setup token. If only the token has expired, run <code>python3 manage.py renew-token</code>. For an older installation, follow the account setup steps in SECURITY.md.</p></details>
    <button className="btn" onClick={() => { setAttempt(value => value + 1); onRefresh(); }}>Check again</button>
  </div>;
  const field = "input mt-1 block min-h-11 w-full !text-base";
  return <form onSubmit={submit} className="space-y-5">
    <ol aria-label="Setup progress" className="flex items-center gap-2 text-xs text-mist">
      {["Welcome", "Protect", "Recovery"].map((label, index) => <li key={label} aria-current={index === (challenge ? 1 : 0) ? "step" : undefined} className={`flex flex-1 items-center gap-2 ${index === (challenge ? 1 : 0) ? "font-semibold text-teal" : ""}`}><span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-current">{index + 1}</span>{label}</li>)}
    </ol>
    <div>
      <Heading level={2} variant="intro" ref={heading} tabIndex={-1} className="outline-hidden">{challenge ? "Make it yours. Keep it private." : "Your finances, in one place."}</Heading>
      <p className="mt-2 text-sm leading-relaxed text-mist">{challenge ? "Choose a password, then add a second layer of protection with an authenticator app." : "Start with a secure account. Then connect a bank, add an investment account, or bring your own CSV."}</p>
    </div>
    {error && <ErrorBox error={error} />}
    <fieldset disabled={busy} className="min-w-0 space-y-4">
    {!challenge ? <>
      <label className="block text-sm font-medium">Setup token<input className={field} type="password" required autoComplete="off" spellCheck={false} minLength={32} maxLength={128} value={token} onChange={event => setToken(event.target.value.trim())} aria-describedby="setup-token-help" /></label>
      <p id="setup-token-help" className="text-xs leading-relaxed text-mist">Paste the one-time token shown when your workspace was installed. It expires after 24 hours.</p>
    </> : <>
      <div className="space-y-3">
        <label className="block text-sm font-medium">New password<input className={field} type="password" required autoComplete="new-password" minLength={15} maxLength={256} value={password} onChange={event => setPassword(event.target.value)} aria-describedby="setup-password-help" /></label>
        <p id="setup-password-help" className="text-xs text-mist">Use at least 15 characters. A few memorable words work well.</p>
        <label className="block text-sm font-medium">Confirm password<input className={field} type="password" required autoComplete="new-password" minLength={15} maxLength={256} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      </div>
      <div className="rounded-xl border border-line bg-surface2/50 p-4">
        <p className="text-sm font-semibold">Add Am I Broke? to your authenticator</p>
        <p className="mt-1 text-xs leading-relaxed text-mist">Open your authenticator app, choose to add an account, and scan this code.</p>
        <QRCodeSVG value={challenge.provisioning_uri} size={176} marginSize={4} className="mx-auto my-3 max-w-full" role="img" aria-label="Scan to add Am I Broke? to your authenticator" />
        <details>
          <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">Can’t scan? Use a setup key</summary>
          <p className="mb-2 text-xs leading-relaxed text-mist">Choose a time-based account with six digits changing every 30 seconds. Use Am I Broke? as its name.</p>
          <code aria-label="Authenticator setup key" className="block select-all break-all rounded-lg bg-white p-3 text-sm">{challenge.secret}</code>
          <button type="button" className="btn mt-2" onClick={() => void copyKey()}>Copy setup key</button>
          {notice && <p role="status" className="mt-2 text-xs text-mist">{notice}</p>}
        </details>
      </div>
      <label className="block text-sm font-medium">Authenticator code<input className={`${field} tracking-[.2em]`} required autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={event => setCode(event.target.value.replace(/\s/g, ""))} placeholder="000000" aria-describedby="setup-code-help" /></label>
      <p id="setup-code-help" className="text-xs text-mist">Enter the current six-digit code. Finish before {new Date(typeof challenge.expires_at === "number" ? challenge.expires_at * 1000 : challenge.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}, or start again for a fresh key.</p>
    </>}
    </fieldset>
    <button className="btn-primary !min-h-11 w-full" disabled={busy}>{busy ? "Please wait…" : challenge ? "Create owner account" : "Begin secure setup"}</button>
    {challenge && <button type="button" className="min-h-11 text-sm text-mist underline underline-offset-4" disabled={busy} onClick={() => { setChallenge(null); setCode(""); setPassword(""); setConfirmation(""); setError(""); setNotice(""); }}>Start again with a new authenticator key</button>}
  </form>;
}
