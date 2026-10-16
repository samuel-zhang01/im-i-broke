import catalog from "./institutionCatalog.json";

const key = (value: string) => value.normalize("NFKC").toLocaleLowerCase("en").replace(/[^\p{L}\p{N}]/gu, "");
const entries = catalog.institutions.flatMap(institution => institution.aliases.map(alias => ({ alias: key(alias), institution })))
  .sort((a, b) => b.alias.length - a.alias.length);

/** Artwork is presentation only. Never use these aliases to link financial accounts. */
export function institutionBrand(name: string) {
  const normalized = key(name);
  if (!normalized) return undefined;
  const exact = entries.find(entry => entry.alias === normalized);
  if (exact) return exact.institution;
  // Providers often append a region or Personal/Business to the institution.
  // Avoid arbitrary substring matches (e.g. Public vs Republic or Public Bank).
  const decorated = entries.find(entry => entry.alias.length >= 3 && normalized.startsWith(entry.alias)
    && /^(?:(?:personal|business|uk|us|usa|gb|sg|ca|canada|australia|aus|europe))+$/u.test(normalized.slice(entry.alias.length)));
  return decorated?.institution;
}

export function institutionName(name: string) {
  return institutionBrand(name)?.name || name.trim() || "Other accounts";
}

export function institutionLogo(name: string) {
  const brand = institutionBrand(name);
  return brand ? `/logos/${brand.slug}.png` : undefined;
}

export function institutionInitials(name: string) {
  return Array.from(name.trim().split(/\s+/u).map(part => Array.from(part)[0] || "").join(""))
    .slice(0, 2).join("").toLocaleUpperCase() || "?";
}

export const institutionCatalog = catalog;
