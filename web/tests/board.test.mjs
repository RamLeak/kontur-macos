import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { emptyBoard, parseBoard } = await loadTypescript("../lib/board-types.ts");
const { registerSurfaceFlush, captureSurfaces, flushSurfaces } = await loadTypescript("../lib/surface-lifecycle.ts");

test("empty native board opens without invented content", () => {
  assert.deepEqual(parseBoard({}, "backend-doc"), emptyBoard("backend-doc"));
  assert.deepEqual(emptyBoard("backend-doc").strokes, []);
});

test("recognized legacy board keeps content and binds to its copied document", () => {
  const legacy = { id: "old-session", version: 12, strokes: [{ id: "stroke", points: [{ x: 3, y: 5 }, { x: 7, y: 9 }], color: "#123456", width: 3 }], texts: [{ id: "text", x: 2, y: 3, text: "Сохранить" }], images: [], viewport: { x: 10, y: -20, zoom: 1.2 }, updated_at: "2026-10-29T10:00:00.000Z" };
  const board = parseBoard(legacy, "new-backend-document");
  assert.equal(board.id, "new-backend-document");
  assert.deepEqual(board.strokes, legacy.strokes);
  assert.deepEqual(board.texts, legacy.texts);
  assert.deepEqual(board.viewport, legacy.viewport);
  assert.equal(board.updated_at, Date.UTC(2026, 9, 29, 10));
  assert.equal(legacy.id, "old-session");
  assert.equal(legacy.updated_at, "2026-10-29T10:00:00.000Z");
});

test("unknown populated board is reported without erasing or mutating payload", () => {
  const unknown = { foreignVersion: 3, paths: [{ x: 1 }] };
  const before = structuredClone(unknown);
  assert.throws(() => parseBoard(unknown, "doc"), { code: "BOARD_UNSUPPORTED" });
  assert.deepEqual(unknown, before);
  assert.throws(() => parseBoard({ strokes: [{ id: "bad", points: [{ x: Infinity, y: 1 }], color: "#000000", width: 1 }] }, "doc"), { code: "BOARD_UNSUPPORTED" });
});

test("surface capture preserves transient text; a rejected surface flush remains a failure", async () => {
  let text = "editing"; let captured = "";
  const unregister = registerSurfaceFlush(async () => { captured = text; throw new Error("disk failed"); }, () => { captured = text; });
  captureSurfaces(); assert.equal(captured, "editing");
  text = "latest";
  await assert.rejects(flushSurfaces(), /disk failed/);
  assert.equal(captured, "latest");
  unregister();
  await flushSurfaces();
});
