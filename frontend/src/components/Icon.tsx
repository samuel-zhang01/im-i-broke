import type { SVGProps } from "react";

const paths = {
  bank: "M3 10h18M12 3l9 5H3l9-5M5 10v8m7-8v8m7-8v8M3 21h18",
  plus: "M12 5v14M5 12h14",
  search: "M21 21l-5-5M18 10a8 8 0 11-16 0 8 8 0 0116 0",
  chart: "M4 20V10m8 10V4m8 16v-7",
  archive: "M3 3h18v4H3zM5 7v14h14V7M9 11h6",
  restore: "M3 10a9 9 0 111 8M3 4v6h6",
  sliders: "M4 7h4m4 0h8M4 17h8m4 0h4M8 4v6m4-6v6m0 4v6m4-6v6",
  shield: "M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3M8 12l3 3 5-6",
  upload: "M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6",
  tag: "M3 3h8l10 10-8 8L3 11V3M7 7h.01",
  chevron: "m9 5 7 7-7 7",
} as const;

/** Decorative companion to a visible label, never a replacement for its name. */
export default function Icon({ name, className = "", ...props }: SVGProps<SVGSVGElement> & { name: keyof typeof paths }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" {...props} className={`shrink-0 ${className}`} aria-hidden="true" focusable="false"><path d={paths[name]} /></svg>;
}
