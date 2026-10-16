/** Stable category identity across banking charts, lists and filtered views. */
const NAMED: Record<string, string> = {
  'eating out': '#AF4321', 'groceries': '#277253', 'transport': '#477486',
  'shopping': '#887393', 'subscriptions': '#536FA3', 'entertainment': '#96631B',
  'bills & utilities': '#607B92', 'rent & housing': '#7D654C',
  'health & education': '#307C77', 'fees & interest': '#B54747',
  'friends & shared costs': '#9C6476', 'uncategorized': '#737A73',
  'other': '#8C938C',
};
const FALLBACK = ['#AF4321', '#277253', '#477486', '#887393', '#536FA3', '#96631B', '#607B92', '#9C6476'];
export function categoryColor(category: string): string {
  const key = category.trim().toLowerCase();
  if (NAMED[key]) return NAMED[key];
  const hash = [...key].reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0);
  return FALLBACK[hash % FALLBACK.length];
}
