import { forwardRef, type HTMLAttributes } from "react";

type HeadingVariant = "page" | "intro" | "section" | "subsection" | "label";
const HEADING_STYLES: Record<HeadingVariant, string> = {
  page: "heading--page",
  intro: "heading--intro",
  section: "heading--section",
  subsection: "heading--subsection",
  label: "heading--label",
};
type HeadingProps = HTMLAttributes<HTMLHeadingElement> & {
  level?: 1 | 2 | 3 | 4;
  variant?: HeadingVariant;
};

/** Semantic level and visual emphasis are independent; all titles share one scale. */
export const Heading = forwardRef<HTMLHeadingElement, HeadingProps>(function Heading(
  { level = 2, variant = level === 1 ? "page" : level === 2 ? "section" : "subsection", className = "", ...props },
  ref,
) {
  const Tag = `h${level}` as "h1" | "h2" | "h3" | "h4";
  return <Tag ref={ref} className={`heading ${HEADING_STYLES[variant]} ${className}`} {...props} />;
});
