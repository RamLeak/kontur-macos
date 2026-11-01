import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

async function loadNavigation() {
  const source = await readFile(join(root, "lib/navigation.ts"), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", transpiled)(module, module.exports);
  return module.exports;
}

function storage() {
  const values = new Map();
  return {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); },
  };
}

test("settings returnTo and workspace fallback stay inside the app", async () => {
  const navigation = await loadNavigation();
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/workspace", search: "?session=s1" }), "/tasks");
  assert.equal(navigation.navigationLocation({ pathname: "/workspace/", search: "?revision=x" }), "/workspace?revision=x");
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/workspace/", search: "?revision=x" }), "/tasks");
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/settings", search: "?section=shortcuts&returnTo=%2Fworkspace%3Fsession%3Ds1" }), "/workspace?session=s1");
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/settings/", search: "?returnTo=%2Fanalytics%2F" }), "/analytics");
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/settings", search: "?returnTo=https%3A%2F%2Fevil.invalid" }), "/tasks");
  assert.equal(navigation.getEscapeNavigationFallback({ pathname: "/" }), "/");
  assert.equal(navigation.normalizeInternalNavigationPath("/api/v1"), null);
  assert.equal(navigation.normalizeInternalNavigationPath("/tasks/../settings"), null);
  assert.equal(navigation.normalizeInternalNavigationPath("/%2Ftasks"), null);
});

test("remember/consume keeps one internal step and truncates a popped route", async () => {
  const navigation = await loadNavigation();
  const local = storage();
  navigation.rememberNavigationEntry("/tasks", local);
  navigation.rememberNavigationEntry("/workspace?session=s1", local);
  navigation.rememberNavigationEntry("/settings?returnTo=%2Fworkspace%3Fsession%3Ds1", local);
  assert.deepEqual(navigation.readNavigationHistory(local), [
    "/tasks",
    "/workspace?session=s1",
    "/settings?returnTo=%2Fworkspace%3Fsession%3Ds1",
  ]);
  const target = navigation.consumeNavigationEntry({ pathname: "/settings", search: "?returnTo=%2Fworkspace%3Fsession%3Ds1" }, local);
  assert.equal(target, "/workspace?session=s1");
  assert.deepEqual(navigation.readNavigationHistory(local), ["/tasks", "/workspace?session=s1"]);
  assert.equal(navigation.consumeNavigationEntry({ pathname: "/workspace", search: "?session=s1" }, local), "/tasks");
});

test("append-only route history preserves A to B to A loop steps", async () => {
  const navigation = await loadNavigation();
  const local = storage();
  navigation.rememberNavigationEntry("/tasks", local);
  navigation.rememberNavigationEntry("/analytics", local);
  navigation.rememberNavigationEntry("/tasks", local);
  assert.deepEqual(navigation.readNavigationHistory(local), ["/tasks", "/analytics", "/tasks"]);
  assert.equal(navigation.consumeNavigationEntry({ pathname: "/tasks" }, local), "/analytics");
  assert.deepEqual(navigation.readNavigationHistory(local), ["/tasks", "/analytics"]);
  navigation.rememberNavigationEntry("/analytics", local);
  assert.equal(navigation.consumeNavigationEntry({ pathname: "/analytics" }, local), "/tasks");
  assert.deepEqual(navigation.readNavigationHistory(local), ["/tasks"]);
});

test("settings without returnTo uses the previous UI route, while invalid/self returnTo does not escape or loop", async () => {
  const navigation = await loadNavigation();
  assert.equal(navigation.resolveEscapeNavigationTarget(
    { pathname: "/settings", search: "?section=shortcuts" },
    ["/bookmarks", "/settings?section=shortcuts"],
  ), "/bookmarks");
  assert.equal(navigation.resolveEscapeNavigationTarget(
    { pathname: "/settings", search: "?section=shortcuts" },
    ["/analytics", "/settings"],
  ), "/analytics");
  assert.equal(navigation.resolveEscapeNavigationTarget(
    { pathname: "/settings", search: "?section=shortcuts&returnTo=%2Fapi%2Fv1" },
    ["/analytics", "/settings?section=shortcuts&returnTo=%2Fapi%2Fv1"],
  ), "/analytics");
  assert.equal(navigation.resolveEscapeNavigationTarget(
    { pathname: "/settings", search: "?section=shortcuts&returnTo=%2Fsettings%3Fsection%3Dinterface" },
    ["/tasks", "/settings?section=shortcuts&returnTo=%2Fsettings%3Fsection%3Dinterface"],
  ), "/tasks");
});

test("Escape navigation ignores modifiers, IME/repeat and editor targets", async () => {
  const navigation = await loadNavigation();
  const base = { key: "Escape", repeat: false, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false };
  assert.equal(navigation.isPlainEscapeEvent(base), true);
  assert.equal(navigation.isPlainEscapeEvent({ ...base, repeat: true }), false);
  assert.equal(navigation.isPlainEscapeEvent({ ...base, isComposing: true }), false);
  assert.equal(navigation.isPlainEscapeEvent({ ...base, ctrlKey: true }), false);
  const textarea = { tagName: "TEXTAREA", isContentEditable: false, closest: () => null };
  assert.equal(navigation.isEscapeProtectedTarget(textarea), true);
  const body = { tagName: "MAIN", isContentEditable: false, closest: () => null };
  assert.equal(navigation.isEscapeProtectedTarget(body), false);
});

test("focused chat input closes an open assistant, then remains protected from routing", async () => {
  const navigation = await loadNavigation();
  const common = {
    plainEscape: true,
    defaultPrevented: false,
    fullscreen: false,
    activeEscapeLayer: false,
    compact: false,
    navigationOpen: false,
    ownedTarget: false,
    focused: false,
  };
  assert.equal(navigation.resolveEscapeAction({ ...common, protectedTarget: true, panelVisible: true }), "close-panel");
  assert.equal(navigation.resolveEscapeAction({ ...common, protectedTarget: true, panelVisible: false }), "ignore");
  assert.equal(navigation.resolveEscapeAction({ ...common, ownedTarget: true, protectedTarget: true, panelVisible: true }), "ignore");
  assert.equal(navigation.resolveEscapeAction({ ...common, protectedTarget: true, compact: true, navigationOpen: true, panelVisible: false }), "close-drawer");
});

test("shell treats only the compact navigation drawer as an Escape layer", async () => {
  const shell = await readFile(join(root, "components/app-shell.tsx"), "utf8");
  assert.match(shell, /resolveEscapeAction\(/);
  assert.match(shell, /compact,navigationOpen,panelVisible,focused/);
});
