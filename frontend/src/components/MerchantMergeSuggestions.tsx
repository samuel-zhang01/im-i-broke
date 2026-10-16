import { Heading } from "./Heading";
import { useState } from "react";
import type { MerchantRule } from "../api";
import { fetchMerchantMergeSuggestions, type MerchantMergeSuggestion } from "../merchantMerge";
import { Async, Card, useAsync } from "./ui";

export default function MerchantMergeSuggestions({ rules, visibleIds, onChoose }: {
  rules: MerchantRule[];
  visibleIds?: number[];
  onChoose: (suggestion: MerchantMergeSuggestion) => void;
}) {
  const suggestions = useAsync(signal => fetchMerchantMergeSuggestions(signal));
  const [limit, setLimit] = useState(3);
  return <Card aria-label="Possible duplicate saved names">
    <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2"><Heading level={3} variant="subsection">Possible duplicates{suggestions.data ? ` (${suggestions.data.total})` : ""}</Heading><span className="text-xs text-mist">Confirm before combining</span></div>
    <p className="mb-4 text-sm text-mist">Similar names and shared aliases can reveal duplicate entries. Review a suggested name to keep, then combine all their matching rules.</p>
    <Async state={suggestions}>{data => {
      const available = new Map(rules.map(rule => [rule.id, rule]));
      const visible = visibleIds ? new Set(visibleIds) : null;
      const items = data.items.filter(item => available.has(item.target_id) && available.has(item.source_id) && (!visible || visible.has(item.target_id) || visible.has(item.source_id)));
      return <>
        {visible && <p className="mb-3 text-xs text-mist">{items.length} suggested pairs match the current directory filters.</p>}
        {items.length === 0 ? <p className="text-sm text-mist">No compatible duplicates found{visible ? " for these filters" : ""}. Use Merge beside any saved name to search for another match.</p> : <div className="divide-y divide-line">{items.slice(0, limit).map(item => {
          const source = available.get(item.source_id)!, target = available.get(item.target_id)!;
          return <div key={`${item.target_id}:${item.source_id}`} className="flex flex-wrap items-start justify-between gap-3 py-4 first:pt-0 last:pb-0">
            <div className="min-w-0 flex-1 basis-64"><p className="break-words text-sm font-semibold">{source.canonical_name} <span className="font-normal text-mist">→</span> {target.canonical_name}</p><p className="mt-1 text-xs text-mist">{item.reason}</p><p className="mt-1 text-xs text-mist">Suggested name to keep: {target.canonical_name} · {target.category ?? "Existing category"}</p><p className="mt-2 break-words text-xs text-mist">{[...source.aliases.slice(0, 1), ...target.aliases.slice(0, 1)].map(alias => alias.pattern).join(" · ")}</p></div>
            <button className="btn shrink-0" onClick={() => onChoose(item)} aria-label={`Review merge ${source.canonical_name} into ${target.canonical_name}`}>Review merge</button>
          </div>;
        })}</div>}
        {items.length > limit && <button className="btn mt-4" onClick={() => setLimit(current => current + 12)}>Show more suggestions ({items.length - limit} remaining)</button>}
        {data.total > data.items.length && <p className="mt-3 text-xs text-mist">Showing suggestions from the closest {data.items.length} of {data.total} possible pairs. Merge a pair to refresh the list, or use Merge beside a saved name to find another match.</p>}
      </>;
    }}</Async>
  </Card>;
}
