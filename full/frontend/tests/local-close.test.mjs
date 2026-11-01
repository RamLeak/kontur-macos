import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const loaded = { exports: {} };
new Function("module", "exports", ts.transpileModule(await readFile(new URL("../lib/local-close.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(loaded, loaded.exports);
const { performLocalClose } = loaded.exports;
const clean = () => ({ dirty: false, saving: false, conflict: false, ready: true });
const deferred = () => { let resolve; const promise = new Promise(value => { resolve = value; }); return { promise, resolve }; };

test("full close waits for native mutations and actual debounced saves before its checkpoint request", async () => {
  const disk = deferred(); const order = [];
  const operation = performLocalClose("nonce", { mutations: async () => { order.push("mutations"); }, flush: async () => { order.push("flush"); await disk.promise; return { serverConfirmed: true, state: clean() }; }, read: clean, confirm: async nonce => { assert.equal(nonce, "nonce"); order.push("confirm"); } });
  await new Promise(resolve => setImmediate(resolve)); assert.deepEqual(order, ["mutations", "flush"]);
  disk.resolve(); await operation; assert.deepEqual(order, ["mutations", "flush", "confirm"]);
});
test("a failed save, dirty/conflicting draft, unready owner or local-only recovery never confirms close", async () => {
  for (const kind of ["failure", "dirty", "conflict", "saving", "unready", "recovery"]) {
    let confirmed = false; const state = clean();
    if (kind in state) state[kind] = true;
    if (kind === "unready") state.ready = false;
    await assert.rejects(performLocalClose("nonce", { mutations: async () => {}, flush: async () => { if (kind === "failure") throw new Error("disk failed"); return { serverConfirmed: kind !== "recovery", localRecoverable: true, state }; }, read: () => state, confirm: async () => { confirmed = true; } }));
    assert.equal(confirmed, false, kind);
  }
});
