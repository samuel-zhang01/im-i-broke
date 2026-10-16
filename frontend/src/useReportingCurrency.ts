import { useEffect } from "react";
import { fetchWorkspacePreferences } from "./productApi";
import { hasWorkspaceValue, workspaceGeneration } from "./workspaceStore";

/** A workspace default never overwrites an explicitly chosen view currency. */
export function useReportingCurrency(key: string, apply: (currency: string) => void) {
  useEffect(() => {
    if (hasWorkspaceValue(key)) return;
    const controller = new AbortController();
    const generation = workspaceGeneration();
    void fetchWorkspacePreferences(controller.signal).then(preferences => {
      const currency = preferences?.reporting_currency;
      if (typeof currency !== "string" || !/^[A-Z]{3}$/.test(currency)) return;
      if (!controller.signal.aborted && generation === workspaceGeneration() && !hasWorkspaceValue(key)) apply(currency);
    }).catch(() => { /* The existing GBP default remains usable during recovery. */ });
    return () => controller.abort();
  }, [key]);
}
