import { trapDialogFocus } from "../../dialogFocus";
import { DialogSession } from "../../dialogSession";
import { Heading } from "../Heading";
import { useEffect, useRef, type ReactNode } from "react";

export default function StockDetailDialog({ title, onClose, children, closeLabel = "Close stock detail" }: { title: string; onClose: () => void; children: ReactNode; closeLabel?: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose); close.current = onClose;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const session = new DialogSession(element);
    return () => session.dispose();
  }, []);
  return <dialog ref={dialog} aria-label={title} onCancel={(event) => { event.preventDefault(); close.current(); }} onKeyDown={trapDialogFocus} onClick={(event) => { if (event.target === event.currentTarget) close.current(); }} className="m-auto max-h-[calc(100dvh-1.5rem)] w-[calc(100%-1.5rem)] max-w-3xl overflow-hidden rounded-xl border border-line bg-white p-0 text-foam shadow-xl backdrop:bg-abyss/45">
    <div className="flex max-h-[calc(100dvh-1.5rem)] flex-col"><header className="modal-header flex shrink-0 items-center justify-between gap-3 border-b border-line px-4 py-2 sm:px-5"><Heading level={2} variant="section" className="min-w-0">{title}</Heading><button type="button" autoFocus className="btn min-h-11 min-w-11 shrink-0 border-transparent px-2" aria-label={closeLabel} onClick={onClose}>×</button></header><div className="modal-body min-h-0 overflow-y-auto overscroll-contain p-4 sm:p-5">{children}</div></div>
  </dialog>;
}
