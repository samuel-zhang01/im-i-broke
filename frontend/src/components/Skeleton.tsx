import type { CSSProperties, ReactNode } from "react";

/** Loading placeholders carry no sample financial values or focusable controls. */
export function SkeletonBlock({ className = "", style }: { className?: string; style?: CSSProperties }) {
  return <div aria-hidden="true" className={`skeleton-block rounded-md ${className}`} style={style} />;
}

export function LoadingRegion({ label = "Loading…", children, className = "", style }: {
  label?: string; children: ReactNode; className?: string; style?: CSSProperties;
}) {
  return <div role="status" aria-label={label} aria-busy="true" className={className} style={style}>
    <span className="sr-only">{label}</span><div aria-hidden="true" className="min-w-0 max-w-full">{children}</div>
  </div>;
}

export function ChartSkeleton({ label = "Loading chart…", height = 320, className = "" }: { label?: string; height?: number | null; className?: string }) {
  return <LoadingRegion label={label} className={`min-w-0 ${className}`}>
    <div style={height === null ? undefined : { height }} className={`flex items-end gap-3 py-4 ${height === null ? "h-[260px] sm:h-[320px]" : ""}`}>
      <div className="flex h-full w-9 shrink-0 flex-col justify-between py-2">{[0, 1, 2, 3].map(i => <SkeletonBlock key={i} className="h-2 w-7" />)}</div>
      <div className="flex h-full min-w-0 flex-1 flex-col justify-between border-b border-l border-line/60 px-3 py-2">{[0, 1, 2, 3].map(i => <div key={i} className="h-px bg-line/40" />)}</div>
    </div>
  </LoadingRegion>;
}

/** Matches the chart, inspection slider, zoom controls and source disclosure. */
export function AccountHistorySkeleton({ label = "Loading account value history…" }: { label?: string }) {
  return <LoadingRegion label={label} className="px-3 pb-1 sm:px-5">
    <SkeletonBlock className="mt-2 h-9 w-52" />
    <SkeletonBlock className="mt-2 h-4 w-32" />
    <ChartSkeleton height={null} />
    <SkeletonBlock className="h-[130px] w-full" />
    <SkeletonBlock className="mt-2 h-11 w-36" />
    <SkeletonBlock className="mt-3 h-11 w-40" />
  </LoadingRegion>;
}

export function TableSkeleton({ label = "Loading records…", rows = 5, className = "", density = "comfortable" }: { label?: string; rows?: number; className?: string; density?: "comfortable" | "compact" }) {
  return <LoadingRegion label={label} className={`overflow-hidden rounded-xl border border-line bg-white ${className}`}>
    <div className="flex h-11 items-center gap-6 border-b border-line bg-surface2 px-4"><SkeletonBlock className="w-32" /><SkeletonBlock className="ml-auto w-20" /></div>
    {Array.from({ length: rows }, (_, i) => <div key={i} className={`flex items-center gap-6 border-b border-line/60 px-4 last:border-0 ${density === "compact" ? "min-h-16 sm:min-h-12" : "min-h-[60px]"}`}><div className="min-w-0 flex-1 space-y-2"><SkeletonBlock className="w-2/3 max-w-60" /><SkeletonBlock className="h-2 w-1/3 max-w-32" /></div><SkeletonBlock className="w-20 shrink-0" /></div>)}
  </LoadingRegion>;
}

export function PortfolioSummarySkeleton() {
  return <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4 rounded-xl border border-line bg-white px-4 py-3 sm:px-5 sm:py-4">
    <div className="w-60 max-w-full"><div className="flex flex-wrap items-center gap-3"><SkeletonBlock className="h-4 w-32" /><SkeletonBlock className="h-4 w-20 [@media(pointer:coarse)]:h-11" /></div><SkeletonBlock className="mt-2 h-10 w-44" /><SkeletonBlock className="mt-1 h-4 w-40" /></div>
    <div className="grid min-w-0 grid-cols-2 gap-x-5 gap-y-3 sm:gap-x-8">{[0, 1].map(i => <div key={i} className="w-32 max-w-full"><SkeletonBlock className="h-4 w-full" /><SkeletonBlock className="mt-1 h-6 w-28 sm:h-7" /><SkeletonBlock className="h-11 w-24" /></div>)}</div>
  </div>;
}

type PageSkeletonProps = { label?: string; variant?: "dashboard" | "table" | "banking" | "portfolio" | "networth" };
export function PageSkeleton(props: PageSkeletonProps) {
  return <LoadingRegion label={props.label || "Loading page…"}><PageSkeletonLayout {...props} /></LoadingRegion>;
}
function PageSkeletonLayout({ label = "Loading page…", variant = "dashboard" }: PageSkeletonProps) {
  if (variant === "networth") return <div className="space-y-4">
    <div className="flex min-h-[220px] flex-wrap items-center justify-between gap-6 rounded-xl border border-line bg-white p-4 sm:min-h-[170px] sm:p-5"><div className="w-64 max-w-full space-y-3"><SkeletonBlock className="w-44" /><SkeletonBlock className="h-12 w-56 max-w-full" /><SkeletonBlock className="h-3 w-full" /></div><div className="grid grid-cols-2 gap-5">{[0, 1].map(i => <div key={i} className="space-y-3"><SkeletonBlock className="w-24" /><SkeletonBlock className="h-6 w-28" /></div>)}</div></div>
    <div className="flex min-h-11 gap-2"><SkeletonBlock className="h-11 w-44" /><SkeletonBlock className="h-11 w-24" /></div>
    <div className="min-h-[480px] rounded-xl border border-line bg-white p-4"><div className="flex flex-wrap justify-between gap-4"><div className="space-y-2"><SkeletonBlock className="h-5 w-44" /><SkeletonBlock className="h-3 w-52" /></div><SkeletonBlock className="h-9 w-60 max-w-full" /></div><ChartSkeleton label={label} height={380} /></div>
    <TableSkeleton label={label} rows={10} density="compact" />
  </div>;
  if (variant === "table") return <div className="space-y-4"><LoadingRegion label={label} className="rounded-xl border border-line bg-white p-4"><SkeletonBlock className="h-9 w-full" /></LoadingRegion><TableSkeleton rows={7} label={label} /></div>;
  if (variant === "banking") return <LoadingRegion label={label} className="space-y-4">
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3"><SkeletonBlock className="h-5 w-24" /><SkeletonBlock className="h-[38px] w-full sm:w-80" /></div>
      <div className="rounded-xl border border-line bg-white p-4"><SkeletonBlock className="h-5 w-60 max-w-full" /><div className="mt-3 space-y-5 rounded-xl bg-surface2 p-4"><SkeletonBlock className="h-4 w-full" /><SkeletonBlock className="h-8 w-44" /><SkeletonBlock className="h-11 w-64 max-w-full" /></div><SkeletonBlock className="mt-2 h-9 w-52 max-w-full" /></div>
      <div className="grid gap-4 xl:grid-cols-2">{[0, 1].map(i => <div key={i} className="space-y-4 rounded-xl border border-line bg-white p-4"><SkeletonBlock className="h-6 w-36" />{[0, 1].map(j => <div key={j} className="flex h-16 items-center justify-between gap-4 border-t border-line pt-3"><SkeletonBlock className="w-1/2" /><SkeletonBlock className="w-20" /></div>)}</div>)}</div>
    </div>
  </LoadingRegion>;
  if (variant === "portfolio") return <div className="space-y-4">
    <LoadingRegion label={label}><PortfolioSummarySkeleton /></LoadingRegion>
    <div className="overflow-hidden rounded-xl border border-line bg-white">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 pt-4 sm:px-5"><div className="max-w-full"><SkeletonBlock className="h-5 w-52 max-w-full" /><SkeletonBlock className="mt-1 h-4 w-60 max-w-full" /></div><SkeletonBlock className="h-11 w-36" /></div>
      <AccountHistorySkeleton label={label} />
    </div>
    <TableSkeleton label={label} rows={4} />
  </div>;
  return <div className="space-y-4">
    <LoadingRegion label={label}><div className="grid grid-cols-2 gap-3 xl:grid-cols-4">{[0, 1, 2, 3].map(i => <div key={i} className="space-y-4 rounded-xl border border-line bg-white p-4"><SkeletonBlock className="w-2/3" /><SkeletonBlock className="h-8 w-3/4" /><SkeletonBlock className="h-3 w-full" /></div>)}</div></LoadingRegion>
    <div className="rounded-xl border border-line bg-white p-4"><SkeletonBlock className="h-5 w-40" /><ChartSkeleton label={label} /></div>
    <TableSkeleton label={label} rows={4} />
  </div>;
}
