import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);

async function loadShortcuts() {
  const source = await readFile(join(root, "lib/kontur-shortcuts.ts"), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", "require", transpiled)(module, module.exports, require);
  return module.exports;
}

test("registry exposes stable commands and independent platform defaults", async () => {
  const shortcuts = await loadShortcuts();
  const ids = shortcuts.COMMANDS.map((command) => command.id);
  for (const id of ["assistant.toggle", "panel.toggle", "focus.toggle", "surface.toggle", "code.run", "answer.check", "task.previous", "task.next", "task.bookmark", "board.pen", "board.marker", "board.pan", "board.line", "board.eraser", "board.undo", "board.redo"]) {
    assert.ok(ids.includes(id), `missing command ${id}`);
  }
  assert.equal(shortcuts.getShortcutProfile("windows")["assistant.toggle"], "Ctrl+Alt+KeyA");
  assert.equal(shortcuts.getShortcutProfile("macos")["assistant.toggle"], null);
  assert.equal(shortcuts.getShortcutProfile("macos")["code.run"], "Cmd+Enter");
});

test("physical bindings match Russian layout aliases and AltGraph is not Ctrl+Alt", async () => {
  const shortcuts = await loadShortcuts();
  assert.equal(shortcuts.matchesShortcut({ code: "KeyP", key: "з" }, "KeyP"), true);
  assert.equal(shortcuts.matchesShortcut({ key: "з" }, "KeyP"), true);
  assert.equal(shortcuts.matchesShortcut({ key: "б" }, "Comma"), true);
  assert.equal(shortcuts.matchesShortcut({ code: "KeyA", key: "ф", ctrlKey: true, altKey: true, getModifierState: (name) => name === "AltGraph" }, "Ctrl+Alt+KeyA"), false);
  assert.equal(shortcuts.getShortcutFromKeyboardEvent({ code: "KeyP", key: "з" }), "KeyP");
  assert.equal(shortcuts.getShortcutFromKeyboardEvent({ code: "KeyP", key: "з", ctrlKey: true }), "Ctrl+KeyP");
});

test("recorder capture reads a native event before preventDefault flips it", async () => {
  const shortcuts = await loadShortcuts();
  const event = {
    key: "з",
    code: "KeyP",
    ctrlKey: true,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
  };
  assert.equal(shortcuts.captureShortcutFromKeyboardEvent(event), "Ctrl+KeyP");
  assert.equal(event.defaultPrevented, true);
  assert.equal(event.propagationStopped, true);
});

test("scope conflicts include global/workspace overlap but keep editor and canvas exclusive", async () => {
  const shortcuts = await loadShortcuts();
  assert.equal(shortcuts.scopesOverlap("workspace", "editor"), true);
  assert.equal(shortcuts.scopesOverlap("editor", "canvas"), false);
  assert.ok(shortcuts.findShortcutConflicts("windows", "code.run", "Ctrl+Alt+KeyA").some((command) => command.id === "assistant.toggle"));
  assert.equal(shortcuts.findShortcutConflicts("windows", "board.pen", "Ctrl+Enter").some((command) => command.id === "code.run"), false);
});

test("text inputs allow modified app chords but preserve native editing and recorder blocking", async () => {
  const shortcuts = await loadShortcuts();
  const input = { tagName: "TEXTAREA", isContentEditable: false, hasAttribute: () => false, closest: () => null };
  const editable = { tagName: "DIV", isContentEditable: false, hasAttribute: () => false, closest: (selector) => selector === "[contenteditable]" ? {} : null };
  const recorder = { tagName: "INPUT", isContentEditable: false, hasAttribute: (name) => name === "data-shortcut-recorder", closest: () => null };
  const event = (overrides = {}) => ({ target: input, key: "a", code: "KeyA", ...overrides });

  const ctrlAltA = event({ ctrlKey: true, altKey: true, key: "ф" });
  assert.equal(shortcuts.isShortcutTargetBlocked(ctrlAltA, "Ctrl+Alt+KeyA"), false);
  assert.equal(shortcuts.matchesShortcut(ctrlAltA, "Ctrl+Alt+KeyA"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ ctrlKey: true, shiftKey: true, code: "KeyP", key: "p" }), "Ctrl+Shift+KeyP"), false);
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ metaKey: true, shiftKey: true, code: "KeyP", key: "p" }), "Cmd+Shift+KeyP"), false);
  assert.equal(shortcuts.matchesShortcut({ ...ctrlAltA, getModifierState: (name) => name === "AltGraph" }, "Ctrl+Alt+KeyA"), false);

  assert.equal(shortcuts.isShortcutTargetBlocked(event({ code: "KeyP", key: "p" }), "KeyP"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ shiftKey: true, code: "KeyP", key: "P" }), "Shift+KeyP"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ code: "Space", key: " " }), "Space"), true);
  for (const key of ["C", "V", "X", "A", "Z"]) {
    assert.equal(shortcuts.isShortcutTargetBlocked(event({ ctrlKey: true, code: `Key${key}`, key: key.toLowerCase() }), `Ctrl+Key${key}`), true, `Ctrl+${key} remains native in text inputs`);
    assert.equal(shortcuts.isShortcutTargetBlocked(event({ metaKey: true, code: `Key${key}`, key: key.toLowerCase() }), `Cmd+Key${key}`), true, `Cmd+${key} remains native in text inputs`);
  }
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ altKey: true, code: "KeyP", key: "p" }), "Alt+KeyP"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked(event({ ctrlKey: true, code: "Enter", key: "Enter" }), "Ctrl+Enter"), false);
  assert.equal(shortcuts.isShortcutTargetBlocked({ target: editable, code: "KeyP", key: "p" }, "KeyP"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked({ target: recorder }, "Ctrl+Enter"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked({ ...ctrlAltA, target: recorder }, "Ctrl+Alt+KeyA"), true);
});

test("canvas-scoped shortcuts never run from text input targets", async () => {
  const shortcuts = await loadShortcuts();
  const source = await readFile(join(root, "lib/kontur-shortcuts.ts"), "utf8");
  const input = { tagName: "TEXTAREA", isContentEditable: false, hasAttribute: () => false, closest: () => null };
  assert.match(source, /scope === "canvas" && isTextInputTarget\(event\.target\)/);
  assert.equal(shortcuts.isShortcutTargetBlocked({ target: input, key: "p", code: "KeyP" }, "KeyP"), true);
  assert.equal(shortcuts.isShortcutTargetBlocked({ target: input, key: "z", code: "KeyZ", ctrlKey: true }, "Ctrl+KeyZ"), true);
});

test("reserved operating-system chords are rejected while clear remains allowed", async () => {
  const shortcuts = await loadShortcuts();
  assert.equal(shortcuts.isReservedShortcut("windows", "Ctrl+KeyL"), true);
  assert.equal(shortcuts.isReservedShortcut("windows", "Alt+Tab"), true);
  assert.equal(shortcuts.isReservedShortcut("windows", "F11"), true);
  assert.equal(shortcuts.isReservedShortcut("macos", "F11"), true);
  assert.equal(shortcuts.isReservedShortcut("macos", "Ctrl+Alt+KeyA"), true);
  assert.equal(shortcuts.isReservedShortcut("macos", "Cmd+Tab"), true);
  assert.equal(shortcuts.isReservedShortcut("windows", "Ctrl+Alt+Enter"), false);
  assert.equal(shortcuts.formatShortcut(null, "windows"), "Не назначено");
  assert.equal(shortcuts.formatShortcut("Cmd+Shift+KeyZ", "macos"), "⇧⌘Z");
});

test("bare Escape stays reserved for shell navigation and capture ignores composition/repeat", async () => {
  const shortcuts = await loadShortcuts();
  assert.equal(shortcuts.isReservedShortcut("windows", "Escape"), true);
  assert.equal(shortcuts.isReservedShortcut("macos", "Escape"), true);
  assert.equal(shortcuts.getShortcutFromKeyboardEvent({ key: "Escape", code: "Escape" }), "Escape");
  assert.equal(shortcuts.getShortcutFromKeyboardEvent({ key: "p", code: "KeyP", repeat: true }), null);
  assert.equal(shortcuts.getShortcutFromKeyboardEvent({ key: "p", code: "KeyP", isComposing: true }), null);
  assert.equal(shortcuts.matchesShortcut({ key: "p", code: "KeyP", repeat: true }, "KeyP"), false);
  assert.equal(shortcuts.matchesShortcut({ key: "p", code: "KeyP", isComposing: true }, "KeyP"), false);
});

test("legacy local profiles keep valid bindings while ignoring newly reserved Escape", async () => {
  const shortcuts = await loadShortcuts();
  const previousWindow = globalThis.window;
  globalThis.window = {
    localStorage: {
      getItem() { return JSON.stringify({ "code.run": "Ctrl+Enter", "task.next": "Escape", "task.previous": "F11" }); },
    },
  };
  try {
    const profile = shortcuts.getShortcutProfile("windows");
    assert.equal(profile["code.run"], "Ctrl+Enter");
    assert.equal(profile["task.next"], "Alt+ArrowRight");
    assert.equal(profile["task.previous"], "Alt+ArrowLeft");
    assert.throws(() => shortcuts.setShortcutBinding("windows", "task.next", "Escape"), /занята системой/);
    assert.throws(() => shortcuts.setShortcutBinding("windows", "task.previous", "F11"), /занята системой/);
  } finally {
    globalThis.window = previousWindow;
  }
});
