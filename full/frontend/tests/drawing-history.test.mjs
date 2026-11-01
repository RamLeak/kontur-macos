import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

async function loadTypeScript(path) {
  const source = await readFile(join(root, path), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", transpiled)(module, module.exports);
  return module.exports;
}

test("drawing history treats a completed gesture as one undo and redo step", async () => {
  const { pushDrawingHistory, undoDrawingHistory, redoDrawingHistory } = await loadTypeScript("lib/drawing-history.ts");
  let state = { history: [], redo: [] };
  state = pushDrawingHistory(state, ["before"], ["after"]);
  const undone = undoDrawingHistory(state, ["after"]);
  assert.deepEqual(undone?.value, ["before"]);
  const redone = redoDrawingHistory(undone.state, ["before"]);
  assert.deepEqual(redone?.value, ["after"]);
});

test("smooth round strokes use quadratic interpolation and preserve a single point", async () => {
  const { compactStrokePoints, drawSmoothRoundStroke } = await loadTypeScript("lib/stroke-smoothing.ts");
  const calls = [];
  const context = {
    beginPath: () => calls.push("begin"),
    moveTo: (...args) => calls.push(["move", ...args]),
    lineTo: (...args) => calls.push(["line", ...args]),
    quadraticCurveTo: (...args) => calls.push(["quad", ...args]),
    arc: (...args) => calls.push(["arc", ...args]),
    stroke: () => calls.push("stroke"),
  };
  drawSmoothRoundStroke(context, [{ x: 0, y: 0 }, { x: 4, y: 2 }, { x: 8, y: 0 }]);
  assert.ok(calls.some((call) => Array.isArray(call) && call[0] === "quad"));
  calls.length = 0;
  drawSmoothRoundStroke(context, [{ x: 1, y: 1 }]);
  assert.ok(calls.some((call) => Array.isArray(call) && call[0] === "arc"));
  assert.deepEqual(compactStrokePoints([{ x: 0, y: 0 }, { x: 0, y: 0.00001 }, { x: 1, y: 1 }], 0.001), [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
});

test("stroke smoothing preference controls the renderer without changing points", async () => {
  const { drawSmoothRoundStroke } = await loadTypeScript("lib/stroke-smoothing.ts");
  const calls = [];
  const context = {
    beginPath: () => calls.push("begin"),
    moveTo: (...args) => calls.push(["move", ...args]),
    lineTo: (...args) => calls.push(["line", ...args]),
    quadraticCurveTo: (...args) => calls.push(["quad", ...args]),
    arc: (...args) => calls.push(["arc", ...args]),
    stroke: () => calls.push("stroke"),
  };
  const points = [{ x: 0, y: 0 }, { x: 4, y: 2 }, { x: 8, y: 0 }];
  drawSmoothRoundStroke(context, points, 0);
  assert.equal(calls.filter((call) => Array.isArray(call) && call[0] === "line").length, 2);
  assert.equal(calls.some((call) => Array.isArray(call) && call[0] === "quad"), false);
  calls.length = 0;
  drawSmoothRoundStroke(context, points, 1);
  assert.ok(calls.some((call) => Array.isArray(call) && call[0] === "quad"));
});
