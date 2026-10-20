import { Heading } from "./components/Heading";
import { createContext, useContext, FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { authRequest } from './authRequest';
import { clearReadCache } from './readCache';
import { clearWorkspaceState } from './workspaceStore';
import BrandMark from './components/BrandMark';
import { Spinner } from './components/ui';
import FirstRunSetup from './components/FirstRunSetup';
import ConnectionAuthorizationGate from './components/ConnectionAuthorizationGate';

type Status = { enabled: boolean; configured: boolean; authenticated: boolean };

type SecuritySettings = { enabled: boolean; busy: boolean; error: string; recovery: () => void; logout: () => void };
const SecurityContext = createContext<SecuritySettings | null>(null);
export function useSecuritySettings() { return useContext(SecurityContext); }

export default function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [security, setSecurity] = useState(false);
  const [codes, setCodes] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const [remember, setRemember] = useState(false);
  const [setupCodes, setSetupCodes] = useState(false);
  const [copyNotice, setCopyNotice] = useState('');
  const recoveryHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (codes.length) recoveryHeading.current?.focus(); }, [codes]);
  const pendingStatus = useRef<Promise<Status | undefined> | null>(null);
  const statusVersion = useRef(0);
  const refresh = useCallback((force = false): Promise<Status | undefined> => {
    if (!force && pendingStatus.current) return pendingStatus.current;
    const version = ++statusVersion.current;
    const request = (async () => {
      try {
        const response = await authRequest('/api/auth/status');
        if (!response.ok) throw new Error('Unable to check sign-in status. Please try again.');
        const current = await response.json() as Status;
        if (version === statusVersion.current) {
          if (!current.authenticated) { clearReadCache(); clearWorkspaceState(); }
          setStatus(current);
        }
        return current;
      } catch (e) {
        if (version === statusVersion.current) setError((e as Error).message);
      } finally {
        if (version === statusVersion.current) pendingStatus.current = null;
      }
    })();
    pendingStatus.current = request;
    return request;
  }, []);
  useEffect(() => {
    void refresh();
    const expired = () => { clearWorkspaceState(); ++statusVersion.current; pendingStatus.current = null; setStatus(s => s ? { ...s, authenticated: false } : s); setSecurity(false); };
    const focused = () => { if (document.visibilityState === 'visible') void refresh(); };
    window.addEventListener('ledger:session-expired', expired);
    window.addEventListener('focus', focused);
    document.addEventListener('visibilitychange', focused);
    const timer = window.setInterval(focused, 60_000);
    return () => {
      window.removeEventListener('ledger:session-expired', expired);
      window.removeEventListener('focus', focused);
      document.removeEventListener('visibilitychange', focused);
      window.clearInterval(timer);
    };
  }, [refresh]);

  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const result = await authRequest(security ? '/api/auth/recovery-codes' : '/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password, code, ...(!security && { remember }) }),
      });
      const body = await result.json();
      if (!result.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Please check your password and code.');
      setPassword(''); setCode('');
      if (security) { setCodes(body.codes); setSetupCodes(false); setSaved(false); setSecurity(false); }
      const current = await refresh(true);
      if (!security && current && !current.authenticated) throw new Error('Your browser did not retain the session. Use your HTTPS Am I Broke? address.');
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function logout() {
    setBusy(true); setError('');
    try {
      const response = await authRequest('/api/auth/logout', { method: 'POST' });
      if (!response.ok && response.status !== 401) throw new Error('Sign out failed. Please try again.');
      ++statusVersion.current; pendingStatus.current = null;
      setStatus(s => s ? { ...s, authenticated: false } : s);
      clearReadCache(); setCodes([]); setPassword(''); setCode(''); setSecurity(false);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function copyRecoveryCodes() {
    try { await navigator.clipboard.writeText(codes.join('\n')); setCopyNotice('Recovery codes copied. Save them in your password manager.'); }
    catch { setCopyNotice('Copy is unavailable here. Select the codes and copy them manually.'); }
  }
  function continueFromRecovery() {
    if (setupCodes) {
      try { sessionStorage.setItem('ledger.onboarding', 'start'); sessionStorage.setItem('ledger.tab', 'settings'); } catch { /* The setup guide is also available in Settings. */ }
      window.history.replaceState(null, '', '#settings');
    }
    setCodes([]); setCopyNotice('');
  }

  if (status?.authenticated && !security && codes.length === 0) return <SecurityContext.Provider value={{
    enabled: status.enabled, busy, error,
    recovery: () => { setSecurity(true); setError(''); }, logout: () => void logout(),
  }}><ConnectionAuthorizationGate>{children}</ConnectionAuthorizationGate></SecurityContext.Provider>;

  return <main className="flex min-h-screen items-center justify-center p-5">
    <section className="w-full max-w-md rounded-2xl border border-line bg-white p-5 shadow-sm sm:p-7">
      <div className="mb-5 flex items-center gap-3">
        <BrandMark className="h-11 w-11" />
        <div>
          <p className="text-base font-semibold text-stone-800">Am I Broke<span className="text-[#db704a]">?</span></p>
          <p className="mt-0.5 text-[10px] uppercase tracking-[.15em] text-stone-500">Private finances</p>
        </div>
      </div>
      <Heading ref={recoveryHeading} tabIndex={-1} level={1} variant="page" className="mb-5 outline-hidden">{codes.length ? 'Save your recovery codes' : security ? 'Replace recovery codes' : !status ? 'Welcome back' : !status.configured ? 'Set up your workspace' : 'Sign in to Am I Broke?'}</Heading>
      {error && <p role="alert" className="mb-4 text-red-700">{error}</p>}
      {codes.length > 0 ? <>
        <p className="mb-4 text-sm leading-relaxed text-mist">These codes are shown once. Store them in your password manager or offline. Each replaces one authenticator code; you still need your password.{!setupCodes && ' Your previous codes and sessions have been revoked.'}</p>
        {setupCodes && <p className="mb-3 text-xs font-medium text-teal">Step 3 of 3 · Keep a way back in</p>}
        <pre className="mb-3 select-all overflow-x-auto rounded-xl bg-surface2 p-4 text-sm leading-relaxed">{codes.join('\n')}</pre>
        <button className="btn mb-4" onClick={() => void copyRecoveryCodes()}>Copy recovery codes</button>
        {copyNotice && <p role="status" className="mb-4 text-xs text-mist">{copyNotice}</p>}
        <label className="mb-4 flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={saved} onChange={e => setSaved(e.target.checked)} />I saved these codes securely.</label>
        <button className="btn-primary !min-h-11 w-full" disabled={!saved} onClick={continueFromRecovery}>{setupCodes && status?.authenticated ? 'Open your workspace' : 'Continue to sign in'}</button>
      </> : !status ? <><Spinner label="Checking access…" />{error && <button className="mt-4 underline" onClick={() => void refresh()}>Retry</button>}</> : !status.configured ? <FirstRunSetup onRefresh={() => void refresh()} onComplete={async recoveryCodes => {
        setCodes(recoveryCodes); setSetupCodes(true); setSaved(false); await refresh(true);
      }} /> : <form onSubmit={submit}>
        <p className="mb-5 text-sm text-stone-600">{security ? 'Enter your password and a fresh authenticator code (or unused recovery code). Replacing codes signs out every session.' : 'Enter your password and the current six-digit code from your authenticator, or one saved recovery code.'}</p>
        <label className="mb-4 block">Password<input required type="password" autoComplete="current-password" maxLength={256} value={password} onChange={e => setPassword(e.target.value)} className="mt-1 block w-full rounded border border-stone-300 p-3" /></label>
        <label className="mb-5 block">Authenticator or recovery code<input required type="text" autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} maxLength={64} value={code} onChange={e => setCode(e.target.value)} className="mt-1 block w-full rounded border border-stone-300 p-3" /></label>
        {!security && <label className="mb-5 flex items-start gap-3 text-sm text-stone-600">
          <input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} className="mt-1" aria-describedby="remember-session-detail" />
          <span>Keep me signed in for 30 days<span id="remember-session-detail" className="mt-1 block text-xs text-stone-500">On this device. Signs out after 7 days without activity.</span></span>
        </label>}
        <button type="submit" disabled={busy} className="w-full rounded bg-stone-800 p-3 text-white">{busy ? 'Checking…' : security ? 'Generate new recovery codes' : 'Sign in'}</button>
        {security && <button type="button" className="mt-4 underline" onClick={() => { setSecurity(false); setPassword(''); setCode(''); setError(''); }}>Cancel</button>}
      </form>}
    </section>
  </main>;
}
