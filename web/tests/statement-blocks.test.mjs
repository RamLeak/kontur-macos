import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { readStatementBlocks } = await loadTypescript("../lib/statement-blocks.ts");

test("documented legacy table dimensions, latex and subtask text are preserved as data", () => {
  const payload = { statementLatex: "x^2 < y \\land z", table: { rows: 2, cols: 3 }, subtasks: [{ sourceId: "synthetic-child", typeNumber: 27, statement: "<script>not HTML</script> & symbols", assetIds: ["asset-a"] }], legacyRevisionId: "legacy", legacyContentHash: "hash" };
  const parsed = readStatementBlocks(payload);
  assert.deepEqual(parsed.answerTable, { rows: 2, cols: 3 });
  assert.equal(parsed.latex, payload.statementLatex);
  assert.equal(parsed.subtasks[0].statement, payload.subtasks[0].statement);
  assert.deepEqual(parsed.subtasks[0].assetIds, ["asset-a"]);
  assert.equal(parsed.unsupported, false);
});

test("unknown or malformed blocks are disclosed without mutating original data", () => {
  const payload = { table: { rows: "two", cols: 2 }, futureDrawing: { paths: [1, 2] } };
  const before = structuredClone(payload);
  assert.equal(readStatementBlocks(payload).unsupported, true);
  assert.deepEqual(payload, before);
  assert.equal(readStatementBlocks([{ future: "format" }]).unsupported, true);
});

test("canonical and legacy-alias matrices retain their actual cell content", () => {
  for (const table of [{ headers: ["A"], rows: [["cell"]] }, { columns: ["A"], data: [["cell"]] }]) {
    assert.deepEqual(readStatementBlocks({ table }).matrix, { headers: ["A"], rows: [["cell"]] });
    assert.equal(readStatementBlocks({ table }).unsupported, false);
  }
});
