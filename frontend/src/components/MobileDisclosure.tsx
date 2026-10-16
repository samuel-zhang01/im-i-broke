import { useId, type ReactNode } from "react";
import { useCompactLayout } from "./ui";
import { useWorkspaceState } from "../workspaceState";

/** Preserve controls and their values while giving small screens room for results. */
export default function MobileDisclosure({ label, summary, children, className = "", contentClassName = "space-y-3" }: {
  label: string; summary?: string; children: ReactNode; className?: string; contentClassName?: string;
}) {
  const compact = useCompactLayout();
  const [open, setOpen] = useWorkspaceState(`disclosure:${label}`, false);
  const id = useId();
  const expanded = !compact || open;
  return <div className={`min-w-0 ${className}`} data-mobile-disclosure={label}>
    <button type="button" aria-expanded={expanded} aria-controls={id} aria-label={label} aria-describedby={summary ? `${id}-summary` : undefined}
      className="flex min-h-11 w-full items-center gap-2 rounded-lg border border-line bg-white px-3 py-2 text-left sm:hidden"
      onClick={() => setOpen(value => !value)}>
      <span className="min-w-0 flex-1"><span className="block text-sm font-medium">{label}</span>
        {summary && <span id={`${id}-summary`} className="block truncate text-xs text-mist" title={summary}>{summary}</span>}</span>
      <span aria-hidden="true" className="shrink-0 text-mist">{open ? "−" : "+"}</span>
    </button>
    <div id={id} className={`${expanded ? "mt-3 sm:mt-0" : "!hidden"} ${contentClassName}`}>{children}</div>
  </div>;
}
