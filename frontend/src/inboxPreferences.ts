// Per-tab UI state only. Unavailable or malformed browser storage is harmless.
export function readInboxPreference<T extends string | boolean | number>(key: string, fallback: T): T {
  try {
    const value = JSON.parse(sessionStorage.getItem(`ledger.inbox.${key}`) ?? "null");
    if (typeof value !== typeof fallback) return fallback;
    const allowed: Record<string, string[]> = {filter: ["uncategorized", "identity", "all"], context: ["", "card", "transfer", "spending"], sort: ["outgoing", "smallest", "frequent", "recent", "oldest", "name"]};
    if (allowed[key] && !allowed[key].includes(value)) return fallback;
    if (typeof value === "number" && (!Number.isInteger(value) || value < 1 || value > 100000)) return fallback;
    return value;
  } catch { return fallback; }
}
export function writeInboxPreference(key: string, value: string | boolean | number) {
  try { sessionStorage.setItem(`ledger.inbox.${key}`, JSON.stringify(value)); } catch { /* Storage may be disabled. */ }
}
