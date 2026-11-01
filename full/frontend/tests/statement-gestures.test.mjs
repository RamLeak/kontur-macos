import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const compiled = new Map();
function source(name) {
  if (!compiled.has(name)) compiled.set(name, ts.transpileModule(readFileSync(new URL(`../${name}`, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText);
  return compiled.get(name);
}
function eventTarget() {
  const listeners = new Map();
  return { addEventListener: (name, fn) => listeners.set(name, new Set([...(listeners.get(name) ?? []), fn])),
    removeEventListener: (name, fn) => listeners.get(name)?.delete(fn), emit: name => [...(listeners.get(name) ?? [])].forEach(fn => fn()) };
}
function harness({ failDeletes = false } = {}) {
  const slots = [], elements = new Map(), frames = new Map(), modules = new Map();
  let index = 0, dirty = true, tree, nodes = [], effects = [], frameId = 0, created = 0;
  const deleted = [], posted = [], store = new Map(["A", "B"].map((id, i) => [id, {
    id, kind: "ink", color: "#111111", points: [{ x: .15 + .6 * i, y: .2 }, { x: .25 + .6 * i, y: .2 }],
    anchor: { start: 0, end: 0, content_hash: "revision" },
  }]));
  const win = Object.assign(eventTarget(), { devicePixelRatio: 1, innerWidth: 1000, innerHeight: 800 });
  const doc = Object.assign(eventTarget(), { body: {}, caretRangeFromPoint: () => null });
  const context = { painted: 0, clearRect() { this.painted = 0; }, stroke() { this.painted++; },
    setTransform() {}, save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {}, quadraticCurveTo() {}, arc() {} };
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const slot = init => slots[index++] ??= init();
  const react = {
    useState(initial) { const cell = slot(() => ({ value: typeof initial === "function" ? initial() : initial })); return [cell.value, value => {
      const next = typeof value === "function" ? value(cell.value) : value;
      if (!Object.is(cell.value, next)) { cell.value = next; dirty = true; }
    }]; },
    useRef: initial => slot(() => ({ current: initial })),
    useMemo(fn, deps) { const cell = slot(() => ({})); if (!same(cell.deps, deps)) { cell.value = fn(); cell.deps = deps; } return cell.value; },
    useCallback(fn, deps) { return react.useMemo(() => fn, deps); },
    useEffect(fn, deps) { const cell = slot(() => ({})); if (!same(cell.deps, deps)) {
      cell.deps = deps; effects.push(() => { cell.cleanup?.(); cell.cleanup = fn(); });
    } },
  };
  react.useLayoutEffect = react.useEffect;
  const jsx = (type, props) => ({ type, props });
  const mocks = {
    react, "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: Symbol("fragment") }, "react-dom": { createPortal: node => node },
    "@/components/icon": { Icon: () => null },
    "@/lib/kontur-preferences": { getPreferences: () => ({ drawingSmoothing: .5 }), PREFERENCES_CHANGED: "preferences" },
    "@/lib/local-drafts": { readLocalDraft: () => null, saveLocalDraft() {}, clearLocalDraft() {}, isLocalDraftDurable: () => true },
    "@/lib/draft-lifecycle": { registerDraftFlush: () => () => {}, registerDraftSaveState: () => () => {} },
    "./statement-canvas.module.css": { __esModule: true, default: new Proxy({}, { get: (_, name) => String(name) }) },
  };
  function load(name) {
    if (modules.has(name)) return modules.get(name);
    const module = { exports: {} };
    const require = id => {
      if (Object.hasOwn(mocks, id)) return mocks[id];
      if (["@/lib/statement-ink", "@/lib/stroke-smoothing"].includes(id)) return load(id.replace("@/", "") + ".ts");
      throw new Error(`Unexpected component dependency: ${id}`);
    };
    new Function("module", "exports", "require", "window", "document", "navigator", "requestAnimationFrame", "cancelAnimationFrame", "crypto", source(name))(
      module, module.exports, require, win, doc, { onLine: true }, fn => { frames.set(++frameId, fn); return frameId; }, id => frames.delete(id), { randomUUID: () => `client-${++created}` });
    modules.set(name, module.exports); return module.exports;
  }
  const Component = load("components/statement-canvas.tsx").StatementCanvas;
  const props = { children: "Synthetic statement", revisionHash: "revision", recoveryScope: "synthetic", annotations: [...store.values()],
    onInk: async annotation => { posted.push(annotation); const saved = { ...annotation, id: `server-${annotation.id}` }; store.set(saved.id, saved); update(); return saved; },
    onDeleteInk: async id => { deleted.push(id); if (failDeletes) throw new Error("Synthetic delete failure"); store.delete(id); update(); },
  };
  function update() { props.annotations = [...store.values()]; dirty = true; }
  function attach(node, path = "root") {
    if (Array.isArray(node)) { node.forEach((child, i) => attach(child, `${path}.${i}`)); return; }
    if (!node || typeof node !== "object") return;
    let element = elements.get(path);
    if (!element) {
      const captures = new Set(), classes = new Set();
      element = { style: {}, ownerDocument: doc, focus() {}, querySelector: () => null, getContext: () => context,
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 400 }),
        setPointerCapture: id => captures.add(id), hasPointerCapture: id => captures.has(id), releasePointerCapture: id => captures.delete(id),
        classList: { add: name => classes.add(name), remove: name => classes.delete(name) } };
      elements.set(path, element);
    }
    node.element = element; element.props = node.props; nodes.push(node);
    if (node.props?.ref) node.props.ref.current = element;
    attach(node.props?.children, `${path}.children`);
  }
  function flush() {
    for (let turns = 0; turns < 50; turns++) {
      if (dirty) { dirty = false; index = 0; nodes = []; tree = Component(props); attach(tree); }
      effects.splice(0).forEach(fn => fn()); const ready = [...frames.values()]; frames.clear(); ready.forEach(fn => fn());
      if (!dirty && !effects.length && !frames.size) return;
    }
    throw new Error("Synthetic hook renderer did not settle");
  }
  const button = label => nodes.find(node => node.type === "button" && (node.props["aria-label"] ?? node.props.children) === label);
  function pointer(handler, x, y, id = 1) {
    const canvas = nodes.find(node => node.type === "canvas");
    canvas.props[handler]({ currentTarget: canvas.element, pointerId: id, pointerType: "mouse", button: 0, clientX: x, clientY: y, shiftKey: false }); flush();
  }
  flush();
  return { deleted, posted, store, button, pointer, win, painted: () => context.painted,
    click(label) { const target = button(label); assert.ok(target && !target.props.disabled, `Enabled button: ${label}`); target.props.onClick(); flush(); },
    enable() { const toggle = nodes.find(node => node.type === "button" && node.props.onClick && node.props["aria-pressed"] === false); toggle.props.onClick(); flush(); },
    recover() { failDeletes = false; },
    async settle() { for (let i = 0; i < 4; i++) { await new Promise(setImmediate); flush(); } },
  };
}
const undoLabel = "Отменить последнюю сохранённую пометку", cancelLabel = "Стереть последний черновик";
async function ready(options) { const h = harness(options); await h.settle(); return h; }
function drag(h) { h.enable(); h.click("Ластик"); h.pointer("onPointerDown", 80, 80); h.pointer("onPointerMove", 320, 80); h.pointer("onPointerUp", 320, 80); }

test("one statement eraser drag deletes two marks and one undo restores both", async () => {
  const h = await ready(); drag(h); await h.settle();
  assert.deepEqual(h.deleted, ["A", "B"]); assert.equal(h.store.size, 0);
  h.click(undoLabel); await h.settle();
  assert.equal(h.posted.length, 2); assert.equal(h.store.size, 2);
  assert.deepEqual([...h.store.values()].map(item => item.points[0].x).sort(), [.15, .75]);
});
test("one retry retries every failed deletion in the eraser group", async () => {
  const h = await ready({ failDeletes: true }); drag(h); await h.settle();
  assert.deepEqual(h.deleted, ["A", "B"]); h.recover(); h.click("Повторить удаление"); await h.settle();
  assert.deepEqual([...h.deleted].sort(), ["A", "A", "B", "B"]); assert.equal(h.store.size, 0);
  assert.equal(h.button("Повторить удаление"), undefined);
});
test("blur and lost capture release the eraser pointer for a new gesture", async () => {
  for (const interruption of ["blur", "capture"]) {
    const h = await ready(); h.enable(); h.click("Ластик"); h.pointer("onPointerDown", 80, 80);
    if (interruption === "blur") h.win.emit("blur"); else h.pointer("onLostPointerCapture", 80, 80);
    h.pointer("onPointerDown", 320, 80, 2); h.pointer("onPointerUp", 320, 80, 2); await h.settle();
    assert.deepEqual(h.deleted, ["A", "B"], interruption);
  }
});
test("the draft cancel button keeps its DOM slot throughout a pen stroke", async () => {
  const h = await ready(); h.enable(); const before = h.button(cancelLabel);
  assert.ok(before); assert.equal(before.props["aria-hidden"], true);
  h.pointer("onPointerDown", 80, 160); const drawing = h.button(cancelLabel);
  assert.equal(drawing.element, before.element); assert.equal(drawing.props["aria-hidden"], false);
  h.pointer("onPointerCancel", 80, 160);
  assert.equal(h.button(cancelLabel).element, before.element); assert.equal(h.button(cancelLabel).props["aria-hidden"], true);
  await h.settle(); assert.equal(h.posted.length, 0);
});
test("blur cancels the current draft without repainting an obsolete saved-ink snapshot", async () => {
  const h = await ready(); h.enable(); h.pointer("onPointerDown", 80, 160);
  h.pointer("onPointerMove", 120, 160); h.pointer("onPointerUp", 120, 160); await h.settle();
  assert.equal(h.store.size, 3); assert.equal(h.painted(), 3);
  h.pointer("onPointerDown", 200, 160); h.win.emit("blur"); await h.settle();
  assert.equal(h.store.size, 3); assert.equal(h.painted(), 3);
});
