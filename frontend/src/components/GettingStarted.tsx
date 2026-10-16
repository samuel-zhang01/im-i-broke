import { useEffect, useRef } from "react";
import { Heading } from "./Heading";
import Icon from "./Icon";
import { fetchWorkspacePreferences } from "../productApi";
import { useAsync } from "./ui";

export type SetupSection = "preferences" | "accounts" | "connections" | "imports" | "reports";

export default function GettingStarted({ onChoose, onDismiss }: { onChoose: (section: SetupSection) => void; onDismiss: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  const preferences = useAsync(fetchWorkspacePreferences);
  return <section aria-labelledby="getting-started-title" className="overflow-hidden rounded-2xl border border-line bg-white">
    <div className="border-b border-line bg-surface2/60 p-5 sm:p-6">
      <p className="eyebrow mb-2">A little setup. A clearer picture.</p>
      <Heading ref={heading} tabIndex={-1} className="outline-none" id="getting-started-title" level={2} variant="intro">Welcome to your workspace</Heading>
      <p className="mt-2 max-w-xl text-sm leading-relaxed text-mist">Start with one account. Add connections or import older transactions when you’re ready.</p>
      <button className="mt-3 min-h-11 text-left text-sm font-medium text-teal underline decoration-teal/30 underline-offset-4" onClick={() => onChoose("preferences")}>{preferences.data?.reporting_currency ? `Reporting in ${preferences.data.reporting_currency} · Change preferences` : "Choose your currency and preferences"}</button>
    </div>
    <div className="space-y-3 p-5 sm:p-6">
      <p className="text-xs font-semibold uppercase tracking-wider text-mist">How would you like to start?</p>
      <button className="group flex w-full items-center gap-4 rounded-xl border border-line p-4 text-left transition hover:border-teal hover:bg-surface2/50" onClick={() => onChoose("connections")}>
        <span aria-hidden="true" className="rounded-lg bg-surface2 p-3 text-teal"><Icon name="bank" /></span>
        <span className="min-w-0 flex-1"><span className="block text-sm font-semibold">Connect a bank or brokerage</span><span className="mt-1 block text-sm leading-relaxed text-mist">See the connection options available for this installation.</span></span><span aria-hidden="true" className="text-teal">→</span>
      </button>
      <button className="group flex w-full items-center gap-4 rounded-xl border border-line p-4 text-left transition hover:border-teal hover:bg-surface2/50" onClick={() => onChoose("accounts")}>
        <span aria-hidden="true" className="rounded-lg bg-surface2 p-3 text-teal"><Icon name="plus" /></span>
        <span className="min-w-0 flex-1"><span className="block text-sm font-semibold">Add an account yourself</span><span className="mt-1 block text-sm leading-relaxed text-mist">Start with a balance, then import a CSV if you have one.</span></span><span aria-hidden="true" className="text-teal">→</span>
      </button>
      <div className="flex flex-wrap gap-x-5 gap-y-1 pt-1">
        <button className="min-h-11 text-sm font-medium text-teal underline underline-offset-4" onClick={() => onChoose("imports")}>Import a CSV</button>
        <button className="min-h-11 text-sm font-medium text-teal underline underline-offset-4" onClick={() => onChoose("reports")}>Open workspace reports</button>
      </div>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-5 py-3 sm:px-6">
      <p className="text-xs text-mist">You can reopen this guide in Settings.</p>
      <button className="btn btn-quiet" onClick={onDismiss}>I’m ready to explore</button>
    </div>
  </section>;
}
