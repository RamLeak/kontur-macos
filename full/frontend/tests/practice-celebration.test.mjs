import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function harness(reply, { sealed = false, failure = false } = {}) {
  const slots = [], calls = [];
  let cursor = 0, tree, handlers;
  const react = {
    useState(initial) { const cell = slots[cursor++] ??= { value: initial }; return [cell.value, value => { cell.value = typeof value === "function" ? value(cell.value) : value; }]; },
    useRef(value) { return slots[cursor++] ??= { current: value }; },
    useEffect() {},
  };
  const Celebration = function Celebration() {};
  const jsx = (type, props) => ({ type, props });
  const mocks = {
    react, "react/jsx-runtime": { jsx, jsxs: jsx },
    "@/lib/api": { DEMO_MODE: false, request: async (...args) => { calls.push(args); if (failure) throw new Error("synthetic failure"); return reply; } },
    "@/lib/kontur-shortcuts": { useShortcuts: (_scope, value) => { handlers = value; } },
    "@/lib/owner-sync": { useOwnerRecord: () => ({ state: { ready: false, value: {} }, edit() {}, resolve() {} }) },
    "./sync-conflict": { SyncConflict() {} },
    "./correct-answer-celebration": { CorrectAnswerCelebration: Celebration },
    "@/lib/ui-copy": { readableError: (_reason, fallback) => fallback },
  };
  const compiled = ts.transpileModule(readFileSync(new URL("../components/practice-answer.tsx", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", "require", "crypto", compiled)(module, module.exports, id => {
    assert.ok(Object.hasOwn(mocks, id), `Unexpected dependency: ${id}`); return mocks[id];
  }, { randomUUID: () => "synthetic-check-key" });
  const render = () => { cursor = 0; tree = module.exports.PracticeAnswer({ sessionId: "solve-synthetic", task: { id: "task", revision: { id: "revision", content_hash: "hash" } }, attempts: [], sealed, beforeCheck: async () => {} }); };
  const find = predicate => {
    const visit = node => { if (!node || typeof node !== "object") return null; if (predicate(node)) return node; for (const child of [node.props?.children].flat(Infinity)) { const found = visit(child); if (found) return found; } return null; };
    return visit(tree);
  };
  render();
  return {
    calls,
    receipt: () => find(node => node.type === Celebration)?.props.receipt,
    async submit() { find(node => node.type === "input").props.onChange({ target: { value: "42" } }); render(); handlers["answer.check"](); await new Promise(resolve => setImmediate(resolve)); render(); },
    rerender: render,
  };
}

test("fireworks start only from a confirmed correct check, not restoration/render", async () => {
  const ui = harness({ attempt_id: "attempt-confirmed", status: "correct", correct: true, reason: null });
  assert.equal(ui.receipt(), null);
  await ui.submit();
  assert.equal(ui.receipt(), "attempt-confirmed");
  ui.rerender();
  assert.equal(ui.receipt(), "attempt-confirmed");
  assert.equal(ui.calls.length, 1);
});

test("incorrect, unavailable, inconsistent and failed checks never celebrate", async () => {
  for (const reply of [
    { attempt_id: "wrong", status: "incorrect", correct: false },
    { attempt_id: null, status: "unavailable", correct: null },
    { attempt_id: "inconsistent", status: "correct", correct: false },
    { attempt_id: null, status: "correct", correct: true },
  ]) { const ui = harness(reply); await ui.submit(); assert.equal(ui.receipt(), null); }
  const failed = harness(null, { failure: true }); await failed.submit(); assert.equal(failed.receipt(), null);
});

test("sealed work cannot check answers or reveal correctness through fireworks", async () => {
  const ui = harness({ attempt_id: "hidden", status: "correct", correct: true }, { sealed: true });
  await ui.submit(); assert.equal(ui.calls.length, 0); assert.equal(ui.receipt(), null);
});
