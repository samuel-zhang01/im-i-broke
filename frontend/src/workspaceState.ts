import { useCallback, useMemo, useSyncExternalStore, type Dispatch, type SetStateAction } from "react";
import { hasWorkspaceValue, readWorkspaceValue, watchWorkspaceState, workspaceGeneration, writeWorkspaceValue } from "./workspaceStore";
export { clearWorkspaceState } from "./workspaceStore";

/** Remembers controls across page unmounts, only for this signed-in browser session. */
export function useWorkspaceState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  // A changed account key receives its own initial value, without leaking the
  // previous account's controls into the next account's first render.
  const fallback = useMemo(() => typeof initial === "function" ? (initial as () => T)() : initial, [key]);
  const snapshot = useCallback(() => hasWorkspaceValue(key) ? readWorkspaceValue<T>(key)! : fallback, [key, fallback]);
  const value = useSyncExternalStore(watchWorkspaceState, snapshot, snapshot);
  const generation = workspaceGeneration();
  const setValue = useCallback<Dispatch<SetStateAction<T>>>(next => {
    const previous = hasWorkspaceValue(key) ? readWorkspaceValue<T>(key)! : fallback;
    writeWorkspaceValue(key, typeof next === "function" ? (next as (previous: T) => T)(previous) : next, generation);
  }, [key, fallback, generation]);
  return [value, setValue];
}
