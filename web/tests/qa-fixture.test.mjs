import { test } from "node:test";
import assert from "node:assert/strict";
import { installTestBridge } from "../../qa/ui-test-bridge.mjs";

test("TEST_ONLY fixture refuses to replace native IPC and labels every seeded task", async () => {
  assert.throws(() => installTestBridge({ __TAURI__: {} }), /must not replace native/);
  const target = { performance };
  const control = installTestBridge(target, { seedCatalog: true });
  assert.ok(control.snapshot().tasks.every(task => task.sourceId.startsWith("TEST_ONLY") && task.title.includes("TEST_ONLY")));
  assert.equal(control.commands.some(item => item.command.startsWith("codex_")), false);
  const status = await target.__KONTUR_LOCAL_TEST_ONLY__.invoke("codex_status");
  assert.equal(status.state, "unavailable");
});

test("TEST_ONLY close check rejects a document revision that storage did not acknowledge", async () => {
  const target = { performance };
  const control = installTestBridge(target);
  const invoke = target.__KONTUR_LOCAL_TEST_ONLY__.invoke;
  target.__KONTUR_LOCAL_REQUEST_CLOSE__ = async ({ nonce }) => invoke("confirm_close", { nonce, savedDocuments: [{ documentId: "TEST_ONLY-document-1", revision: 999 }], allFlushed: true });
  await assert.rejects(control.requestClose(), { code: "close_rejected" });
});
