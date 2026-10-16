import { useState } from "react";
import { institutionInitials, institutionLogo } from "../institutions";

/** Decorative beside the institution's visible name; assets never contact a third party. */
export default function InstitutionLogo({ name, large = false }: { name: string; large?: boolean }) {
  const src = institutionLogo(name);
  const [failedSrc, setFailedSrc] = useState<string>();
  return <span aria-hidden="true" className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-xl border border-line bg-white ${large ? "h-14 w-14 p-1.5" : "h-10 w-10 p-1"}`}>
    {src && failedSrc !== src
      ? <img src={src} alt="" width={large ? 44 : 30} height={large ? 44 : 30} className="h-full w-full object-contain" onError={() => setFailedSrc(src)} />
      : <span className="text-sm font-semibold text-mist">{institutionInitials(name)}</span>}
  </span>;
}
