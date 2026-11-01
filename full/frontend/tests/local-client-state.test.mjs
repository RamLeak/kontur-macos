import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

test("exam browser histories are collected and saved with all three scope identifiers", async () => {
  const scope = "exam:exam-1:session-2:item:item-3:browser";
  const key = `kontur:device-python-runs:v1:${encodeURIComponent(scope)}`;
  const runs = [{ id: "exam-run-1", status: "completed", stdout: "42\n", stderr: "", duration_ms: 1, exit_code: 0, created_at: "2026-01-01T00:00:00.000Z" }];
  const expected = { version: 1, entries: { [key]: runs } };
  assert.equal(isClientStateKey(key), true);
  assert.deepEqual(readClientStorage(new MemoryStorage([[key, JSON.stringify(runs)]])), expected);
  const h = harness(expected); const store = new LocalClientStateStore(h.io);
  await store.initialize(); await store.flush(); assert.deepEqual(h.remote().value, expected);
});

test("exam run scopes keep canonical encoding, three safe IDs and the existing history bounds", () => {
  const prefix = "kontur:device-python-runs:v1:";
  const invalid = ["exam:exam-1:item:item-3:browser", "exam:exam-1:session-2:item:item-3:server", "exam:exam-1:session-2:extra:item:item-3:browser", "exam:exam-1::item:item-3:browser", "exam:exam-1:constructor:item:item-3:browser", "exam:exam-1:session-2:item:../item:browser", `exam:${"x".repeat(161)}:session-2:item:item-3:browser`];
  for (const scope of invalid) {
    const key = prefix + encodeURIComponent(scope); assert.equal(isClientStateKey(key), false, scope);
    assert.throws(() => validateClientStateValue({ version: 1, entries: { [key]: [] } }));
  }
  const canonical = prefix + encodeURIComponent("exam:exam-1:session-2:item:item-3:browser");
  assert.equal(isClientStateKey(canonical.replace("%3A", "%3a")), false);
  assert.throws(() => validateClientStateValue({ version: 1, entries: { [canonical]: Array.from({ length: 21 }, () => ({})) } }));
});
const require = createRequire(import.meta.url), modules = new Map(), overrides = new Map();
function load(name) {
  if (overrides.has(name)) return overrides.get(name);
  const path = fileURLToPath(new URL(`../lib/${name}.ts`, import.meta.url));
  if (modules.has(path)) return modules.get(path).exports;
  const module = { exports: {} }; modules.set(path, module);
  const compiled = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  new Function("module", "exports", "require", compiled)(module, module.exports, id => id.startsWith("./") ? load(id.slice(2)) : require(id));
  return module.exports;
}
const { LocalClientStateStore } = load("local-client-state-store");
const { validateClientStateValue, isClientStateKey, emptyClientState } = load("local-client-state-schema");
const clone = value => JSON.parse(JSON.stringify(value));
const pref = theme => ({ version: 1, entries: { "kontur:interface-preferences:v1": { theme } } });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));
function harness(local = emptyClientState(), initial = { version: 0, value: emptyClientState() }) {
  let current = clone(local), remote = clone(initial), outbox = null, failJournal = false;
  const writes = [], calls = [];
  const io = {
    readLocal: () => clone(current), applyLocal: value => { current = clone(value); },
    readOutbox: () => clone(outbox), writeOutbox: value => { if (failJournal) throw new Error("storage full"); outbox = clone(value); },
    readRemote: async () => { calls.push("GET"); return clone(remote); },
    writeRemote: async (expectedVersion, value) => {
      calls.push("PUT"); writes.push({ expectedVersion, value: clone(value) });
      if (expectedVersion !== remote.version) throw Object.assign(new Error("conflict"), { status: 409 });
      remote = { version: expectedVersion + 1, value: clone(value) }; return clone(remote);
    },
  };
  return { io, writes, calls, remote: () => clone(remote), local: () => clone(current), outbox: () => clone(outbox), edit: value => { current = clone(value); }, setRemote: value => { remote = clone(value); }, failJournal: value => { failJournal = value; } };
}

test("hydrate committed settings before consumers and seed a new backend from existing harmless settings", async () => {
  const existing = harness(pref("light"), { version: 4, value: pref("dark") });
  const first = new LocalClientStateStore(existing.io); await first.initialize();
  assert.deepEqual(existing.local(), pref("dark")); assert.equal(first.snapshot().dirty, false); assert.equal(existing.writes.length, 0);
  const fresh = harness(pref("light")); const second = new LocalClientStateStore(fresh.io); await second.initialize(); await second.flush();
  assert.deepEqual(fresh.remote(), { version: 1, value: pref("light") }); assert.equal(fresh.outbox(), null);
});

test("one writer drains an edit made while an older optimistic write is pending", async () => {
  const h = harness(); const store = new LocalClientStateStore(h.io); await store.initialize();
  const release = deferred(); const write = h.io.writeRemote; let active = 0, maximum = 0;
  h.io.writeRemote = async (...args) => { maximum = Math.max(maximum, ++active); if (h.writes.length === 0) await release.promise; try { return await write(...args); } finally { active--; } };
  h.edit(pref("light")); store.change(h.local()); const flushing = store.flush(); await tick();
  h.edit(pref("dark")); store.change(h.local()); release.resolve(); await flushing;
  assert.equal(maximum, 1); assert.deepEqual(h.writes.map(w => w.expectedVersion), [0, 1]); assert.deepEqual(h.remote().value, pref("dark")); assert.equal(store.snapshot().dirty, false);
});

test("lost ACK plus a newer edit survives restart and rebases only the matching in-flight snapshot", async () => {
  const h = harness(); const before = new LocalClientStateStore(h.io); await before.initialize();
  const release = deferred(); const write = h.io.writeRemote;
  h.io.writeRemote = async (...args) => { await release.promise; await write(...args); throw new Error("response lost"); };
  h.edit(pref("light")); before.change(h.local()); const failed = before.flush(); await tick();
  h.edit(pref("dark")); before.change(h.local()); release.resolve(); await assert.rejects(failed);
  assert.equal(before.snapshot().dirty, true); assert.equal(before.snapshot().localRecoverable, true); assert.ok(h.outbox().inflight);
  h.io.writeRemote = write; const restarted = new LocalClientStateStore(h.io); await restarted.initialize(); await restarted.flush();
  assert.deepEqual(h.remote(), { version: 2, value: pref("dark") }); assert.equal(h.outbox(), null);
});

test("unrelated remote revision retains local recovery and refuses a durable ACK", async () => {
  const h = harness(); const before = new LocalClientStateStore(h.io); await before.initialize();
  h.edit(pref("light")); before.change(h.local());
  h.setRemote({ version: 2, value: pref("dark") });
  const restarted = new LocalClientStateStore(h.io); await assert.rejects(restarted.initialize(), /конфликт/i);
  assert.deepEqual(h.local(), pref("light")); assert.equal(h.outbox().baseVersion, 0); assert.equal(restarted.snapshot().conflict, true); await assert.rejects(restarted.flush());
});

test("journal failure prevents a write and any successful flush acknowledgement", async () => {
  const h = harness(); const store = new LocalClientStateStore(h.io); await store.initialize(); h.failJournal(true);
  h.edit(pref("dark")); assert.throws(() => store.change(h.local())); await assert.rejects(store.flush());
  assert.equal(h.writes.length, 0); assert.equal(store.snapshot().dirty, true); assert.equal(store.snapshot().localRecoverable, false);
  h.failJournal(false); await store.flush(); assert.equal(store.snapshot().dirty, false);
});

test("post-import reload accepts the committed backup revision and removes old missing keys", async () => {
  const h = harness(pref("dark"), { version: 8, value: pref("dark") }); const store = new LocalClientStateStore(h.io); await store.initialize();
  h.setRemote({ version: 3, value: emptyClientState() }); await store.reloadAfterImport();
  assert.deepEqual(h.local(), emptyClientState()); assert.equal(store.snapshot().version, 3); assert.equal(store.snapshot().dirty, false);
});

test("strict schema rejects authority, arbitrary caches, unknown fields and oversized histories without trimming", () => {
  for (const key of ["kontur:signed-out", "kontur:pending-draft:secret", "kontur:interface-preferences:v1:owner:other", "kontur:study-pending:v1:x", "auth", "kontur:device-python-runs:v1:../auth"]) {
    assert.equal(isClientStateKey(key), false, key); assert.throws(() => validateClientStateValue({ version: 1, entries: { [key]: {} } }));
  }
  assert.throws(() => validateClientStateValue({ version: 1, entries: { "kontur:interface-preferences:v1": { theme: "dark", token: "never" } } }));
  assert.throws(() => validateClientStateValue({ version: 1, entries: { "kontur:shortcut-profile:windows:v1": { "code.run": "Alt+F4" } } }));
  const run = { id: "run", status: "completed", stdout: "я".repeat(200000), stderr: "я".repeat(200000), duration_ms: 0, exit_code: 0, created_at: "2026-10-29T00:00:00.000Z" };
  const runs = Array.from({ length: 12 }, (_, i) => ({ ...run, id: `run-${i}` }));
  assert.throws(() => validateClientStateValue({ version: 1, entries: { "kontur:device-python-runs:v1:practice%3Atask-1%3Abrowser": runs } }), /8 МиБ/);
  assert.equal(runs.length, 12); assert.equal(runs[0].stdout.length, 200000);
});

test("a four-megabyte original Python history saves and reopens without copying it into the recovery journal", async () => {
  const run = { id: "run", status: "completed", stdout: "x".repeat(200000), stderr: "", duration_ms: 0, exit_code: 0, created_at: "2026-10-29T00:00:00.000Z" };
  const history = { version: 1, entries: { "kontur:device-python-runs:v1:practice%3Atask-1%3Abrowser": Array.from({ length: 20 }, (_, i) => ({ ...run, id: `run-${i}` })) } };
  const h = harness(history); const store = new LocalClientStateStore(h.io); await store.initialize();
  assert.ok(JSON.stringify(h.outbox()).length < 256); await store.flush();
  assert.deepEqual(h.remote().value, history); h.edit(emptyClientState()); const reopened = new LocalClientStateStore(h.io); await reopened.initialize();
  assert.deepEqual(h.local(), history); assert.equal(h.local().entries[Object.keys(history.entries)[0]][19].stdout.length, 200000);
});

const { readClientStorage, applyClientStorage, observeClientStorage } = load("local-client-storage");
class MemoryStorage {
  constructor(entries = []) { this.values = new Map(entries); this.reject = null; }
  get length() { return this.values.size; }
  key(index) { return [...this.values.keys()][index] ?? null; }
  getItem(key) { if (key === "synthetic-excluded") throw new Error("excluded value must never be read"); return this.values.get(key) ?? null; }
  setItem(key, value) { if (this.reject?.(key, value)) throw new Error("quota"); this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}
test("storage backup reads only admitted values and leaves excluded data untouched on restore", () => {
  const storage = new MemoryStorage([["synthetic-excluded", "untouched"], ["kontur:interface-preferences:v1", '{"theme":"dark"}']]);
  assert.deepEqual(readClientStorage(storage), pref("dark")); applyClientStorage(storage, emptyClientState());
  assert.equal(storage.values.get("synthetic-excluded"), "untouched"); assert.equal(storage.values.has("kontur:interface-preferences:v1"), false);
});

test("restoration rolls back allowed settings when WebView quota fails midway", () => {
  const key = "kontur:interface-preferences:v1";
  const storage = new MemoryStorage([[key, '{"theme":"dark"}'], ["kontur:assistant-technical-mode:v1", "false"]]);
  let rejected = false; storage.reject = (name, raw) => { if (!rejected && name === key && raw.includes("light")) { rejected = true; return true; } return false; };
  assert.throws(() => applyClientStorage(storage, pref("light")), /quota/);
  assert.deepEqual(readClientStorage(storage), { version: 1, entries: { [key]: { theme: "dark" }, "kontur:assistant-technical-mode:v1": false } });
});

test("observer journals before a write, tracks only the selected storage and preserves original setter failure", () => {
  const local = new MemoryStorage(), session = new MemoryStorage(); const order = [];
  let deny = false;
  const restore = observeClientStorage(local, {
    before: () => { order.push("before"); assert.equal(local.getItem("kontur:assistant-technical-mode:v1"), null); if (deny) throw new Error("journal full"); },
    after: () => { order.push("after"); }, failed: () => { order.push("failure"); },
  });
  try {
    local.setItem("not-allowlisted", "private"); session.setItem("kontur:assistant-technical-mode:v1", "true"); assert.deepEqual(order, []);
    local.setItem("kontur:assistant-technical-mode:v1", "true"); assert.deepEqual(order, ["before", "after"]);
    local.values.delete("kontur:assistant-technical-mode:v1"); deny = true;
    assert.throws(() => local.setItem("kontur:assistant-technical-mode:v1", "false"), /journal full/); assert.equal(local.getItem("kontur:assistant-technical-mode:v1"), null);
    assert.deepEqual(order.slice(-2), ["before", "failure"]);
  } finally { restore(); }
});

test("original draft barrier remains dirty and awaits the real client-state ACK", async () => {
  const lifecycle = load("draft-lifecycle"); lifecycle.resetDraftLifecycle();
  const h = harness(); const store = new LocalClientStateStore(h.io); await store.initialize();
  const disk = deferred(), write = h.io.writeRemote;
  h.io.writeRemote = async (...args) => { await disk.promise; return write(...args); };
  lifecycle.registerDraftFlush(() => store.flush(), "offline-client-state");
  lifecycle.registerDraftSaveState(() => ({ ...store.snapshot(), offline: false }), "offline-client-state");
  h.edit(pref("dark")); store.markLocalMutation(); const flush = lifecycle.flushOpenDrafts(); await tick();
  assert.equal(lifecycle.getOpenDraftSaveState().dirty, true); assert.equal(lifecycle.getOpenDraftSaveState().saving, true);
  disk.resolve(); await flush; assert.equal(lifecycle.getOpenDraftSaveState().dirty, false); assert.equal(lifecycle.getOpenDraftSaveState().saving, false);
  lifecycle.resetDraftLifecycle();
});
test("exam mark toggled off and original shortcut profiles remain valid backup data", () => {
  const { DEFAULT_SHORTCUT_PROFILES } = load("kontur-shortcuts");
  assert.doesNotThrow(() => validateClientStateValue({ version: 1, entries: {
    "kontur:shortcut-profile:windows:v1": clone(DEFAULT_SHORTCUT_PROFILES.windows),
    "kontur:shortcut-profile:macos:v1": clone(DEFAULT_SHORTCUT_PROFILES.macos),
    "kontur:exam-ui:v1:exam-1": { itemId: "item-1", flags: { "item-1": false }, surfaces: { "item-1": "board" } },
    "kontur:study-checkpoint:v1:exam:exam-1:item:item-1": { runId: "run-1", question: "Первая строка\nВторая строка" },
  } }));
});
test("lost ACK reconciled without a newer edit clears the stale error and needs no duplicate PUT", async () => {
  const h = harness(), store = new LocalClientStateStore(h.io); await store.initialize(); const write = h.io.writeRemote;
  h.io.writeRemote = async (...args) => { await write(...args); throw new Error("response lost"); };
  h.edit(pref("dark")); store.change(h.local()); await assert.rejects(store.flush(), /response lost/);
  assert.ok(store.snapshot().error); h.io.writeRemote = write; await store.flush();
  assert.equal(h.writes.length, 1); assert.equal(store.snapshot().dirty, false); assert.equal(store.snapshot().error, null);
});

test("choosing the backup discards failed local intents instead of replaying them on the next flush", async () => {
  const { CLIENT_STATE_OUTBOX } = load("local-client-state-schema");
  const storage = new MemoryStorage([["kontur:interface-preferences:v1", '{"theme":"dark"}']]);
  const originalWindow = globalThis.window; let remote = { version: 1, value: pref("dark") }, writes = 0, denyJournal = false;
  storage.reject = key => denyJournal && key === CLIENT_STATE_OUTBOX;
  globalThis.window = { localStorage: storage, dispatchEvent: () => true };
  overrides.set("local-fetch", { isLocalDesktop: () => true });
  overrides.set("api", { request: async (path, init) => {
    assert.equal(path, "/local/client-state");
    if (init?.method !== "PUT") return clone(remote);
    writes++; const request = JSON.parse(init.body);
    if (request.expectedVersion !== remote.version) throw Object.assign(new Error("conflict"), { status: 409 });
    remote = { version: remote.version + 1, value: clone(request.value) }; return clone(remote);
  } });
  const bridge = load("local-client-state");
  try {
    await bridge.initializeLocalClientState();
    storage.setItem("kontur:interface-preferences:v1", '{"theme":"light"}'); remote = { version: 2, value: pref("system") };
    await assert.rejects(bridge.flushLocalClientState(), /conflict/); await assert.rejects(bridge.flushLocalClientState(), /конфликт/i);
    assert.equal(bridge.localClientStateSnapshot().conflict, true);
    denyJournal = true; assert.throws(() => storage.setItem("kontur:interface-preferences:v1", '{"theme":"dark"}'), /quota/); denyJournal = false;
    await bridge.resolveLocalClientStateConflict("remote"); await bridge.flushLocalClientState();
    assert.equal(writes, 1); assert.deepEqual(remote, { version: 2, value: pref("system") }); assert.deepEqual(readClientStorage(storage), pref("system"));
    assert.equal(bridge.localClientStateSnapshot().dirty, false); assert.equal(bridge.localClientStateSnapshot().error, null);
  } finally { globalThis.window = originalWindow; overrides.delete("api"); overrides.delete("local-fetch"); }
});
