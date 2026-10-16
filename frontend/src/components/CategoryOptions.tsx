import { api } from "../api";
import { categoryOptions } from "../categories";
import { ErrorBox, useAsync } from "./ui";

export function useCategoryOptions(...current: (string | null | undefined)[]) {
  const state = useAsync(signal => api.categoryOptions(signal));
  return { ...state, options: categoryOptions(state.data, current) };
}

export function CategoryOptionsError({ state }: { state: { error: string | null; refetch: () => void } }) {
  return state.error ? <ErrorBox error={`Could not load saved category choices. Built-in and current choices remain available. ${state.error}`} retry={state.refetch} /> : null;
}
