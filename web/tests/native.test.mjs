import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { invokeNative, safeError } = await loadTypescript("../lib/native.ts");

test("production browser never falls back to an injected QA bridge", async () => {
  const before = process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY;
  delete process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY;
  let called = false;
  globalThis.window = { __KONTUR_LOCAL_TEST_ONLY__: { invoke: async () => { called = true; return {}; } } };
  await assert.rejects(invokeNative("load_snapshot"), { code: "native_unavailable" });
  assert.equal(called, false);
  if (before === undefined) delete process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY; else process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY = before;
  delete globalThis.window;
});

test("only an explicit TEST_ONLY flag enables browser QA data", async () => {
  const before = process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY;
  process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY = "1";
  globalThis.window = { __KONTUR_LOCAL_TEST_ONLY__: { invoke: async (command, args) => ({ command, args }) } };
  assert.deepEqual(await invokeNative("load_document", { documentId: "native-id" }), { command: "load_document", args: { documentId: "native-id" } });
  if (before === undefined) delete process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY; else process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY = before;
  delete globalThis.window;
});

test("real IPC preserves fixed conflict information and hides unstructured diagnostics", async () => {
  globalThis.window = { __TAURI__: { core: { invoke: async () => { throw { code: "revision_conflict", message: "Конфликт версии", conflictRevision: 12 }; } } } };
  await assert.rejects(invokeNative("save_document", {}), { code: "revision_conflict", conflictRevision: 12 });
  const error = safeError(new Error("private database path and raw stderr"), "Не удалось сохранить");
  assert.equal(error.message, "Не удалось сохранить");
  delete globalThis.window;
});
