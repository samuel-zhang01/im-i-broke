// A review or rule edit can remove every result from the current last page.
export function validPageOffset(total: number, limit: number, offset: number): number {
  const last = total > 0 ? Math.floor((total - 1) / limit) * limit : 0;
  return Math.min(offset, last);
}
