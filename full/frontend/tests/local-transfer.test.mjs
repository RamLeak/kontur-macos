import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const loaded = { exports: {} };
new Function("module", "exports", ts.transpileModule(await readFile(new URL("../lib/local-transfer.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(loaded, loaded.exports);
const { importSelection, importResult, exportResult } = loaded.exports;
const checksum = "a".repeat(64);
test("full backup counts retain exams, chats and settings rather than a seven-table subset", () => {
  const counts = { tasks: 10, exam_sessions: 3, conversations: 4, sync_records: 8 };
  const value = importSelection({ token: "opaque-native-token", preview: { formatVersion: 2, format: "full", contentSha256: checksum, counts, conflictCount: 1, warnings: ["TEST_ONLY warning"] } });
  assert.deepEqual(value.preview.counts, counts); assert.equal(value.token, "opaque-native-token");
  assert.deepEqual(importResult({ format: "full", contentSha256: checksum, imported: counts, duplicateRecords: 2, warnings: [], backupPath: "private-path" }).imported, counts);
  assert.equal("backupPath" in importResult({ format: "full", contentSha256: checksum, imported: counts, duplicateRecords: 2, warnings: [], backupPath: "private-path" }), false);
  assert.equal("path" in exportResult({ format: "full", contentSha256: checksum, counts, path: "private-path" }), false);
});
test("cancel is distinct from malformed or unconfirmed import preview", () => {
  assert.equal(importSelection(null), null); assert.equal(exportResult(null), null);
  assert.throws(() => importSelection({ token: "opaque", preview: { formatVersion: 1, format: "legacy", contentSha256: checksum, counts: { tasks: -1 }, conflictCount: 0, warnings: [] } }));
  assert.throws(() => importSelection({ preview: {} }));
});
