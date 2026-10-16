import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { api } = await sourceModule("api");
const { watchData, dataChanged } = await sourceModule("dataEvents");
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const tick = () => new Promise(resolve => setImmediate(resolve));

test("successful edits update observers; reads, failed saves and preferences do not", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let changes = 0;
  const stop = watchData(() => changes++);
  t.after(stop);
  globalThis.fetch = async () => json({ merchant: { id: 1 } });
  await api.summary(); await api.merchants(); await api.scenarios();
  await api.savePreferences("spending", { query: {} });
  await tick();
  assert.equal(changes, 0, "read requests must never cause refresh loops");
  const writes = [
    () => api.approveMerchantAlias(1, "Sample Shop"),
    () => api.dismissMerchantSuggestion(1, "Sample Shop"),
    () => api.restoreMerchantSuggestion(1, "Sample Shop"),
    () => api.updateMerchant(1, {}),
    () => api.disableMerchant(1),
    () => api.saveReview(1, { category: "Eating out" }),
    () => api.resetReview(1),
    () => api.setCategory(1, "Shopping"),
    () => api.saveScenario("Sample", {}),
    () => api.deleteScenario(1),
  ];
  for (const write of writes) {
    const before = changes;
    await write(); await tick();
    assert.equal(changes, before + 1);
  }
  globalThis.fetch = async () => json({ detail: "Conflict" }, 409);
  await assert.rejects(() => api.approveMerchantAlias(1, "Sample Shop"), /Conflict/);
  globalThis.fetch = async () => new Response("broken JSON", { status: 200 });
  await assert.rejects(() => api.saveReview(1, {}));
  await tick();
  assert.equal(changes, writes.length, "no success announcement before valid acknowledgement");
});

test("network failures produce an actionable message, without a false save notification", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  await assert.rejects(api.summary, /local Am I Broke\? server.*running/);
});

test("visible clients notice database revisions and cross-tab changes, and clean up on leave", async t => {
  const names = ["window", "document", "fetch", "BroadcastChannel", "setInterval", "clearInterval"];
  const originals = Object.fromEntries(names.map(name => [name, globalThis[name]]));
  t.after(() => { for (const name of names) {
    if (originals[name] === undefined) delete globalThis[name]; else globalThis[name] = originals[name];
  } });
  globalThis.window = new EventTarget();
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: "visible" });
  let poll, cleared = false, closed = false, channel;
  globalThis.setInterval = callback => { poll = callback; return 123; };
  globalThis.clearInterval = id => { assert.equal(id, 123); cleared = true; };
  globalThis.BroadcastChannel = class {
    constructor() { channel = this; }
    postMessage(value) { this.sent = value; }
    close() { closed = true; }
  };
  let revision = "a", requests = 0, changes = 0, failed = true;
  globalThis.fetch = async () => { requests++; if (failed) throw new Error("Server restarting"); return json({ revision }); };
  const stop = watchData(() => changes++);
  await tick();
  assert.equal(changes, 0);
  failed = false;
  poll(); await tick();
  assert.equal(changes, 1, "first successful revision synchronizes retained data");
  poll(); await tick();
  assert.equal(changes, 1, "unchanged revision is quiet");
  revision = "b"; poll(); await tick();
  assert.equal(changes, 2);
  document.visibilityState = "hidden";
  const before = requests; poll(); await tick();
  assert.equal(requests, before);
  document.visibilityState = "visible";
  window.dispatchEvent(new Event("focus")); await tick();
  assert.equal(changes, 2, "focus checks the revision without reloading unchanged views");
  channel.onmessage({ data: "changed" }); await tick();
  assert.equal(changes, 3);
  dataChanged("transactions/1/review"); await tick();
  assert.equal(channel.sent, "changed");
  assert.equal(changes, 4);
  failed = true; poll(); await tick(); failed = false; poll(); await tick();
  assert.equal(changes, 5, "a recovered connection refreshes even when the database did not change");
  stop();
  assert.equal(cleared && closed, true);
  window.dispatchEvent(new Event("focus")); await tick();
  assert.equal(changes, 5);
});
