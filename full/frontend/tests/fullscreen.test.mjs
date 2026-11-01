import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);

async function loadFullscreen() {
  const source = await readFile(join(root, "lib/fullscreen.ts"), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", "require", transpiled)(module, module.exports, require);
  return module.exports;
}

test("plain F11 is the only fullscreen key event accepted by the helper", async () => {
  const fullscreen = await loadFullscreen();
  assert.equal(fullscreen.isPlainF11Event({ key: "F11" }), true);
  for (const event of [
    { key: "F11", repeat: true },
    { key: "F11", isComposing: true },
    { key: "F11", keyCode: 229 },
    { key: "F11", ctrlKey: true },
    { key: "F11", altKey: true },
    { key: "F11", shiftKey: true },
    { key: "F11", metaKey: true },
    { key: "F11", defaultPrevented: true },
    { key: "Escape" },
  ]) assert.equal(fullscreen.isPlainF11Event(event), false);
});

test("fullscreen helper calls request/exit and preserves native promise rejection", async () => {
  const fullscreen = await loadFullscreen();
  let requested = 0;
  let exited = 0;
  const documentLike = { fullscreenElement: null, exitFullscreen: () => { exited += 1; return Promise.resolve(); } };
  const rootLike = { requestFullscreen: () => { requested += 1; return Promise.resolve(); } };

  assert.equal(fullscreen.canToggleFullscreen(documentLike, rootLike), true);
  await fullscreen.toggleFullscreen(documentLike, rootLike);
  assert.equal(requested, 1);

  documentLike.fullscreenElement = {};
  await fullscreen.toggleFullscreen(documentLike, rootLike);
  assert.equal(exited, 1);

  const rejected = fullscreen.toggleFullscreen({ fullscreenElement: null }, { requestFullscreen: () => Promise.reject(new Error("permission denied")) });
  await assert.rejects(rejected, /permission denied/);
});

test("missing fullscreen API is left to the browser default", async () => {
  const fullscreen = await loadFullscreen();
  const documentLike = { fullscreenElement: null };
  const rootLike = {};
  assert.equal(fullscreen.canToggleFullscreen(documentLike, rootLike), false);
  assert.equal(fullscreen.toggleFullscreen(documentLike, rootLike), undefined);
});

test("AppShell guards fullscreen capture, pending requests and user-safe errors", async () => {
  const shell = await readFile(join(root, "components/app-shell.tsx"), "utf8");
  assert.match(shell, /isPlainF11Event\(event\)/);
  assert.match(shell, /hasActiveEscapeLayer\(document\)/);
  assert.match(shell, /fullscreenPendingRef\.current/);
  assert.match(shell, /toggleFullscreen\(document,root\)/);
  assert.match(shell, /Не удалось изменить полноэкранный режим/);
});
