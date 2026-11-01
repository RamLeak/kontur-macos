import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("sign-out draft registry waits for every editor and propagates save failures", async () => {
  const source = await read("lib/draft-lifecycle.ts");
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  const { registerDraftFlush, flushOpenDrafts } = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);
  const events = [];
  let resolveSave;
  const gate = new Promise((resolve) => { resolveSave = resolve; });
  const removeFirst = registerDraftFlush(async () => { events.push("code"); await gate; });
  const failure = new Error("board save failed");
  let rejectSave = true;
  const removeSecond = registerDraftFlush(async () => { events.push("board"); if (rejectSave) throw failure; });
  let settled = false;
  const pending = flushOpenDrafts().finally(() => { settled = true; });
  await Promise.resolve();
  assert.deepEqual(events, ["code", "board"]);
  assert.equal(settled, false);
  resolveSave();
  await assert.rejects(pending, (reason) => reason === failure);
  removeFirst();
  removeSecond();
  await assert.rejects(flushOpenDrafts(), (reason) => reason === failure);
  rejectSave = false;
  await flushOpenDrafts();
  const callsAfterSave = events.length;
  await flushOpenDrafts();
  assert.equal(events.length, callsAfterSave, "detached editors are unregistered only after a successful save");
});

test("profile sign-out flushes editors before clearing the owner session and reports failures", async () => {
  const shell = await read("components/app-shell.tsx");
  const api = await read("lib/api.ts");
  assert.match(shell, /await flushOpenDrafts\(\);\s*await logoutOwnerSession\(\)/);
  assert.match(shell, /setSignOutError\("Не удалось сохранить работу или завершить сессию/);
  assert.match(shell, /setAuthState\("signed-out"\)/);
  assert.match(api, /request\("\/auth\/session", \{ method: "DELETE" \}\)/);
  for (const path of ["components/workspace-view.tsx", "components/infinite-board.tsx", "components/exams-view.tsx"]) {
    assert.match(await read(path), /registerDraftFlush\(/, path);
  }
  const sidebar = await read("components/sidebar.tsx");
  assert.match(sidebar, /profile-row/);
  assert.match(sidebar, /settingsHref\("profile"\)/);
  assert.match(sidebar, /settingsHref\("samples"\)/);
  assert.match(sidebar, /returnTo/);
  assert.match(sidebar, /onSignOut/);
});

test("workspace keeps the composer in layout and provides compact keyboard-accessible tools", async () => {
  const css = await read("app/globals.css");
  const workspace = await read("components/workspace-view.tsx");
  const composer = await read("components/composer.tsx");
  const editor = await read("components/code-editor.tsx");
  assert.match(css, /\.app-shell \{[^}]*height: 100dvh;[^}]*overflow: hidden/);
  assert.match(css, /\.task-scroll \{[^}]*overflow: auto/);
  assert.doesNotMatch(css, /\.composer-wrap[^{}]*\{[^}]*position:\s*fixed/);
  assert.match(workspace, /role="tabpanel"/);
  assert.match(workspace, /mobile-task|mobilePane/);
  assert.match(composer, /composer-footer/);
  assert.match(editor, /Escape/);
  assert.match(editor, /highlightPython\(value\)/);
  assert.doesNotMatch(editor, /dangerouslySetInnerHTML|innerHTML/);
});
