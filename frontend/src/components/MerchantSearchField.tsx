import { useId } from "react";
import type { MerchantSearchMode } from "../merchantSearch";

/** Consistent name matching controls in the directory, picker and merge preview. */
export default function MerchantSearchField({ label, value, mode, onChange, onModeChange, error, placeholder, className = "" }: {
  label: string; value: string; mode: MerchantSearchMode;
  onChange: (value: string) => void; onModeChange: (mode: MerchantSearchMode) => void;
  error?: string | null; placeholder?: string; className?: string;
}) {
  const hintId = useId();
  return <div className={`min-w-0 ${className}`}>
    <div className="flex flex-wrap items-end gap-2">
      <label className="field-label min-w-0 flex-1 basis-48">{label}
        <input type="search" className="input w-full" aria-label={label} aria-describedby={mode === "regex" || error ? hintId : undefined}
          aria-invalid={!!error} value={value} onChange={event => onChange(event.target.value)}
          placeholder={placeholder ?? (mode === "regex" ? "^(AMEX|AMERICAN EXP)" : "Name, alias or approximate spelling…")} />
      </label>
      <label className="field-label">Match using
        <select className="input" aria-label={`${label} matching mode`} value={mode} onChange={event => onModeChange(event.target.value as MerchantSearchMode)}>
          <option value="fuzzy">Fuzzy</option><option value="text">Text</option><option value="regex">Regex</option>
        </select>
      </label>
    </div>
    {(mode === "regex" || error) && <p id={hintId} role={error ? "alert" : undefined} className={`mt-2 text-xs leading-relaxed ${error ? "text-coral" : "text-mist"}`}>
      {error || <>Case-insensitive. For example, <code>^(AMEX|AMERICAN EXP)</code> finds either prefix; <code>.*</code> matches any text.</>}
    </p>}
  </div>;
}
