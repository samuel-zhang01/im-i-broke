import { useState } from "react";
import { institutionCatalog } from "../institutions";
import InstitutionLogo from "./InstitutionLogo";

export default function InstitutionDirectory() {
  const [query, setQuery] = useState("");
  const brokerages = institutionCatalog.institutions.filter(row => row.providers.includes("snaptrade"));
  const matches = brokerages.filter(row => [row.name, ...row.aliases].some(name => name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  return <details className="rounded-xl border border-line px-4">
    <summary className="min-h-11 cursor-pointer py-3 text-sm font-medium">Browse supported brokerages</summary>
    <div className="space-y-3 pb-4">
      <p className="text-xs leading-relaxed text-mist">Connect any brokerage available to you through SnapTrade. Account types, region and provider access determine which balances, holdings and history are available. New institutions and accounts appear when your connection returns them.</p>
      <label className="block text-sm">Find a brokerage<input className="input mt-1 w-full" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search institution name" /></label>
      <p className="text-xs text-mist">{matches.length} of {brokerages.length} published integrations · catalog checked {institutionCatalog.checked_on}</p>
      <ul aria-label="Supported brokerage directory" className="grid max-h-80 gap-2 overflow-y-auto sm:grid-cols-2">
        {matches.map(row => <li key={row.slug}><a className="flex min-h-14 items-center gap-3 rounded-lg border border-line p-2 text-sm hover:bg-surface2" href={row.source} target="_blank" rel="noopener noreferrer"><InstitutionLogo name={row.name} /><span className="min-w-0 break-words">{row.name}<span className="block text-xs text-mist">Connection details ↗</span></span></a></li>)}
      </ul>
      {!matches.length && <p className="text-sm text-mist">No catalog match. Check your provider for newly added institutions; an unfamiliar name does not prevent accounts from appearing.</p>}
      <p className="text-xs text-mist">For bank accounts, use Lunch Flow’s institution picker. For other sources, add a manual account or import a CSV.</p>
    </div>
  </details>;
}
