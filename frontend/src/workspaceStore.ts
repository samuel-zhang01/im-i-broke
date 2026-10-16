/** Ephemeral view controls only. Never put responses, credentials or documents here. */
const values = new Map<string, unknown>();
const listeners = new Set<() => void>();
export const WORKSPACE_LIMIT = 160;
let generation = 0;

export function workspaceGeneration() { return generation; }
export function readWorkspaceValue<T>(key: string): T | undefined {
  return values.get(key) as T | undefined;
}
export function hasWorkspaceValue(key: string) { return values.has(key); }
export function writeWorkspaceValue<T>(key: string, value: T, expectedGeneration = generation) {
  // Discard late callbacks from a session which has already signed out.
  if (expectedGeneration !== generation) return;
  if (values.has(key) && Object.is(values.get(key), value)) return;
  values.delete(key);
  values.set(key, value);
  while (values.size > WORKSPACE_LIMIT) values.delete(values.keys().next().value!);
  listeners.forEach(listener => listener());
}
export function clearWorkspaceState() {
  generation += 1;
  values.clear();
  listeners.forEach(listener => listener());
}
export function watchWorkspaceState(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
