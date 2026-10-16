const gbp = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  maximumFractionDigits: 2,
});

export function money(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return gbp.format(n);
}

// Compact for axis labels: £1.2k, £3.4M
export function moneyShort(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e6) return `${sign}£${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}£${(abs / 1e3).toFixed(1)}k`;
  return `${sign}£${abs.toFixed(0)}`;
}

export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  // Statement dates are calendar dates, not UTC instants. Preserve the date in
  // every browser timezone (new Date("YYYY-MM-DD") can display the prior day).
  const d = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(`${iso}T12:00:00`) : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "2-digit" });
}
