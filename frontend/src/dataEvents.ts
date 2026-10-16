import { authRequest } from "./authRequest";
import { clearReadCache } from "./readCache";
// Invalidate loaded data after a confirmed write. Messages contain no ledger data.
type Listener = () => void;
const listeners = new Set<Listener>();
let channel: BroadcastChannel | undefined;
let stopWatching: (() => void) | undefined;
let scheduled = false;
let revision: string | undefined;

function emit() {
  clearReadCache();
  if (scheduled) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    listeners.forEach((listener) => listener());
  });
}

export function dataChanged(path: string) {
  // Saving display preferences or caching public research doesn't change totals.
  if (!/^(merchants|merchant-suggestions|transactions|scenarios|valuation|banking|workspace|connections|investments|wealth)(\/|$)/.test(path)) return;
  emit();
  channel?.postMessage("changed");
}

export function watchData(listener: Listener) {
  listeners.add(listener);
  if (!stopWatching && typeof window !== "undefined") {
    const visible = () => document.visibilityState !== "hidden";
    const resume = () => { if (visible()) void check(); };
    window.addEventListener("focus", resume);
    window.addEventListener("online", resume);
    document.addEventListener("visibilitychange", resume);
    try {
      channel = new BroadcastChannel("ledger-data");
      channel.onmessage = (event) => { if (event.data === "changed") emit(); };
    } catch { /* Focus and revision polling also work without browser storage. */ }
    let missedCheck = false;
    let active = true;
    let checking = false;
    let controller: AbortController | undefined;
    const check = async () => {
      if (!visible() || checking) return;
      checking = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 8000);
      try {
        const result = await authRequest("/api/revision", { cache: "no-store", signal: controller.signal });
        if (!result.ok) { missedCheck = true; return; }
        const body = await result.json();
        if (!active) return;
        if (typeof body.revision !== "string") { missedCheck = true; return; }
        // The first successful poll establishes a baseline. Reopening a page
        // keeps the previous revision, so unchanged data does not fetch twice.
        // Failed polling, external edits and cross-tab writes still refresh.
        if ((revision !== undefined && revision !== body.revision) || missedCheck) emit();
        revision = body.revision;
        missedCheck = false;
      } catch { missedCheck = true; /* Keep displayed data until connection recovers. */ }
      finally { clearTimeout(timeout); checking = false; }
    };
    void check();
    const timer = setInterval(() => void check(), 15000);
    stopWatching = () => {
      active = false;
      controller?.abort();
      clearInterval(timer);
      window.removeEventListener("focus", resume);
      window.removeEventListener("online", resume);
      document.removeEventListener("visibilitychange", resume);
      channel?.close();
      channel = undefined;
    };
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) { stopWatching?.(); stopWatching = undefined; }
  };
}
