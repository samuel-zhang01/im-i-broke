import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { api } = await sourceModule("api");
const { watchData } = await sourceModule("dataEvents");
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const tick = () => new Promise(resolve => setImmediate(resolve));

const query = {
  canonical_name: "Example Coffee", kind: "merchant", spending_treatment: "normal",
  merchant_id: null, example_id: 11, patterns: ["Example Coffee London"],
};
const assignment = {
  example_id: 11, merchant_id: null, canonical_name: "Example Coffee",
  category: "Eating out", kind: "merchant", spending_treatment: "normal",
  additional_example_ids: [22, 33],
};
const candidate = {
  example_id: 22, label: "Example Coffe", context: "spending_card",
  n_transactions: 3, outgoing_total: 18.50, examples: [], score: 0.96,
  reason: "Similar spelling in card purchases.",
};
const secondCandidate = { ...candidate, example_id: 33, label: "Example Coffee UK", n_transactions: 2, outgoing_total: 12.00 };
const preview = {
  pattern: "Example Coffee London", context: "spending_card", canonical_name: "Example Coffee",
  category: "Eating out", kind: "merchant", spending_treatment: "normal",
  matched_transactions: 8, outgoing_total: 42.50, preserved_reviews: 1, examples: [],
  conflict: null, preview_token: "reviewed-aliases-token", learned_suggestion: null,
  similar_names: [candidate, secondCandidate], additional_aliases: [candidate, secondCandidate], alias_count: 3,
};

function observe(t) {
  const originalFetch = globalThis.fetch;
  let changes = 0;
  const stop = watchData(() => changes++);
  t.after(() => { stop(); globalThis.fetch = originalFetch; });
  return () => changes;
}

test("similar-name discovery and selection previews are reads and preserve the requested scope", async t => {
  const changes = observe(t);
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return json(url.endsWith("/similar") ? { items: [candidate], method: "Local spelling comparison" } : preview);
  };

  const suggestions = await api.similarMerchantNames(query);
  const result = await api.previewMerchantClassification(assignment);
  await tick();

  assert.deepEqual(suggestions.items, [candidate]);
  assert.deepEqual(result, preview);
  assert.deepEqual(requests.map(request => request.url), [
    "/api/merchants/similar", "/api/merchants/classify/preview",
  ]);
  assert.deepEqual(requests.map(request => JSON.parse(request.options.body)), [query, assignment]);
  assert.ok(requests.every(request => request.options.method === "POST"));
  assert.equal(changes(), 0, "typing and choosing draft aliases must not refresh or mutate the ledger");
});

test("classification sends exactly the selected example IDs and preview token, then notifies observers once", async t => {
  const changes = observe(t);
  const save = { ...assignment, preview_token: preview.preview_token };
  const confirmation = { ...preview, ok: true, merchant_id: 5, updated_transactions: 7, message: "Saved three exact aliases." };
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return json(confirmation);
  };

  const result = await api.classifyMerchant(save);
  await tick();

  assert.equal(request.url, "/api/merchants/classify");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), save);
  assert.deepEqual(result, confirmation);
  assert.equal(changes(), 1, "a confirmed batch save refreshes the app without a page reload");
});

test("stale alias conflicts reach the editor without announcing a successful save", async t => {
  const changes = observe(t);
  globalThis.fetch = async () => json({ detail: "These selected names changed. Refresh the preview." }, 409);

  for (const operation of [
    () => api.similarMerchantNames(query),
    () => api.previewMerchantClassification(assignment),
    () => api.classifyMerchant({ ...assignment, preview_token: "stale-token" }),
  ]) {
    await assert.rejects(operation, error => error.status === 409 && /selected names changed/.test(error.message));
  }
  await tick();
  assert.equal(changes(), 0, "conflicts must retain the draft instead of announcing a write");
});

test('read-only POST previews time out as reads, while saves retain an uncertain-save warning', async t => {
  const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout;
  let abort, duration;
  t.after(() => { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer; });
  globalThis.setTimeout = (callback, ms) => { abort = callback; duration = ms; return undefined; };
  globalThis.fetch = (_path, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('aborted','AbortError')));
    queueMicrotask(() => abort());
  });
  await assert.rejects(api.previewMerchantClassification(assignment), /local server took too long/i);
  assert.equal(duration,30000);
  await assert.rejects(api.similarMerchantNames(query), /local server took too long/i);
  assert.equal(duration,30000);
  await assert.rejects(api.classifyMerchant(assignment), /may have completed/i);
  assert.equal(duration,120000);
});
