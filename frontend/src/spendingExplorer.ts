import { authRequest } from "./authRequest";
import { readCached } from "./readCache";
export type SpendCell = {
  month: string;
  category: string;
  merchant: string;
  n: number;
  gross: number;
  refunds: number;
  net: number;
};
export type SpendExplorerData = {
  cells: SpendCell[];
  months: string[];
  weekly_cells: (Omit<SpendCell, "month"> & { week: string })[];
  weeks: string[];
  weekly_coverage: Record<string, { coverage_complete: boolean }>;
  coverage: Record<
    string,
    { coverage_complete: boolean; coverage_label: string }
  >;
  date_from: string | null;
  date_to: string | null;
};
export async function getSpendingExplorer(
  query: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<SpendExplorerData> {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "")
      params.set(key, String(value));
  });
  const path = `/api/spending-explorer?${params}`;
  return readCached(path, async requestSignal => {
    const abort = new AbortController();
    const cancel = () => abort.abort();
    requestSignal.addEventListener("abort", cancel, { once: true });
    const timeout = setTimeout(() => abort.abort(), 30000);
    try {
      const res = await authRequest(path, { signal: abort.signal });
      const data = await res.json();
      if (!res.ok) throw Error(typeof data.detail === "string" ? data.detail : "Could not load spending. Check the selected dates.");
      return data;
    } catch (error) {
      if (requestSignal.aborted) throw new DOMException("The request was cancelled.", "AbortError");
      if (abort.signal.aborted) throw Error("The local server took too long. Retry the spending breakdown.");
      throw error;
    } finally { clearTimeout(timeout); requestSignal.removeEventListener("abort", cancel); }
  }, signal);
}
export function spendBreakdown(cells: SpendCell[], selected: string[] | null) {
  const categories = new Map<string, number>(),
    vendors = new Map<
      string,
      {
        merchant: string;
        net: number;
        gross: number;
        refunds: number;
        n: number;
      }
    >();
  let net = 0,
    gross = 0,
    refunds = 0,
    n = 0;
  for (const c of cells) {
    categories.set(c.category, (categories.get(c.category) ?? 0) + c.net);
    if (selected !== null && !selected.includes(c.category)) continue;
    net += c.net;
    gross += c.gross;
    refunds += c.refunds;
    n += c.n;
    const v = vendors.get(c.merchant) ?? {
      merchant: c.merchant,
      net: 0,
      gross: 0,
      refunds: 0,
      n: 0,
    };
    v.net += c.net;
    v.gross += c.gross;
    v.refunds += c.refunds;
    v.n += c.n;
    vendors.set(c.merchant, v);
  }
  return {
    net,
    gross,
    refunds,
    n,
    categories: [...categories]
      .map(([category, net]) => ({ category, net }))
      .sort((a, b) => b.net - a.net),
    vendors: [...vendors.values()].sort((a, b) => b.net - a.net),
  };
}
export function readExplorerPreferences(key: string) {
  const fallback = {
    period: "all",
    account: "",
    from: "",
    to: "",
    selected: null as string[] | null,
    vendorSearch: "",
  };
  try {
    const v = JSON.parse(
      sessionStorage.getItem(`ledger.explorer.${key}`) ?? "null",
    );
    if (!v || typeof v !== "object") return fallback;
    return {
      period: ["all", "year", "12", "custom"].includes(v.period)
        ? v.period
        : "all",
      account: typeof v.account === "string" ? v.account : "",
      from:
        typeof v.from === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v.from)
          ? v.from
          : "",
      to:
        typeof v.to === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v.to)
          ? v.to
          : "",
      selected:
        Array.isArray(v.selected) &&
        v.selected.every((x: unknown) => typeof x === "string")
          ? (v.selected as string[])
          : null,
      vendorSearch: typeof v.vendorSearch === "string" ? v.vendorSearch : "",
    };
  } catch {
    return fallback;
  }
}
export function saveExplorerPreferences(
  key: string,
  value: ReturnType<typeof readExplorerPreferences>,
) {
  try {
    sessionStorage.setItem(`ledger.explorer.${key}`, JSON.stringify(value));
  } catch {
    /* Browser storage can be unavailable. */
  }
}
