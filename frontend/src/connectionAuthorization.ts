export type AuthorizationCallback = { provider: "lunchflow" | "snaptrade"; state: string; code?: string; error?: string };

/** Remove authorization parameters before rendering or fetching account data. */
export function captureAuthorizationCallback(address: string, replace: (path: string) => void): AuthorizationCallback | null {
  const url = new URL(address);
  if (!url.pathname.startsWith("/connect/callback/")) return null;
  replace("/");
  const provider = url.pathname.split("/").pop();
  if (provider !== "lunchflow" && provider !== "snaptrade") return null;
  const state = url.searchParams.get("state") || "";
  const code = url.searchParams.get("code") || undefined;
  if (url.searchParams.has("error") || (provider === "snaptrade" && ["ERROR", "ABANDONED"].includes((url.searchParams.get("status") || "").toUpperCase()))) {
    return { provider, state, error: "The provider did not authorize this connection. You can start again from Settings." };
  }
  if (!state || state.length > 2048 || (code?.length || 0) > 8192 || (provider === "lunchflow" && !code)) {
    return { provider, state: "", error: "The connection response was incomplete. Start again from Settings." };
  }
  return { provider, state, code };
}

let pending = typeof window === "undefined" ? null : captureAuthorizationCallback(window.location.href, path => window.history.replaceState(null, "", path));
export const getPendingAuthorization = () => pending;
export function clearPendingAuthorization() { pending = null; }
