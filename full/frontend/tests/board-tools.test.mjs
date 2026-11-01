import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

async function loadBoardTools() {
  const source = await readFile(join(root, "lib/board-tools.ts"), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", transpiled)(module, module.exports);
  return module.exports;
}

test("board shortcuts use physical codes and Russian layout aliases", async () => {
  const { resolveBoardShortcut } = await loadBoardTools();
  assert.equal(resolveBoardShortcut({ code: "KeyP", key: "з" }), "pen");
  assert.equal(resolveBoardShortcut({ code: "Digit2", key: "@" }), "pen");
  assert.equal(resolveBoardShortcut({ code: "Numpad2", key: "2" }), "pen");
  assert.equal(resolveBoardShortcut({ code: "KeyM", key: "ь" }), "marker");
  assert.equal(resolveBoardShortcut({ code: "KeyH", key: "р" }), "marker");
  assert.equal(resolveBoardShortcut({ code: "Digit3", key: "#" }), "marker");
  assert.equal(resolveBoardShortcut({ code: "Numpad3", key: "3" }), "marker");
  assert.equal(resolveBoardShortcut({ key: "h" }), "marker");
  assert.equal(resolveBoardShortcut({ code: "KeyV", key: "м" }), "pan");
  assert.equal(resolveBoardShortcut({ code: "KeyO", key: "щ" }), "ellipse");
  assert.equal(resolveBoardShortcut({ key: "у" }), "eraser");
  assert.equal(resolveBoardShortcut({ code: "KeyP", key: "з", shiftKey: true }), null);
  assert.equal(resolveBoardShortcut({ code: "KeyP", key: "з", altKey: true }), null);
});

test("undo and redo require the platform modifier and preserve Shift+Z redo", async () => {
  const { resolveBoardShortcut } = await loadBoardTools();
  assert.equal(resolveBoardShortcut({ code: "KeyZ", key: "я", ctrlKey: true }), "undo");
  assert.equal(resolveBoardShortcut({ code: "KeyZ", key: "я", metaKey: true, shiftKey: true }), "redo");
  assert.equal(resolveBoardShortcut({ code: "KeyZ", key: "я" }), null);
  assert.equal(resolveBoardShortcut({ code: "KeyZ", key: "я", ctrlKey: true, altKey: true }), null);
});

test("stroke styles keep pencil thin/opaque and marker thick/translucent", async () => {
  const { getStrokeStyle, isBoardSpaceKey, strokeOpacity } = await loadBoardTools();
  const pencil = getStrokeStyle("pen", "#242529", 2);
  const marker = getStrokeStyle("marker", "#f2c94c", 2);
  assert.deepEqual(pencil, { kind: "freehand", color: "#242529", width: 1.5, alpha: 1 });
  assert.deepEqual(marker, { kind: "marker", color: "#f2c94c", width: 9, alpha: 0.42 });
  assert.ok(marker.width > pencil.width);
  assert.equal(strokeOpacity("marker"), 0.42);
  assert.equal(strokeOpacity("freehand"), 1);
  assert.equal(isBoardSpaceKey({ code: "Space", key: " " }), true);
  assert.equal(isBoardSpaceKey({ code: "KeyV", key: "м" }), false);
});
test("stroke opacity preserves stored values, legacy fallback, and preference clamping", async () => {
  const { strokeOpacity, strokeOpacityForPreference } = await loadBoardTools();
  assert.equal(strokeOpacity("marker"), 0.42);
  assert.equal(strokeOpacity("freehand"), 1);
  assert.equal(strokeOpacity("marker", 0.21), 0.21);
  assert.equal(strokeOpacity("marker", 0.01), 0.05);
  assert.equal(strokeOpacity("freehand", 2), 1);
  assert.equal(strokeOpacity("freehand", Number.NaN), 1);
  assert.equal(strokeOpacityForPreference("marker", 0.5), 0.21);
  assert.equal(strokeOpacityForPreference("freehand", 0.5), 0.5);
  assert.equal(strokeOpacityForPreference("marker", Number.NaN), 0.42);
  assert.equal(strokeOpacityForPreference("freehand", 0), 0.05);
});
