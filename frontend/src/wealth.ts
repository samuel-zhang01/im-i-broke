import { isKnown, portfolioDate } from "./connectedPortfolio";
import type { WealthAccount, WealthData, WealthPoint } from "./wealthApi";

export function selectedWealthAccounts(accounts: WealthAccount[], ids: string[]) {
  const selected = new Set(ids);
  return accounts.filter((account) => selected.has(account.id));
}

export function wealthSourceGroups(accounts: WealthAccount[]) {
  const linked = accounts.filter((account) => account.source_status === "linked");
  const unlinked = accounts.filter((account) => account.source_status !== "linked");
  return {
    active: unlinked.filter((account) => !account.archived && !account.archived_by_user && !account.history_only),
    archived: unlinked.filter((account) => account.archived || account.archived_by_user || account.history_only),
    linked,
  };
}

export function wealthSourceDate(account: WealthAccount) {
  const source = account.valuation?.reported_source ?? account.source;
  const prefix = account.valuation?.basis === "statement_activity_estimate" ? "Estimated through" : source === "lunchflow" ? "Fetched" : source === "statement" ? "Statement dated" : "Valued";
  return account.as_of ? `${prefix} ${portfolioDate(account.as_of)}` : account.valuation?.basis === "statement_activity_estimate" ? "Estimate date unknown" : source === "lunchflow" ? "Fetch date unknown" : source === "statement" ? "Statement date unknown" : "Valuation date unknown";
}

export function wealthValuationLabel(account: WealthAccount) {
  if (account.valuation?.current_unknown) return "Current balance unknown · excluded from total";
  if (account.valuation?.basis === "available_credit") return "Credit limit − available credit";
  if (account.valuation?.estimated) return "Estimated from statement + booked activity";
  return account.estimated ? "Estimated value" : "Reported balance";
}

export function wealthBalanceNeedsReview(account: WealthAccount) {
  return Boolean(account.balance_check && (account.balance_check.requires_review ?? !["no_discrepancy", "available_credit"].includes(account.balance_check.status)));
}

export function wealthComposition(accounts: WealthAccount[]) {
  const names = { investment: "Investments", cash: "Bank accounts", credit: "Credit balances" };
  const groups = new Map<string, { name: string; kind: WealthAccount["kind"]; value: number; accounts: number }>();
  for (const account of accounts) {
    if (!isKnown(account.value) || account.value <= 0) continue;
    const group = groups.get(account.kind) ?? { name: names[account.kind], kind: account.kind, value: 0, accounts: 0 };
    group.value += account.value;
    group.accounts += 1;
    groups.set(account.kind, group);
  }
  return [...groups.values()].sort((a, b) => b.value - a.value);
}

export function pointHasIncompleteCoverage(point: WealthPoint) {
  return point.complete === false || point.missing_accounts > 0 || point.estimated;
}

export function chartPointValue(point: WealthPoint): number | null {
  return point.complete !== false && isKnown(point.net_worth) && point.known_accounts > 0 ? point.net_worth : null;
}

export function wealthPointEvidence(point: WealthPoint): string {
  if (chartPointValue(point) === null) return point.known_accounts > 0 ? "Incomplete account coverage" : "No supported valuation";
  const basis = point.valuation_basis;
  const reconstructed = point.reconstructed_accounts ?? 0;
  if (basis === "daily_holdings") return "Daily holdings, market prices & dated FX · estimate";
  if (basis === "provider_estimate") return "Broker-provided history · estimate";
  if (basis === "mixed") return `${reconstructed > 0 ? `Mixed sources · ${reconstructed} account${reconstructed === 1 ? "" : "s"} reconstructed daily` : "Mixed historical sources"}${point.estimated ? " · estimate" : ""}`;
  return point.estimated ? "Recorded balance · estimated conversion or value" : "Recorded balance";
}

export function wealthChartHistory(data: WealthData) {
  if (data.history) return { ...data.history, comparable: true };
  return {
    points: data.points, account_count: data.summary.selected_accounts, selected_accounts: data.summary.selected_accounts,
    account_ids: data.selected_account_ids, excluded_account_ids: [] as string[], current_value: data.summary.net_worth, end_value: data.points[data.points.length - 1]?.net_worth ?? null,
    change: data.summary.change, change_pct: data.summary.change_pct, change_comparable: data.summary.change_comparable,
    start_date: data.points[0]?.date ?? null, end_date: data.points[data.points.length - 1]?.date ?? null,
    coverage_note: "Account coverage varies across these dated observations.", comparable: false,
  };
}

/** Only all-account observations can be presented as net-assets history. */
export function wealthNetAssetsHistory(data: WealthData) {
  const history = wealthChartHistory(data);
  const candidates = data.history?.net_points ?? data.points;
  const first = candidates.findIndex(point => point.complete !== false && point.missing_accounts === 0
    && point.known_accounts === data.summary.selected_accounts && isKnown(point.net_worth));
  const points = first < 0 ? [] : candidates.slice(first).map(point =>
    point.missing_accounts === 0 && point.known_accounts === data.summary.selected_accounts ? point
      : { ...point, complete: false, net_worth: null, assets: null, liabilities: null });
  const last = [...points].reverse().find(point => chartPointValue(point) !== null);
  const key = points[0]?.valuation_key;
  const comparable = points.length > 1 && Boolean(key) && points.every(point =>
    chartPointValue(point) !== null && point.valuation_key === key && !point.source_break);
  const change = comparable && isKnown(points[0].net_worth) && last && isKnown(last.net_worth)
    ? Math.round((last.net_worth - points[0].net_worth) * 100) / 100 : null;
  return { ...history, points, account_count: data.summary.selected_accounts,
    account_ids: data.selected_account_ids, excluded_account_ids: [],
    net_start_date: points[0]?.date ?? null, end_date: last?.date ?? null,
    end_value: last?.net_worth ?? null, current_value: data.summary.net_worth,
    change, change_pct: change !== null && points[0].net_worth
      ? change / Math.abs(points[0].net_worth) * 100 : null,
    change_comparable: change !== null };
}

export function filterWealthAccounts(accounts: WealthAccount[], query: string, scope: "all" | "assets" | "debt" | "unknown") {
  const term = query.trim().toLocaleLowerCase();
  return accounts.filter((account) => (!term || `${account.name} ${account.institution} ${account.currency ?? ""}`.toLocaleLowerCase().includes(term))
    && (scope === "all" || scope === "unknown" ? scope === "all" || !isKnown(account.value) : isKnown(account.value) && (scope === "debt" ? account.value < 0 : account.value >= 0)))
    .sort((a, b) => {
      if (!isKnown(a.value)) return isKnown(b.value) ? 1 : a.name.localeCompare(b.name);
      if (!isKnown(b.value)) return -1;
      return Math.abs(b.value) - Math.abs(a.value) || a.name.localeCompare(b.name);
    });
}

export function clampWealthDates(points: WealthPoint[], start: string, end: string) {
  const dates = points.map((point) => point.date.slice(0, 10)).sort();
  if (!dates.length) return { start: "", end: "" };
  const low = dates[0], high = dates[dates.length - 1];
  const safeStart = start >= low && start <= high ? start : low;
  const safeEnd = end >= low && end <= high ? end : high;
  return safeStart <= safeEnd ? { start: safeStart, end: safeEnd } : { start: safeEnd, end: safeStart };
}

export function wealthHistoryDateLabel(timestamp: number, start: string, end: string): string {
  const broadWindow = Date.parse(end) - Date.parse(start) > 180 * 86400000;
  return new Date(timestamp).toLocaleDateString("en-GB", {
    month: "short", timeZone: "UTC",
    ...(broadWindow ? { year: "2-digit" as const } : { day: "numeric" as const, ...(start.slice(0, 4) !== end.slice(0, 4) ? { year: "2-digit" as const } : {}) }),
  });
}

export function wealthHistorySegments(points: WealthPoint[]) {
  const segments: { partial: boolean; values: (number | null)[] }[] = [];
  let previousKey: string | null = null;
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    const value = chartPointValue(point);
    if (value === null) { previousKey = null; continue; }
    const key = `${point.coverage_key ?? `${point.known_accounts}:${point.missing_accounts}`}|${point.valuation_key ?? point.valuation_basis ?? "recorded_balance"}`;
    if (key !== previousKey || point.source_break) segments.push({ partial: false, values: points.map(() => null) });
    const segment = segments[segments.length - 1];
    segment.partial ||= pointHasIncompleteCoverage(point);
    segment.values[index] = value;
    previousKey = key;
  }
  return segments;
}

/** Visual links contain only existing endpoints; they never create observations. */
export function wealthHistoryConnectors(points: WealthPoint[]) {
  const segments = wealthHistorySegments(points);
  const segmentAt = points.map((_, index) => segments.findIndex(segment => segment.values[index] !== null));
  const connectors: { from: number; to: number }[] = [];
  let previous: number | null = null;
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index];
    const day = point.date.slice(0, 10), timestamp = Date.parse(`${day}T00:00:00Z`);
    if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString().slice(0, 10) !== day) {
      previous = null;
      continue;
    }
    if (chartPointValue(point) === null) continue;
    if (previous !== null && segmentAt[previous] !== segmentAt[index]
      && point.coverage_key && point.coverage_key === points[previous].coverage_key
      && point.date > points[previous].date) connectors.push({ from: previous, to: index });
    previous = index;
  }
  return connectors;
}

/** ECharts takes the first series for its navigator; supply the whole dated history. */
export function wealthHistoryNavigator(points: WealthPoint[]) {
  return {
    id: "history-navigator", name: "History navigator", type: "line", silent: true,
    showSymbol: false, symbol: "none", connectNulls: false,
    lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 },
    emphasis: { disabled: true }, tooltip: { show: false },
    data: points.map(point => [Date.parse(`${point.date.slice(0, 10)}T00:00:00Z`), chartPointValue(point)]),
  };
}

export function wealthHistoryConnectorSeries(points: WealthPoint[], colour: string, timestamp?: (point: WealthPoint) => number | null) {
  return wealthHistoryConnectors(points).map(({ from, to }, index) => ({
    id: `history-link-${index}`, name: "History visual link", type: "line", silent: true,
    showSymbol: false, symbol: "none", connectNulls: false, preserveLinePattern: true,
    lineStyle: { color: colour, type: "dashed", opacity: 0.65 },
    emphasis: { disabled: true }, tooltip: { show: false }, z: 1,
    data: [from, to].map(index => [timestamp ? timestamp(points[index]) : Date.parse(`${points[index].date.slice(0, 10)}T00:00:00Z`), chartPointValue(points[index])]),
  }));
}

export function compactWealth(value: number, step = Math.max(Math.abs(value) / 1_000, 1)): string {
  const unit = Math.abs(value) >= 1_000_000 ? 1_000_000 : Math.abs(value) >= 1_000 ? 1_000 : 1;
  const precision = Math.min(6, Math.max(0, Math.ceil(Math.log10(unit / Math.max(step, 0.01)))));
  const amount = (value / unit).toLocaleString("en-GB", { maximumFractionDigits: precision });
  return `${amount}${unit === 1_000_000 ? "m" : unit === 1_000 ? "k" : ""}`;
}
