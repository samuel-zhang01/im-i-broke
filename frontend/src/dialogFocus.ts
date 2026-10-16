import type { KeyboardEvent } from "react";

/** Keep keyboard focus inside a dialog using only controls in its visible tab order. */
export function trapDialogFocus(event: KeyboardEvent<HTMLElement>) {
  if (event.defaultPrevented || event.key !== "Tab") return;
  const container = event.currentTarget;
  const controls = [...container.querySelectorAll<HTMLElement>("a[href],button,input,select,textarea,summary,[tabindex]")].filter(element => {
    if (element.tabIndex < 0 || element.matches(":disabled") || element.closest("[hidden],[inert]") ||
      !element.getClientRects().length || getComputedStyle(element).visibility !== "visible") return false;
    for (let parent = element.parentElement; parent && parent !== container; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS" && !parent.hasAttribute("open") && !parent.querySelector(":scope > summary")?.contains(element)) return false;
    }
    return true;
  });
  const current = controls.indexOf(document.activeElement as HTMLElement);
  if (current < 0 || (!event.shiftKey && current === controls.length - 1) || (event.shiftKey && current === 0)) {
    event.preventDefault();
    (event.shiftKey ? controls[controls.length - 1] : controls[0])?.focus();
    if (!controls.length) container.focus();
  }
}
