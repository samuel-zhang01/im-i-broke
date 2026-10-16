import { useState } from "react";
import type { ConnectedAccount } from "../../snaptradeApi";
import { accountLabel, portfolioDate, type AccountSectionName } from "../../connectedPortfolio";
import { Pill } from "../ui";

export function SectionWarnings<K extends AccountSectionName>({ accounts, section, label }: { accounts: Pick<ConnectedAccount, "id" | "name" | "institution" | "number_suffix" | K>[]; section: K; label: string }) {
  const issues = accounts.filter((account) => ["error", "unsupported"].includes(account[section].status) || account[section].truncated || account[section].stale);
  if (!issues.length) return null;
  return <div role="status" className="space-y-2 rounded-lg border border-amber/30 bg-amber/5 p-3 text-sm text-amber">
    {issues.map((account) => <p key={account.id}><strong>{accountLabel(account)}: </strong>
      {account[section].status === "unsupported" ? `${label} are not available from this connection.`
        : account[section].status === "error" ? account[section].error || `${label} could not be retrieved.`
        : account[section].truncated ? `Only part of the available ${label.toLowerCase()} is shown.` : `${label} may be out of date.`}
      {account[section].stale && ` Saved ${label.toLowerCase()} from ${portfolioDate(account[section].cached_at ?? null, true)} remain visible.`}
    </p>)}
  </div>;
}

export function StatusPill({ value }: { value: string | null }) {
  const normalized = value?.toLowerCase() ?? "";
  const tone = /cancel|reject|fail|expired/.test(normalized) ? "coral"
    : /fill|complete|execut/.test(normalized) && !/partial/.test(normalized) ? "kelp"
    : /open|pending|partial|new/.test(normalized) ? "amber" : "mute";
  return <Pill tone={tone}>{value?.replace(/_/g, " ").toLowerCase() || "Unknown"}</Pill>;
}

export { TableRegion } from "../DataTable";

export function usePage<T>(rows: T[], resetKey: string) {
  const [state, setState] = useState({ key: resetKey, page: 1 });
  const pages = Math.max(1, Math.ceil(rows.length / 25));
  const page = state.key === resetKey ? Math.min(state.page, pages) : 1;
  return {
    rows: rows.slice((page - 1) * 25, page * 25), page, pages,
    controls: rows.length > 25 ? <div className="flex flex-wrap items-center justify-between gap-3 pt-3 text-xs text-mist">
      <span>{(page - 1) * 25 + 1}–{Math.min(page * 25, rows.length)} of {rows.length}</span>
      <div className="flex items-center gap-3"><button className="btn" disabled={page === 1} onClick={() => setState({ key: resetKey, page: page - 1 })}>Previous</button><span>Page {page} of {pages}</span><button className="btn" disabled={page === pages} onClick={() => setState({ key: resetKey, page: page + 1 })}>Next</button></div>
    </div> : null,
  };
}
