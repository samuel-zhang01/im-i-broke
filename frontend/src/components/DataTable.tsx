import type { HTMLAttributes, TableHTMLAttributes } from "react";

type DataTableProps = TableHTMLAttributes<HTMLTableElement> & {
  density?: "comfortable" | "compact";
};

/** Shared table presentation; callers retain their native headings, sorting and cells. */
export function DataTable({ density = "comfortable", className = "", ...props }: DataTableProps) {
  return <table {...props} data-density={density} className={`data-table ${className}`} />;
}

/** A named, keyboard-scrollable viewport for wide tables. */
export function TableRegion({ label, className = "", ...props }: HTMLAttributes<HTMLDivElement> & { label: string }) {
  return <div {...props} role="region" aria-label={label} tabIndex={0} className={`data-table-region min-w-0 overflow-x-auto rounded-lg border border-line ${className}`} />;
}

/** Secondary cell content has one shared line height and gap across record tables. */
export function TableMeta({ tone = "muted", className = "", ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: "muted" | "inherit" | "warning" }) {
  const color = tone === "warning" ? "text-amber" : tone === "muted" ? "text-mist" : "";
  return <span {...props} className={`mt-0.5 block text-xs font-normal leading-4 ${color} ${className}`} />;
}
