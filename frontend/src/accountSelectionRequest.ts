import { authRequest } from "./authRequest";

/** Keep normal reads unchanged; large selections must also work behind proxies. */
export function accountSelectionRequest(path: string, filters: URLSearchParams, accountIds: string[], signal?: AbortSignal): Promise<Response> {
  const query = new URLSearchParams(filters);
  for (const id of accountIds.length ? accountIds : ["__none__"]) query.append("account", id);
  const url = `${path}?${query}`;
  if (url.length <= 4000) return authRequest(url, { signal });
  return authRequest(`${path}?${filters}`, {
    method: "POST", signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account: accountIds }),
  });
}
