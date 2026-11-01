import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";

const { SaveQueue } = await loadTypescript("../lib/save-queue.ts");
const initial = { id: "document-a", revision: 4, updatedAt: 1, code: "" };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test("typing updates immediately; concurrent flushes await serialized latest edits", async () => {
  const first = deferred();
  const calls = [];
  const queue = new SaveQueue(initial, async (value, revision) => {
    calls.push({ value, revision });
    if (calls.length === 1) await first.promise;
    return { ...value, revision: revision + 1, updatedAt: revision + 1 };
  }, () => {}, 60_000);
  queue.edit({ code: "first" });
  assert.equal(queue.state.value.code, "first");
  const flush = queue.flush();
  queue.edit({ code: "latest" });
  assert.equal(queue.state.value.code, "latest");
  assert.equal(queue.flush(), flush);
  first.resolve();
  const saved = await flush;
  assert.equal(saved.code, "latest");
  assert.equal(saved.revision, 6);
  assert.deepEqual(calls.map(c => [c.value.code, c.revision]), [["first", 4], ["latest", 5]]);
  assert.equal(queue.state.dirty, false);
  queue.dispose();
});

test("failed disk save retains draft and rejects navigation barrier; retry recovers", async () => {
  let fail = true;
  const queue = new SaveQueue(initial, async (value, revision) => {
    if (fail) throw { code: "DISK_FULL", message: "Недостаточно места" };
    return { ...value, revision: revision + 1, updatedAt: 10 };
  }, () => {}, 60_000);
  queue.edit({ code: "must survive" });
  await assert.rejects(queue.flush(), { code: "DISK_FULL" });
  assert.equal(queue.state.value.code, "must survive");
  assert.equal(queue.state.dirty, true);
  assert.equal(queue.state.value.revision, 4);
  queue.edit({ code: "latest after failure" });
  fail = false;
  const saved = await queue.flush();
  assert.equal(saved.code, "latest after failure");
  assert.equal(saved.revision, 5);
  assert.equal(queue.state.error, null);
  queue.dispose();
});

test("conflict does not replace the local draft or claim durable success", async () => {
  const queue = new SaveQueue(initial, async () => { throw { code: "CONFLICT", conflictRevision: 9 }; }, () => {}, 60_000);
  queue.edit({ code: "local conflict" });
  await assert.rejects(queue.flush(), { code: "CONFLICT" });
  assert.equal(queue.state.value.code, "local conflict");
  assert.equal(queue.state.dirty, true);
  assert.equal(queue.state.value.revision, 4);
  queue.dispose();
});

test("stale or wrong-document acknowledgement cannot unlock close", async () => {
  for (const saved of [{ ...initial }, { ...initial, id: "other", revision: 5 }]) {
    const queue = new SaveQueue(initial, async () => saved, () => {}, 60_000);
    queue.edit({ code: "new" });
    await assert.rejects(queue.flush(), /INVALID_STORAGE_ACK/);
    assert.equal(queue.state.dirty, true);
    queue.dispose();
  }
});
