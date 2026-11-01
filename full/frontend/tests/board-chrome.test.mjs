import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../components/infinite-board.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
} }).outputText;

// Render/control test only: effects, persistence, canvas and browser layout are outside this seam.
function renderBoard(overrides = {}) {
  const cells = [], preferences = { boardColor: "#242529", boardMarkerColor: "#f4cf58", boardWidth: 3,
    boardMarkerWidth: 18, boardOpacity: 1, boardMarkerOpacity: .35, drawingSmoothing: .5, boardBackground: "plain" };
  let index = 0, nodes = [];
  const jsx = (type, props) => ({ type, props });
  const useState = initial => {
    const cell = cells[index++] ??= { value: typeof initial === "function" ? initial() : initial };
    return [cell.value, value => { cell.value = typeof value === "function" ? value(cell.value) : value; }];
  };
  const unexpectedApi = () => { throw new Error("Chrome controls must not call an external API"); };
  const mocks = {
    react: { useState, useRef: initial => cells[index++] ??= { current: initial }, useId: () => "synthetic-board",
      useCallback: fn => fn, useEffect() {}, useLayoutEffect() {} },
    "react/jsx-runtime": { jsx, jsxs: jsx }, "./modal": { Modal: p => p.open ? jsx("div", { role: "dialog", "aria-label": p.title, children: p.children }) : null },
    "@/components/icon": { Icon: () => null },
    "./infinite-board.module.css": { __esModule: true, default: new Proxy({}, { get: (_, name) => String(name) }) },
    "@/lib/kontur-preferences": { getPreferences: () => preferences, setPreferences: value => Object.assign(preferences, value) },
    "@/lib/api": { saveBoard: unexpectedApi }, "@/lib/device-files": { loadDeviceAssetById: unexpectedApi },
    "@/lib/kontur-shortcuts": { useShortcuts() {} }, "@/lib/types": { ApiError: class extends Error {} },
    "@/lib/ui-copy": { readableError: () => "Synthetic error" },
    "@/lib/local-drafts": {}, "@/lib/draft-lifecycle": {}, "@/lib/board-tools": {},
    "@/lib/stroke-smoothing": {}, "@/lib/statement-ink": {},
  };
  const loaded = { exports: {} };
  new Function("module", "exports", "require", compiled)(loaded, loaded.exports, name => {
    assert.ok(Object.hasOwn(mocks, name), `Declared dependency: ${name}`); return mocks[name];
  });
  const props = { sessionId: "synthetic", initialBoard: { version: 1,
    strokes: [{ id: "existing", kind: "freehand", points: [{ x: 0, y: 0 }, { x: 20, y: 20 }], color: "#242529", width: 3 }],
    texts: [], images: [], viewport: { x: 0, y: 0, zoom: 1 } }, ...overrides };
  function visit(node) {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== "object") return;
    if (typeof node.type === "function") return visit(node.type(node.props));
    nodes.push(node);
    if (node.props.ref) node.props.ref.current = { focus() {} };
    visit(node.props.children);
  }
  function render() { index = 0; nodes = []; visit(loaded.exports.InfiniteBoard(props)); }
  const byLabel = label => nodes.find(node => (node.props["aria-label"] ?? node.props.children) === label);
  render();
  return { preferences, byLabel, nodes: () => nodes,
    click(label) { const node = byLabel(label); assert.ok(node && !node.props.disabled, `Enabled control: ${label}`); node.props.onClick(); render(); },
    change(label, value) { nodes.find(node => node.type === "input" && node.props["aria-label"] === label).props.onChange({ target: { value } }); render(); },
  };
}

test("floating board chrome retains eight selectable named tools and separate navigation", () => {
  const board = renderBoard();
  const rail = board.nodes().find(node => node.props["data-board-chrome"] === "rail");
  const dock = board.nodes().find(node => node.props["data-board-chrome"] === "navigation");
  assert.equal(rail.props["aria-orientation"], "vertical"); assert.ok(dock);
  for (const label of ["Перо", "Маркер", "Ластик", "Линия", "Прямоугольник", "Эллипс", "Текст", "Панорама"]) {
    board.click(label); assert.equal(board.byLabel(label).props["aria-pressed"], true);
    assert.equal(board.byLabel(label).props.title, label);
  }
  for (const label of ["Отменить", "Повторить", "Уменьшить", "Увеличить", "Вернуть масштаб и положение доски"]) assert.ok(board.byLabel(label));
});

test("brush disclosure preserves marker settings and returns to the canvas controls", () => {
  const board = renderBoard(); board.click("Маркер"); board.click("Настройки кисти");
  assert.equal(board.byLabel("Настройки кисти").props["aria-expanded"], true);
  board.change("Толщина штриха", "21"); board.change("Непрозрачность штриха", "0.6");
  assert.equal(board.preferences.boardMarkerWidth, 21); assert.equal(board.preferences.boardMarkerOpacity, .6);
  assert.equal(board.preferences.boardWidth, 3);
  board.click("Закрыть настройки кисти");
  assert.equal(board.byLabel("Настройки кисти").props["aria-expanded"], false);
  assert.equal(board.byLabel("Толщина штриха"), undefined);
  board.click("Ластик"); assert.equal(board.byLabel("Настройки кисти").props.disabled, true);
});

test("board menu keeps all backgrounds and the existing clear confirmation", () => {
  const board = renderBoard(); board.click("Настройки доски");
  for (const label of ["Чистый", "Клетка", "Точки"]) assert.ok(board.byLabel(label));
  board.click("Точки"); assert.equal(board.preferences.boardBackground, "dots");
  board.click("Очистить доску"); assert.ok(board.byLabel("Очистить доску?"));
});

test("disabled and inactive boards cannot open a toolbar panel or change a tool", () => {
  for (const state of [{ disabled: true }, { active: false }]) {
    const board = renderBoard(state);
    for (const label of ["Перо", "Маркер", "Ластик", "Настройки кисти", "Настройки доски", "Уменьшить"]) assert.equal(board.byLabel(label).props.disabled, true);
    assert.equal(board.byLabel("Настройки инструмента"), undefined);
    assert.equal(board.byLabel("Толщина штриха"), undefined);
  }
});
