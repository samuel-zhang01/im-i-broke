import Icon from "../components/Icon";
import { Heading } from "../components/Heading";
import { Disclosure } from "../components/Disclosure";
import { useSecuritySettings } from "../AuthGate";
import { Card } from "../components/ui";
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { performanceDiagnostics, type PerformanceReading } from "../performanceDiagnostics";
import { CategorySettings, WorkspacePreferencesSettings } from "../components/WorkspaceSettings";
import AccountSettings from "../components/AccountSettings";
import CreditCardSettings from "../components/CreditCardSettings";
import ConnectionSettings from "../components/ConnectionSettings";
import CsvImportSettings from "../components/CsvImportSettings";
import SessionSettings from "../components/SessionSettings";
import CurrencyReport from "../components/CurrencyReport";

import GettingStarted, { type SetupSection } from "../components/GettingStarted";

const metricLabels = { LCP: "Initial loading", INP: "Interaction response", CLS: "Layout shifts" };
function readingValue(reading: PerformanceReading) {
  if (!reading.supported) return "Not supported by this browser";
  if (reading.value === null) return reading.name === "INP" ? "No interaction measured yet" : "Not measured yet";
  if (reading.name === "CLS") return reading.value.toFixed(3);
  if (reading.name === "LCP") return `${(reading.value / 1000).toFixed(2)} s`;
  return `${Math.round(reading.value)} ms`;
}

function PerformanceReadings() {
  const readings = useSyncExternalStore(performanceDiagnostics.subscribe, performanceDiagnostics.getSnapshot);
  return <div className="mt-3 space-y-3">
    <p className="text-sm text-mist">Measurements for this open tab since its last full page load. They cover the whole visit, including sign-in, and continue across in-app pages. Values can change while you use the app.</p>
    <dl className="divide-y divide-line">
      {readings.map(reading => <div key={reading.name} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-3" data-performance-metric={reading.name}>
        <dt className="text-sm font-medium">{metricLabels[reading.name]} <span className="font-normal text-mist">· {reading.name}</span></dt>
        <dd className="text-right text-sm tabular-nums">
          <span data-performance-value>{readingValue(reading)}</span>
          {reading.rating && <span className="ml-2 text-xs text-mist">{reading.rating === "good" ? "Good" : reading.rating === "poor" ? (reading.name === "CLS" ? "Unstable" : "Slow") : "Needs improvement"}</span>}
        </dd>
      </div>)}
    </dl>
    <p className="text-xs text-mist">Only the latest three numeric readings stay in memory. No account data is collected, and nothing is saved or sent. Reload to start a new measurement. Browser support and background tabs can leave readings unavailable.</p>
  </div>;
}

type SettingsSection = SetupSection | "categories" | "cards";
type PanelRequest = { section: SettingsSection; sequence: number } | null;
type SettingsIcon = Parameters<typeof Icon>[0]["name"];

function SettingsPanel({ id, request, icon, title, description, children }: { id: SettingsSection; request: PanelRequest; icon: SettingsIcon; title: string; description: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [visited, setVisited] = useState(false);
  const summary = useRef<HTMLElement>(null);
  useEffect(() => {
    if (request?.section !== id) return;
    setOpen(true);
    setVisited(true);
    summary.current?.focus({ preventScroll: true });
    summary.current?.scrollIntoView({ block: "start", behavior: "auto" });
  }, [request, id]);
  return <Disclosure id={`settings-${id}`} title={title} description={description} icon={<Icon name={icon} width={18} height={18} />}
    summaryRef={summary} open={open} onToggle={event => { setOpen(event.currentTarget.open); if (event.currentTarget.open) setVisited(true); }}>
    {/* Mount on first use; closing a panel keeps unfinished forms in memory. */}
    {(open || visited) && children}
  </Disclosure>;
}

const shortcuts: { section: SettingsSection; label: string; icon: SettingsIcon }[] = [
  { section: "connections", label: "Banks & brokerages", icon: "bank" },
  { section: "accounts", label: "Accounts & balances", icon: "plus" },
  { section: "cards", label: "Credit card balances", icon: "sliders" },
  { section: "imports", label: "CSV imports", icon: "upload" },
  { section: "preferences", label: "Currency & formats", icon: "sliders" },
  { section: "categories", label: "Categories", icon: "tag" },
  { section: "reports", label: "Currency report", icon: "chart" },
];

export default function Settings({ initialSection }: { initialSection?: string }) {
  const security = useSecuritySettings();
  const [guideOpen, setGuideOpen] = useState(() => { try { return sessionStorage.getItem("ledger.onboarding") === "start"; } catch { return false; } });
  const [requestedPanel, setRequestedPanel] = useState<PanelRequest>(null);
  const chooseSection = (section: SettingsSection) => setRequestedPanel(previous => ({ section, sequence: (previous?.sequence || 0) + 1 }));
  useEffect(() => { if (initialSection === "reports" || initialSection === "connections") chooseSection(initialSection); }, [initialSection]);
  const dismissGuide = () => { setGuideOpen(false); try { sessionStorage.removeItem("ledger.onboarding"); } catch { /* Optional guide state. */ } };
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  return <div className="max-w-6xl space-y-5">
    <section aria-labelledby="settings-intro" className="relative overflow-hidden rounded-xl border border-line bg-surface2/60 p-4 sm:p-5">
      <div aria-hidden="true" className="absolute inset-y-0 left-0 w-1 bg-teal/70" />
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div><p className="eyebrow mb-2">Set up · connect · personalize</p><Heading id="settings-intro" level={2} variant="intro">Make this workspace yours</Heading><p className="mt-2 max-w-xl text-sm leading-relaxed text-mist">Bring your accounts together, choose how your reports look and manage access.</p></div>
        <div className="flex flex-wrap gap-2"><button className="btn-primary" onClick={() => chooseSection("connections")}><Icon name="plus" />Connect an account</button>{!guideOpen && <button className="btn" onClick={() => setGuideOpen(true)}>Getting started</button>}</div>
      </div>
    </section>
    {guideOpen && <GettingStarted onChoose={chooseSection} onDismiss={dismissGuide} />}
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
      <div className="min-w-0 space-y-5">
        <section aria-labelledby="settings-data-title" className="space-y-3">
          <div className="px-1"><Heading id="settings-data-title" level={2} variant="section">Your accounts & data</Heading><p className="mt-1 text-sm text-mist">Connect a provider, manage an account or bring your own history.</p></div>
          <SettingsPanel id="connections" request={requestedPanel} icon="bank" title="Provider connections" description="Add and manage bank and investment providers."><ConnectionSettings /></SettingsPanel>
          <SettingsPanel id="accounts" request={requestedPanel} icon="plus" title="Account settings" description="Names, ownership, reporting choices and manual balances."><AccountSettings onImport={() => chooseSection("imports")} /></SettingsPanel>
          <SettingsPanel id="cards" request={requestedPanel} icon="sliders" title="Credit card balances" description="Choose how each connected card reports its balance and confirm its credit limit."><CreditCardSettings onManualAccounts={() => chooseSection("accounts")} /></SettingsPanel>
          <SettingsPanel id="imports" request={requestedPanel} icon="upload" title="Import a CSV" description="Map your bank’s columns and review transactions before importing."><CsvImportSettings onCreateAccount={() => chooseSection("accounts")} onOpenReports={() => chooseSection("reports")} /></SettingsPanel>
        </section>
        <section aria-labelledby="settings-personalize-title" className="space-y-3">
          <div className="px-1"><Heading id="settings-personalize-title" level={2} variant="section">Make sense of your money</Heading><p className="mt-1 text-sm text-mist">Set your defaults and organize the way you see your finances.</p></div>
          <SettingsPanel id="preferences" request={requestedPanel} icon="sliders" title="Workspace preferences" description="Your reporting currency, number formats and time zone."><WorkspacePreferencesSettings /></SettingsPanel>
          <SettingsPanel id="categories" request={requestedPanel} icon="tag" title="Transaction categories" description="Create, rename and organize categories to fit your spending."><CategorySettings /></SettingsPanel>
          <SettingsPanel id="reports" request={requestedPanel} icon="chart" title="Workspace reports" description="Income and spending in your reporting currency, with conversion evidence."><CurrencyReport /></SettingsPanel>
        </section>
        <p className="px-1 text-xs leading-relaxed text-mist">Changes are saved when you confirm them. You can close and reopen a section without losing an unfinished form while you stay on this page.</p>
      </div>
      <div className="min-w-0 space-y-4">
        <nav aria-label="Settings shortcuts" className="hidden rounded-xl border border-line bg-white p-4 xl:block"><p className="eyebrow mb-2 px-2">Jump to a setting</p>{shortcuts.map(item => <button key={item.section} className="flex min-h-11 w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm text-mist transition-colors hover:bg-surface2 hover:text-foam" onClick={() => chooseSection(item.section)}><Icon name={item.icon} />{item.label}<span aria-hidden="true" className="ml-auto">↗</span></button>)}</nav>
        <Card className="!p-4">
          <div className="mb-4 flex items-center gap-3"><span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${security?.enabled ? "bg-kelp/10 text-kelp" : "bg-surface2 text-mist"}`}><Icon name="shield" width={20} height={20} /></span><Heading level={2} variant="section">Account &amp; security</Heading></div>
          <p className="text-sm leading-relaxed text-mist">{security?.enabled ? "Protected with two-factor authentication" : "Sign-in is disabled for this private workspace."}</p>
          {security?.enabled && <div className="mt-4 flex flex-wrap gap-2"><button className="btn" disabled={security.busy} onClick={security.recovery}>Recovery codes</button><button className="btn btn-quiet" disabled={security.busy} onClick={security.logout}>Sign out</button></div>}
          {security?.error && <p role="alert" className="mt-3 text-sm text-coral">{security.error}</p>}
          {security?.enabled && <Disclosure title="Signed-in sessions" variant="plain" className="mt-4" onToggle={event => setSessionsOpen(event.currentTarget.open)}>{sessionsOpen && <SessionSettings />}</Disclosure>}
          <Disclosure title="Performance diagnostics" variant="plain" className="mt-4" onToggle={event => setDiagnosticsOpen(event.currentTarget.open)}>{diagnosticsOpen && <PerformanceReadings />}</Disclosure>
        </Card>
        <div className="rounded-xl border border-dashed border-line p-4"><p className="text-sm font-medium">Start with what you have</p><p className="mt-2 text-sm leading-relaxed text-mist">A bank connection is optional. Add a manual account and a balance, then import older transactions whenever you’re ready.</p><button className="mt-3 min-h-11 text-left text-sm font-medium text-teal underline decoration-teal/30 underline-offset-4" onClick={() => chooseSection("accounts")}>Set up a manual account <span aria-hidden="true">→</span></button></div>
        <p className="px-1 text-sm leading-relaxed text-mist">Am I Broke? · <a className="underline underline-offset-4" href="https://github.com/samuel-zhang01/im-i-broke" target="_blank" rel="noreferrer">Source code</a> · <a className="underline underline-offset-4" href="https://github.com/samuel-zhang01/im-i-broke/blob/main/LICENSE" target="_blank" rel="noreferrer">AGPL-3.0</a></p>
      </div>
    </div>
  </div>;
}
