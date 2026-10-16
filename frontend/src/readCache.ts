/** Short-lived, session-memory reuse for private bank reads. Nothing is persisted. */
type Entry = {
  promise: Promise<unknown>; controller: AbortController; expiresAt: number;
  settled: boolean; users: number; abortTimer?: ReturnType<typeof setTimeout>;
};
const entries = new Map<string, Entry>();
const MAX_ENTRIES = 60;
const DEFAULT_TTL = 15_000;
function cancelled(): Error { return new DOMException("The request was cancelled.", "AbortError"); }
function keyFor(path: string): string {
  const url = new URL(path, "http://ledger.local");
  url.searchParams.sort();
  return `${url.pathname}${url.search}`;
}
export function clearReadCache(prefix?: string) {
  for (const [key] of entries) if (!prefix || key.startsWith(prefix)) entries.delete(key);
  // A request already being read by a component may finish; deleted entries can
  // never repopulate the map or overwrite a newer read after a confirmed write.
}
export function readCached<T>(path: string, load: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal, ttl = DEFAULT_TTL): Promise<T> {
  if (signal?.aborted) return Promise.reject(cancelled());
  const key = keyFor(path), now = Date.now();
  let entry = entries.get(key);
  if (entry?.controller.signal.aborted || (entry?.settled && entry.expiresAt <= now)) { entries.delete(key); entry = undefined; }
  if (!entry) {
    const controller = new AbortController();
    entry = { controller, expiresAt: Infinity, settled: false, users: 0, promise: Promise.resolve() };
    const created = entry;
    created.promise = Promise.resolve().then(() => load(controller.signal)).then(value => {
      created.settled = true; created.expiresAt = Date.now() + ttl;
      return value;
    }, error => {
      created.settled = true;
      if (entries.get(key) === created) entries.delete(key);
      throw error;
    });
    entries.set(key, created);
    if (entries.size > MAX_ENTRIES) {
      for (const [oldKey, old] of entries) {
        if (oldKey !== key && old.settled) entries.delete(oldKey);
        if (entries.size <= MAX_ENTRIES) break;
      }
    }
  }
  const shared = entry;
  if (shared.abortTimer) { clearTimeout(shared.abortTimer); shared.abortTimer = undefined; }
  shared.users++;
  return new Promise<T>((resolve, reject) => {
    let done = false;
    function release() {
      if (done) return false;
      done = true; signal?.removeEventListener("abort", abort); shared.users--;
      if (!shared.settled && shared.users === 0) {
        // A brief grace period lets StrictMode and a changed hook subscription
        // reuse the same request while still cancelling abandoned searches.
        shared.abortTimer = setTimeout(() => {
          if (!shared.settled && shared.users === 0) {
            if (entries.get(key) === shared) entries.delete(key);
            shared.controller.abort();
          }
        }, 40);
      }
      return true;
    }
    function abort() { if (release()) reject(cancelled()); }
    signal?.addEventListener("abort", abort, { once: true });
    shared.promise.then(value => { if (release()) resolve(value as T); }, error => { if (release()) reject(error); });
  });
}
