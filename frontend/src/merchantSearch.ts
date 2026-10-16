import { RE2JS } from "re2js";

export type MerchantSearchMode = "fuzzy" | "text" | "regex";
type SearchableMerchant = { canonical_name: string; aliases: readonly { pattern: string }[] };
export interface MerchantSearchResult<T> { items: T[]; error: string | null }

function normalize(value: string): string {
  return value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// Adjacent transpositions are common typing mistakes. This bounded-distance
// comparison only ranks search results; it never creates a matching rule.
function distance(left: string, right: string, limit: number): number {
  // Only one or two edits can match. A narrow diagonal band avoids allocating
  // a quadratic matrix for long imported labels or pasted search terms. Keep
  // three rows because adjacent transpositions read two rows back.
  const beyond = limit + 1;
  let previousPrevious = new Uint32Array(right.length + 1).fill(beyond);
  let previous = new Uint32Array(right.length + 1).fill(beyond);
  let current = new Uint32Array(right.length + 1).fill(beyond);
  for (let j = 0; j <= Math.min(right.length, limit); j++) previous[j] = j;
  for (let i = 1; i <= left.length; i++) {
    current[0] = i <= limit ? i : beyond;
    const start = Math.max(1, i - limit), end = Math.min(right.length, i + limit);
    if (start > 1) current[start - 1] = beyond;
    if (end < right.length) current[end + 1] = beyond;
    for (let j = start; j <= end; j++) {
      current[j] = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left[i - 1] === right[j - 1] ? 0 : 1));
      if (i > 1 && j > 1 && left[i - 1] === right[j - 2] && left[i - 2] === right[j - 1]) {
        current[j] = Math.min(current[j], previousPrevious[j - 2] + 1);
      }
    }
    [previousPrevious, previous, current] = [previous, current, previousPrevious];
  }
  return previous[right.length];
}

function tokenScore(query: string, candidate: string): number {
  if (query === candidate) return 100;
  if (/\d/.test(query + candidate)) return 0;
  if (query.length >= 3 && candidate.startsWith(query)) return 90;
  if (Math.min(query.length, candidate.length) < 4) return 0;
  const allowed = Math.min(query.length, candidate.length) >= 8 ? 2 : 1;
  if (Math.abs(query.length - candidate.length) > allowed) return 0;
  const edits = distance(query, candidate, allowed);
  return edits <= allowed ? 80 - edits * 10 : 0;
}

type PreparedQuery = { normalized: string; compact: string; tokens: string[] };
function fieldScore(query: PreparedQuery, field: string): number {
  if (field === query.normalized) return 1000;
  if (field.includes(query.normalized)) return 850 + (field.startsWith(query.normalized) ? 20 : 0);
  // Punctuation in acronyms and short shop names can be present in only one
  // statement, e.g. A.M.E.X and AMEX. This remains a search convenience.
  if (field.replace(/ /g, "") === query.compact) return 800;
  const remaining = field.split(" ");
  let total = 0;
  for (const token of query.tokens) {
    let best = 0, selected = -1;
    for (let index = 0; index < remaining.length; index++) {
      const score = tokenScore(token, remaining[index]);
      if (score > best) { best = score; selected = index; }
    }
    if (selected < 0) return 0;
    total += best;
    remaining.splice(selected, 1);
  }
  return 400 + total / query.tokens.length - Math.min(remaining.length, 10);
}

/** Search individual saved names and aliases without changing their rules. */
export function searchMerchantResult<T extends SearchableMerchant>(items: readonly T[], query: string, mode: MerchantSearchMode = "fuzzy"): MerchantSearchResult<T> {
  if (!query.trim()) return { items: [...items], error: null };
  if (mode === "regex") {
    if (query.length > 128) return { items: [], error: "Keep search expressions to 128 characters or fewer." };
    try {
      // RE2 uses linear-time matching, so patterns such as (a+)+$ cannot freeze
      // the interface. Search each original label, never a joined alias string.
      const expression = RE2JS.compile(query, RE2JS.CASE_INSENSITIVE);
      return { items: items.filter(item => [item.canonical_name, ...item.aliases.map(alias => alias.pattern)].some(value => expression.matcher(value).find())), error: null };
    } catch {
      return { items: [], error: "Invalid or unsupported search expression. Check the brackets and escaping; lookarounds and backreferences are not supported." };
    }
  }
  const normalized = normalize(query);
  if (!normalized) return { items: [], error: null };
  if (mode === "text") {
    return { items: items.filter(item => [item.canonical_name, ...item.aliases.map(alias => alias.pattern)].some(value => normalize(value).includes(normalized))), error: null };
  }
  const prepared = { normalized, compact: normalized.replace(/ /g, ""), tokens: normalized.split(" ").sort((a, b) => b.length - a.length) };
  const ranked = items.map((item, index) => ({
    item, index,
    score: Math.max(fieldScore(prepared, normalize(item.canonical_name)), ...item.aliases.map(alias => {
      const score = fieldScore(prepared, normalize(alias.pattern));
      return score ? score - 10 : 0;
    })),
  }));
  return { items: ranked.filter(result => result.score > 0).sort((a, b) => b.score - a.score || a.index - b.index).map(result => result.item), error: null };
}

export function searchMerchants<T extends SearchableMerchant>(items: readonly T[], query: string): T[] {
  return searchMerchantResult(items, query, "fuzzy").items;
}
