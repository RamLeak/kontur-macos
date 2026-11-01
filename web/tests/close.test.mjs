import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { acknowledgeClose } = await loadTypescript("../lib/close.ts");
const deferred = () => { let resolve; const promise = new Promise(a => { resolve = a; }); return { promise, resolve }; };
const saved = (id, revision) => ({ value: { id, revision, updatedAt: 1 }, dirty: false, saving: false, error: null });

test("close waits for pending navigation and every disk acknowledgement before confirming nonce", async () => {
  const write = deferred(); const order = [];
  const operation = acknowledgeClose("native-nonce", {
    waitForPending: async () => { order.push("pending"); },
    flush: async () => { order.push("flush"); await write.promise; order.push("committed"); },
    documents: () => [saved("a", 4), saved("b", 12)],
    confirm: async (nonce, documents) => { order.push("confirm"); assert.equal(nonce, "native-nonce"); assert.deepEqual(documents, [{ documentId: "a", revision: 4 }, { documentId: "b", revision: 12 }]); },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(order, ["pending", "flush"]);
  write.resolve(); await operation;
  assert.deepEqual(order, ["pending", "flush", "committed", "confirm"]);
});

test("flush error, conflict, dirty work and empty storage never acknowledge close", async () => {
  for (const kind of ["disk", "conflict", "dirty", "saving", "empty"]) {
    let confirmed = false;
    const state = saved("a", 4);
    if (kind === "dirty") state.dirty = true;
    if (kind === "saving") state.saving = true;
    if (kind === "conflict") state.error = { code: "revision_conflict" };
    await assert.rejects(acknowledgeClose("nonce", {
      waitForPending: async () => {}, flush: async () => { if (kind === "disk") throw new Error("disk failed"); },
      documents: () => kind === "empty" ? [] : [state], confirm: async () => { confirmed = true; },
    }));
    assert.equal(confirmed, false, kind);
  }
});
