import { forwardRef, useId, type ComponentPropsWithoutRef, type ReactNode, type Ref } from "react";
import Icon from "./Icon";
import "./Disclosure.css";

type DisclosureProps = Omit<ComponentPropsWithoutRef<"details">, "children" | "title"> & {
  title: ReactNode;
  description?: string;
  icon?: ReactNode;
  children?: ReactNode;
  variant?: "card" | "plain";
  summaryRef?: Ref<HTMLElement>;
  contentClassName?: string;
};

/** Native disclosure behavior with one shared title, spacing and focus policy. */
export const Disclosure = forwardRef<HTMLDetailsElement, DisclosureProps>(function Disclosure({
  title,
  description,
  icon,
  children,
  variant = "card",
  summaryRef,
  contentClassName = "",
  className = "",
  id,
  ...props
}, ref) {
  const generatedId = useId();
  const titleId = `${id ?? generatedId}-title`;
  const descriptionId = `${id ?? generatedId}-description`;
  return <details {...props} ref={ref} id={id} className={`disclosure disclosure--${variant} ${className}`}>
    <summary ref={summaryRef} className="disclosure-summary" aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined}>
      {icon && <span className="disclosure-icon" aria-hidden="true">{icon}</span>}
      <span className="disclosure-label">
        <span id={titleId} className="disclosure-title">{title}</span>
        {description && <span id={descriptionId} className="disclosure-description">{description}</span>}
      </span>
      <Icon name="chevron" className="disclosure-chevron" />
    </summary>
    <div className={`disclosure-content ${contentClassName}`}>{children}</div>
  </details>;
});
