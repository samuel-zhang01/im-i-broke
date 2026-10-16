import { useCallback, useLayoutEffect, useRef } from "react";
import { readWorkspaceValue, workspaceGeneration, writeWorkspaceValue } from "./workspaceStore";

type Position = { x: number; y: number };
export type WorkspaceNavigation = { sequence: number; restore: boolean };
const keyFor = (page: string) => `scroll:${page}`;

/** Restore only after real content settles; input always gives control back. */
export function useWorkspaceScroll(page: string, navigation: WorkspaceNavigation) {
  const current = useRef({ page, generation: workspaceGeneration(), pending: false });
  const remember = useCallback(() => {
    const state = current.current;
    if (!state.pending) writeWorkspaceValue(keyFor(state.page), { x: window.scrollX, y: window.scrollY }, state.generation);
  }, []);
  useLayoutEffect(() => {
    const state = { page, generation: workspaceGeneration(), pending: false };
    current.current = state;
    const target = navigation.restore ? readWorkspaceValue<Position>(keyFor(page)) : undefined;
    if (!target || target.y < 1) return;
    const main = document.getElementById("main-content");
    if (!main) return;
    state.pending = true;
    let stopped = false;
    let frame = 0;
    let timeout = 0;
    let previousHeight = -1;
    let stableFrames = 0;
    let restored = false;
    const finish = () => {
      if (stopped) return;
      stopped = true;
      state.pending = false;
      cancelAnimationFrame(frame);
      clearTimeout(timeout);
      mutations.disconnect();
      resize?.disconnect();
      window.removeEventListener("wheel", cancel);
      window.removeEventListener("touchstart", cancel);
      window.removeEventListener("pointerdown", cancel);
      window.removeEventListener("keydown", cancel);
      main.removeEventListener("focusin", cancel);
    };
    const cancel = () => finish();
    const check = () => {
      if (stopped) return;
      if (workspaceGeneration() !== state.generation) { finish(); return; }
      // Hidden controls may contain a loader without affecting page geometry.
      const busy = [...main.querySelectorAll<HTMLElement>('[aria-busy="true"]')]
        .some(element => element.getClientRects().length > 0);
      const height = document.documentElement.scrollHeight;
      stableFrames = !busy && height === previousHeight ? stableFrames + 1 : 0;
      previousHeight = height;
      if (stableFrames >= 2) {
        window.scrollTo({ left: target.x, top: target.y, behavior: "auto" });
        // A restored viewport can mount deferred sections. Give those a chance
        // to replace their reserved space before completing restoration.
        if (restored) { finish(); return; }
        restored = true;
        stableFrames = 0;
      }
      if (!busy) frame = requestAnimationFrame(check);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(check); };
    const mutations = new MutationObserver(schedule);
    mutations.observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["aria-busy", "class", "style"] });
    const resize = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(schedule);
    resize?.observe(main);
    window.addEventListener("wheel", cancel, { passive: true });
    window.addEventListener("touchstart", cancel, { passive: true });
    window.addEventListener("pointerdown", cancel, { passive: true });
    window.addEventListener("keydown", cancel);
    main.addEventListener("focusin", cancel);
    // Failed or indefinitely pending sources must never leave a latent jump.
    timeout = window.setTimeout(finish, 15_000);
    schedule();
    return finish;
  }, [page, navigation]);
  useLayoutEffect(() => {
    const previous = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";
    return () => { window.history.scrollRestoration = previous; };
  }, []);
  return remember;
}
