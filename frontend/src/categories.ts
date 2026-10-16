export const CATEGORIES = [
  "Income",
  "Housing",
  "Friends & shared costs",
  "Groceries",
  "Eating out",
  "Transport",
  "Shopping",
  "Entertainment",
  "Travel",
  "Home & personal care",
  "Bills & utilities",
  "Phone & internet",
  "Subscriptions",
  "Health & education",
  "Transfers & payments",
  "Cash",
  "Fees & interest",
  "Investments",
  "Uncategorized",
];

// The server catalog owns ordering and archiving. Retain a record's current
// category when archived so opening an editor never changes it implicitly.
export function categoryOptions(catalog?: readonly (string | null | undefined)[] | null, ...current: (readonly (string | null | undefined)[])[]): string[] {
  return [...new Set([...(catalog ?? CATEGORIES), ...current.flat()]
    .filter((value): value is string => typeof value === "string" && value.trim().length > 0))];
}
