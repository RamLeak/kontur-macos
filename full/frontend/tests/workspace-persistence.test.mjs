import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("code editor flushes the latest dirty content on lifecycle boundaries", async () => {
  const editor = await read("components/code-editor.tsx");
  const workspace = await read("components/workspace-view.tsx");
  assert.match(editor, /onBlur\?: \(\) => void/);
  assert.match(editor, /onBlur=\{onBlur\}/);
  assert.match(workspace, /codeFlushPromiseRef/);
  assert.match(workspace, /persistCurrentCode\(sessionId, latestContent\)/);
  assert.match(workspace, /document\.visibilityState === "hidden"/);
  assert.match(workspace, /document\.addEventListener\("visibilitychange"/);
  assert.match(workspace, /void flushCurrentCode\(\)\.catch\(\(\) => undefined\)/);
  assert.match(workspace, /previous \? previous\.catch\(\(\) => undefined\)/);
});

test("infinite board serializes saves and flushes the newest snapshot", async () => {
  const board = await read("components/infinite-board.tsx");
  assert.match(board, /queuedBoardRef/);
  assert.match(board, /savePromiseRef/);
  assert.match(board, /confirmedVersionRef/);
  assert.match(board, /version: confirmedVersionRef\.current/);
  assert.match(board, /const pending = savePromiseRef\.current[\s\S]{0,120}await pending/);
  assert.match(board, /document\.visibilityState === "hidden"/);
  assert.match(board, /void flushSaves\(\)\.catch\(\(\) => undefined\)/);
  assert.match(board, /strokes: \[\.\.\.current\.strokes, stroke\]/);
  assert.match(board, /boardRef\.current = next/);
});

test("new chat opens the project welcome screen and history expands in place", async () => {
  const sidebar = await read("components/sidebar.tsx");
  const overview = await read("components/overview-view.tsx");
  assert.match(sidebar, /href=\{newChatHref\}/);
  assert.match(sidebar, /historyShowAll/);
  assert.match(sidebar, /historyShowAll \? recentConversations : recentConversations\.slice\(0, 5\)/);
  assert.match(sidebar, /className="show-more"/);
  assert.doesNotMatch(sidebar, /<Link className="show-more" href="\/tasks"/);
  assert.match(overview, /data-testid="choose-task" href="\/tasks"/);
  assert.match(overview, /latestHref = latestConversation \? `\/workspace\?conversation=/);
});

test("file panel downloads the original through device cache and offers its exact Python filename", async () => {
  const workspace = await read("components/workspace-view.tsx");
  const row = workspace.slice(workspace.indexOf("function AssetRow("), workspace.indexOf("function HighlightedStatement("));
  assert.match(row, /const inputPath = asset.name/);
  assert.match(row, /navigator\.clipboard\.writeText/);
  assert.match(row, /loadDeviceAsset\(asset, revisionHash\)/);
  assert.match(row, /downloadDeviceFile/);
  assert.match(row, /Скопировать имя/);
  assert.doesNotMatch(row, /href=|onPrepare\(asset\.id\)/);
  assert.doesNotMatch(workspace, /В браузере нет локальной копии/);
});
