import { trapDialogFocus } from "../dialogFocus";
import { DialogSession } from "../dialogSession";
import { Heading } from "./Heading";
import { forwardRef, useCallback, useEffect, useRef, useState, type HTMLAttributes, type ReactNode } from "react";
import { watchData } from "../dataEvents";
import { LoadingRegion, SkeletonBlock } from "./Skeleton";

// Retain the last successful result while fetching; stale requests cannot win.
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[] = [], options: { enabled?: boolean } = {}) {
  const enabled = options.enabled !== false;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const refetch = useCallback(() => setNonce((n) => n + 1), []);
  useEffect(() => watchData(refetch), [refetch]);

  useEffect(() => {
    const controller = new AbortController();
    if (!enabled) { setLoading(false); return () => controller.abort(); }
    setLoading(true);
    setError(null);
    // StrictMode's discarded mount starts no network request.
    queueMicrotask(() => {
      if (controller.signal.aborted) return;
      fn(controller.signal)
        .then((d) => { if (!controller.signal.aborted) setData(d); })
        .catch((e) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  return { data, loading, error, refetch };
}

export function DeferredSection({ children, label = "More insights", minHeight = 180 }: { children: ReactNode; label?: string; minHeight?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (visible || !ref.current) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "240px" });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [visible]);
  return <div ref={ref} style={{ minHeight }}>{visible ? children : <div className="card flex items-center justify-between gap-4 p-5" style={{ minHeight }}><p className="text-sm text-mist">{label}</p><button className="btn" onClick={() => setVisible(true)}>Load section</button></div>}</div>;
}

export function useCompactLayout(query = "(max-width: 639px)") {
  const [compact, setCompact] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setCompact(media.matches);
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return compact;
}
export function PageControls({ total, page, size, onPage, label = "List pages" }: { total: number; page: number; size: number; onPage: (page: number) => void; label?: string }) {
  if (!total) return null;
  return <nav aria-label={label} className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-3"><p className="text-xs text-mist">{page * size + 1}–{Math.min((page + 1) * size, total)} of {total.toLocaleString()}</p>{total > size && <div className="flex gap-2"><button className="btn" disabled={page === 0} onClick={() => onPage(page - 1)}>← Previous page</button><button className="btn" disabled={(page + 1) * size >= total} onClick={() => onPage(page + 1)}>Next page →</button></div>}</nav>;
}

export const Card = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function Card({
  children,
  className = "",
  ...props
}, ref) {
  return <div {...props} ref={ref} className={`card card-padded ${className}`}>{children}</div>;
});

export function SectionTitle({
  children,
  hint,
}: {
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <Heading level={2} variant="section" className="text-foam">{children}</Heading>
      {hint && <span className="text-xs text-mist">{hint}</span>}
    </div>
  );
}

export function Pill({
  children,
  tone = "teal",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return <span className={`pill ${TONES[tone]}`}>{children}</span>;
}

type Tone = "teal" | "cyan" | "blue" | "kelp" | "coral" | "amber" | "mute";
const TONES: Record<Tone, string> = {
  teal: "bg-teal/15 text-teal",
  cyan: "bg-cyan/15 text-cyan",
  blue: "bg-seablue/15 text-seablue",
  kelp: "bg-kelp/15 text-kelp",
  coral: "bg-coral/15 text-coral",
  amber: "bg-amber/15 text-amber",
  mute: "bg-mist/10 text-mist",
};

export function Spinner({ label = "Loading…", minHeight = 160 }: { label?: string; minHeight?: number }) {
  return (
    <LoadingRegion label={label} className="flex items-center justify-center px-5 py-6" style={{ minHeight }}>
      <div className="w-56 max-w-full space-y-3"><SkeletonBlock className="h-4 w-3/4" /><SkeletonBlock className="h-3 w-full" /><SkeletonBlock className="h-3 w-5/6" /><p className="text-xs text-mist">{label}</p></div>
    </LoadingRegion>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="py-4 text-center text-sm text-mist">{children}</div>
  );
}

export function ErrorBox({
  error,
  retry,
}: {
  error: string;
  retry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="rounded-xl border border-coral/40 bg-coral/10 p-4 text-sm text-coral [overflow-wrap:anywhere]"
    >
      <p>{error}</p>
      {retry && (
        <button type="button" className="btn mt-3" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  );
}

// Wraps the common loading/error/empty pattern.
export function Async<T>({
  state,
  children,
  empty,
  fallback,
}: {
  state: {
    data: T | null;
    loading: boolean;
    error: string | null;
    refetch?: () => void;
  };
  children: (data: T) => ReactNode;
  empty?: ReactNode;
  fallback?: ReactNode;
}) {
  if (state.loading && state.data === null) return fallback ?? <Spinner />;
  if (state.error && state.data === null)
    return <ErrorBox error={state.error} retry={state.refetch} />;
  return <div className="async-content content-enter" aria-busy={state.loading}>
    <RefreshStatus loading={state.loading} error={state.error} retry={state.refetch} />
    <div className="space-y-4">
    {state.data === null || (Array.isArray(state.data) && state.data.length === 0)
      ? empty ?? <Empty>Nothing here yet.</Empty>
      : children(state.data)}
    </div>
  </div>;
}

export function RefreshStatus({ loading, error, retry, retaining = true }: {
  loading: boolean; error?: string | null; retry?: () => void; retaining?: boolean;
}) {
  if (error) return <div className="mb-3 space-y-2">{retaining && <p className="text-xs text-mist">Couldn’t update this view. Previous results remain visible.</p>}<ErrorBox error={error} retry={retry} /></div>;
  if (!loading) return null;
  return <p role="status" className="sr-only">Updating results…</p>;
}

export { CATEGORIES } from "../categories";

/** Form actions stay outside the scrollable body and retain native validation. */
export function DialogActions({ formId, onCancel, label, busy = false, disabled = false, children }: {
  formId: string; onCancel: () => void; label: string; busy?: boolean; disabled?: boolean; children?: ReactNode;
}) {
  return <div className="flex flex-wrap items-center justify-between gap-3">
    {children}
    <div className="flex w-full flex-wrap gap-2 sm:ml-auto sm:w-auto [&>button]:flex-1 sm:[&>button]:flex-none">
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
      <button type="submit" form={formId} className="btn-primary" disabled={busy || disabled}>{label}</button>
    </div>
  </div>;
}

export function Metric({
  label,
  value,
  detail,
  explanation,
  tone = "text-foam",
}: {
  label: string;
  value: string;
  detail?: string;
  explanation: string;
  tone?: string;
}) {
  return (
    <Card className="flex min-w-0 flex-col">
      <p className="metric-label">{label}</p>
      <p
        className={`mt-1 break-words text-[clamp(1.4rem,2.4vw,1.875rem)] font-semibold leading-tight tracking-tight tabular-nums ${tone}`}
      >
        {value}
      </p>
      {detail && (
        <p className="mt-2 text-xs leading-relaxed text-mist">{detail}</p>
      )}
      <details className="mt-auto pt-2 text-xs leading-relaxed text-mist">
        <summary className="cursor-pointer underline decoration-line underline-offset-4">
          What this means
        </summary>
        <p className="mt-2 border-t border-line pt-2">{explanation}</p>
      </details>
    </Card>
  );
}

export function Modal({
  title,
  children,
  onClose,
  footer,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const session = new DialogSession(ref.current);
    return () => session.dispose();
  }, []);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-abyss/45 p-3 sm:p-6"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className="modal-panel flex max-h-[calc(100dvh-1.5rem)] w-full min-w-0 max-w-2xl flex-col overflow-hidden rounded-xl border border-line bg-white shadow-xl outline-hidden sm:max-h-[calc(100dvh-3rem)]"
        onKeyDown={(e) => {
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
          trapDialogFocus(e);
        }}
      >
        <div className="modal-header flex shrink-0 items-center justify-between gap-3 border-b border-line px-4 py-2 sm:px-5">
          <Heading level={2} variant="section" className="min-w-0 break-words">{title}</Heading>
          <button
            type="button"
            aria-label="Close dialog"
            className="btn min-h-11 min-w-11 shrink-0 border-transparent px-2"
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <div className="modal-body min-h-0 min-w-0 overflow-y-auto overscroll-contain p-4 [overflow-wrap:anywhere] sm:p-5">{children}</div>
        {footer && <div className="modal-footer shrink-0 border-t border-line bg-white px-4 py-3 sm:px-5">{footer}</div>}
      </div>
    </div>
  );
}
