import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function helpers() {
  const source = await readFile(new URL("../lib/workspace-layout.ts", import.meta.url), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);
}

test("split dragging respects readable widths and invalid saved values", async () => {
  const { clampSplit, splitAtPointer, splitBounds } = await helpers();
  assert.equal(clampSplit(NaN, 1000), 64);
  assert.equal(clampSplit(Infinity, 1000), 64);
  for (const width of [390, 790, 900, 1166, 2000]) {
    const [min, max] = splitBounds(width);
    assert.ok(min <= 50 && max >= 50);
    assert.equal(splitAtPointer(-100, 270, width), min);
    assert.equal(splitAtPointer(9000, 270, width), max);
  }
});

test("workspace split migration preserves the old condition share after the pane order inversion", async () => {
  const { readStoredSplit, SPLIT_STORAGE_KEY } = await helpers();
  const values = new Map([["kontur:workspace-split:v2", "38"]]);
  const storage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
  };
  assert.equal(readStoredSplit(1000, storage), 62);
  assert.equal(values.get(SPLIT_STORAGE_KEY), "62");
  assert.equal(readStoredSplit(1000, storage), 62);
});

test("split has keyboard/pointer controls and persists width", async () => {
  const component = await readFile(new URL("../components/resizable-workspace.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../components/resizable-workspace.module.css", import.meta.url), "utf8");
  assert.match(component, /role="separator"/);
  assert.match(component, /setPointerCapture/);
  assert.match(component, /onLostPointerCapture/);
  assert.match(component, /localStorage\.setItem/);
  assert.match(component, /"ArrowLeft", "ArrowRight", "Home", "End", "Enter"/);
  assert.match(css, /@media \(max-width: 790px\)/);
});

test("welcome explicitly creates real project chats and workspace handles both chat kinds", async () => {
  const chat = await readFile(new URL("../components/chat-view.tsx", import.meta.url), "utf8");
  const composer = await readFile(new URL("../components/composer.tsx", import.meta.url), "utf8");
  const workspace = await readFile(new URL("../components/workspace-view.tsx", import.meta.url), "utf8");
  assert.match(chat, /project_id: currentProjectId/);
  assert.match(chat, /data-testid="welcome-tasks"/);
  assert.match(chat, /onStartConversation=\{startConversation\}/);
  assert.match(composer, /await onStartConversation\(/);
  assert.match(composer, /conversationId: effectiveConversationId/);
  assert.match(workspace, /useSearchParams/);
  assert.match(workspace, /if \(!loadPromise\.current\)/);
  assert.match(workspace, /reason\.code === "SESSION_NOT_FOUND"/);
  assert.doesNotMatch(chat, /dangerouslySetInnerHTML|deleteConversation/);
  assert.match(chat, /viewScope\.current\.generation === generation/);
  assert.match(chat, /if \(!isCurrentView\(\)\) throw/);
  assert.match(chat, /if \(!isCurrentView\(\)\) return/);
  assert.match(chat, /if \(isCurrentView\(\)\) setMessages/);
});

test("board history snapshots are captured before deferred React updaters", async () => {
  const board = await readFile(new URL("../components/infinite-board.tsx", import.meta.url), "utf8");
  assert.match(board, /const previousBoard = cloneBoard\(boardRef\.current\)/);
  assert.match(board, /setHistory\(\(previous\) => \[\.\.\.previous\.slice\(-29\), previousBoard\]/);
  assert.doesNotMatch(board, /set(?:Redo)?History\(\(\w+\) => [^\n]*cloneBoard\(boardRef\.current\)/);
});

test("assistant width presets reserve a usable code pane and increase only the assistant", async () => {
  const { assistantSplit } = await helpers();
  assert.equal(Math.round((1 - assistantSplit(1440, "standard") / 100) * 1432), 360);
  assert.ok(assistantSplit(1440, "narrow") > assistantSplit(1440, "wide"));
  assert.ok(assistantSplit(1024, "wide") > 50);
});
