import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { fetchMerchantMergeSuggestions, previewMerchantMerge, saveMerchantMerge } = await sourceModule("merchantMerge");
const { watchData } = await sourceModule("dataEvents");
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const tick = () => new Promise(resolve => setImmediate(resolve));
const selection = { target_id: 5, source_ids: [8, 12] };

function observe(t) {
  const originalFetch = globalThis.fetch;
  let changes = 0;
  const stop = watchData(() => changes++);
  t.after(() => { stop(); globalThis.fetch = originalFetch; });
  return () => changes;
}

test("duplicate discovery and merge previews keep the exact selection and never announce writes", async t => {
  const changes = observe(t), requests = [];
  const preview = { ...selection, preview_token: "previewed-selection" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return json(url.includes("/suggestions") ? { items: [], total: 0 } : preview);
  };
  assert.deepEqual(await fetchMerchantMergeSuggestions(), { items: [], total: 0 });
  assert.deepEqual(await previewMerchantMerge(selection), preview);
  assert.deepEqual(requests.map(request => request.url), ["/api/merchants/merge/suggestions?limit=100", "/api/merchants/merge/preview"]);
  assert.equal(requests[1].options.method, "POST");
  assert.deepEqual(JSON.parse(requests[1].options.body), selection);
  assert.ok(requests.every(request => request.options.headers.get("X-Ledger-Request") === "1"));
  assert.ok(requests.every(request => request.options.credentials === "same-origin"));
  await tick();
  assert.equal(changes(), 0);
});

test("merge saves send the confirmed preview token and refresh the application after success", async t => {
  const changes = observe(t);
  const confirmed = { ...selection, preview_token: "confirmed-preview" };
  const result = { ok: true, target_id: 5, merged_ids: [8, 12], updated_transactions: 7, preserved_reviews: 2, message: "Merged three saved names." };
  let request;
  globalThis.fetch = async (url, options) => { request = { url, options }; return json(result); };
  assert.deepEqual(await saveMerchantMerge(confirmed), result);
  assert.equal(request.url, "/api/merchants/merge");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), confirmed);
  await tick();
  assert.equal(changes(), 1);
});

test("stale merge errors retain their actionable detail without reporting successful writes", async t => {
  const changes = observe(t);
  globalThis.fetch = async () => json({ detail: "The names or saved decisions changed. Review a fresh merge preview before confirming." }, 409);
  await assert.rejects(saveMerchantMerge({ ...selection, preview_token: "stale" }), error => error.status === 409 && /fresh merge preview/.test(error.message));
  await tick();
  assert.equal(changes(), 0);
});

test("preview cancellation is forwarded and read timeouts differ from uncertain saves", async t => {
  observe(t);
  const originalTimer = globalThis.setTimeout;
  let abort, duration;
  t.after(() => { globalThis.setTimeout = originalTimer; });
  globalThis.setTimeout = (callback, ms) => { abort = callback; duration = ms; return undefined; };
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    queueMicrotask(() => abort());
  });
  await assert.rejects(previewMerchantMerge(selection), /took too long to check/);
  assert.equal(duration, 30000);
  await assert.rejects(saveMerchantMerge({ ...selection, preview_token: "confirmed" }), /may have completed/);
  assert.equal(duration, 120000);

  const controller = new AbortController();
  globalThis.fetch = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
  });
  const pending = previewMerchantMerge(selection, controller.signal);
  controller.abort();
  await assert.rejects(pending, error => error.name === "AbortError");
});
