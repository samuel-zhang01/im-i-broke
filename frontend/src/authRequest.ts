import { clearReadCache } from "./readCache";
import { clearWorkspaceState } from "./workspaceStore";
/** Same-origin cookies plus a non-simple header protect authenticated mutations. */
export async function authRequest(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers);
  headers.set('X-Ledger-Request', '1');
  const result = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
  if (result.status === 401 || (result.ok && /^\/api\/auth\/(login|logout|verify)/.test(path))) { clearReadCache(); clearWorkspaceState(); }
  if (result.status === 401 && !path.startsWith('/api/auth/')) {
    window.dispatchEvent(new Event('ledger:session-expired'));
  }
  return result;
}
