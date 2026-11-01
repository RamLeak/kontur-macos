import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { hitTestInkStroke } from "../lib/statement-ink.ts";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("board controls are compact and expose the approved drawing surfaces", async () => {
  const board = await read("components/infinite-board.tsx");
  const styles = await read("components/infinite-board.module.css");
  for (const tool of ["pen", "marker", "eraser", "line", "rect", "ellipse", "text", "pan"]) {
    assert.match(board, new RegExp(`key: "${tool}"`));
  }
  assert.match(styles, /height: 44px/);
  assert.match(styles, /min-width: 44px/);
  assert.match(board, /Толщина штриха/);
  assert.match(board, /Цвет/);
  assert.match(board, /Чистый/);
  assert.match(board, /Клетка/);
  assert.match(board, /Точки/);
});

test("board eraser hits long-stroke midpoints and respects zoomed painted edges", () => {
  const points = [{ x: 0, y: 100 }, { x: 100, y: 100 }];
  const zoom = 2;
  const radius = 22 / zoom;
  const paintedWidth = 3 / zoom;
  const canvas = { width: 1, height: 1 };

  assert.equal(hitTestInkStroke(points, { x: 50, y: 100 + radius + paintedWidth / 2 - 0.01 }, canvas, paintedWidth, radius), true);
  assert.equal(hitTestInkStroke(points, { x: 50, y: 100 + radius + paintedWidth / 2 + 0.01 }, canvas, paintedWidth, radius), false);
  assert.equal(hitTestInkStroke(points, { x: 50, y: 100 }, canvas, paintedWidth, radius), true);
});

test("board keeps preferences, active focus gating, touch surface, and silent save success", async () => {
  const board = await read("components/infinite-board.tsx");
  const styles = await read("components/infinite-board.module.css");
  assert.match(board, /active\?: boolean/);
  assert.match(board, /getPreferences\(\)\.boardBackground/);
  assert.match(board, /setPreferences\(\{ boardBackground: value \}\)/);
  assert.match(board, /useShortcuts\("canvas", shortcutActions\)/);
  assert.match(board, /active && boardFocused && !disabled/);
  assert.doesNotMatch(board, /resolveBoardShortcut/);
  assert.doesNotMatch(board, /Сохранено/);
  assert.doesNotMatch(board, /v\{board\.version\}/);
  assert.doesNotMatch(board, /P\/2 —/);
  assert.match(styles, /touch-action: none/);
  assert.match(board, /saveState === "error" && saveError/);
});

test("board persistence can be adapted for an exam item without changing the default save path", async () => {
  const board = await read("components/infinite-board.tsx");
  assert.match(board, /persist\?: \(document: BoardDocument\) => Promise<BoardDocument>/);
  assert.match(board, /persistDocumentOverride\(document\)/);
  assert.match(board, /saveBoard\(sessionId, document\)/);
  assert.match(board, /\.then\(\(\) => saveDocument\(payload\)\)/);
});

test("Shift locks pen and marker strokes to a line from the start point", async () => {
  const board = await read("components/infinite-board.tsx");
  assert.match(board, /drawingStartRef/);
  assert.match(board, /straight: \(tool === "pen" \|\| tool === "marker"\) && event\.shiftKey/);
  assert.match(board, /if \(event\.shiftKey\) drawingStart\.straight = true/);
  assert.match(board, /drawing\.points = \[\{ x: drawingStart\.x, y: drawingStart\.y \}, point\]/);
});

test("board tool widths retain the legacy defaults while accepting a requested width", async () => {
  const tools = await read("lib/board-tools.ts");
  assert.match(tools, /requestedWidth\?: number/);
  assert.match(tools, /const defaultWidth = tool === "marker" \? 18 : 3/);
  assert.match(tools, /Math\.min\(48, Math\.max\(1, requestedWidth/);
});

test("board imports only owner-checked asset IDs and keeps images in render, history, and clear", async () => {
  const board = await read("components/infinite-board.tsx");
  const types = await read("lib/types.ts");
  assert.match(types, /export interface BoardImage/);
  assert.match(types, /images\?: BoardImage\[\]/);
  assert.match(board, /importImage\?: \{ assetId: string; revisionHash: string; requestId: string \}/);
  assert.match(board, /loadDeviceAssetById\(assetId, revisionHash, signal\)/);
  assert.match(board, /image\/png.*image\/jpeg.*image\/gif.*image\/webp/);
  assert.doesNotMatch(board, /API_BASE}\/assets\/\$\{encodeURIComponent\(assetId\)\}\/download/);
  assert.match(board, /context\.drawImage\(cached\.image/);
  assert.match(board, /images: board\.images \?\? \[\]/);
  assert.match(board, /images: \[\]/);
  assert.match(board, /Очистить доску\?/);
  assert.doesNotMatch(board, /download_url/);
});

test("board stores preference opacity on new strokes and preserves legacy render fallback", async () => {
  const board = await read("components/infinite-board.tsx");
  const tools = await read("lib/board-tools.ts");
  const types = await read("lib/types.ts");
  assert.match(types, /opacity\?: number/);
  assert.match(board, /strokeOpacityForPreference/);
  assert.match(board, /strokeOpacityForPreference\(style\.kind, tool==="marker"\?getPreferences\(\)\.boardMarkerOpacity:getPreferences\(\)\.boardOpacity\)/);
  assert.match(board, /strokeOpacity\(stroke\.kind, stroke\.opacity\)/);
  assert.match(tools, /export function strokeOpacity\(kind: BoardStroke\["kind"\], opacity\?: number\)/);
  assert.match(tools, /Number\.isFinite\(boardOpacity\) \? boardOpacity : 1/);
});

test("board recovery scopes local content, strips server metadata, and offers explicit choices", async () => {
  const board = await read("components/infinite-board.tsx");
  assert.match(board, /draftScope\?: string/);
  assert.match(board, /const draftScope = draftScopeProp\?\.trim\(\) \|\| sessionId/);
  assert.match(board, /assessLocalDraft\(validDraft, initialBoard\.version, boardDraftContent\(initialBoard\)\)/);
  assert.match(board, /saveLocalDraft\(draftScope, "board", confirmedVersionRef\.current, boardDraftContent\(next\)\)/);
  assert.match(board, /if \(!disabled && draftAssessmentReadyRef\.current\)/);
  assert.match(board, /saveLocalDraft\(draftScope, "board", confirmedVersionRef\.current, boardDraftContent\(next\)\);\r?\n      \}\r?\n      queuedBoardRef/);
  assert.match(board, /const latestDraft = readLocalDraft<BoardDraftContent>\(draftScope, "board"\)/);
  assert.match(board, /saveLocalDraft\(draftScope, "board", saved\.version, latestContent\)/);
  assert.match(board, /clearLocalDraft\(draftScope, "board"\)/);
  assert.match(board, /Восстановить черновик/);
  assert.match(board, /Оставить серверную версию/);
  assert.match(board, /if \(!disabled\) persist\(restored\)/);
  assert.match(board, /if \(disabled \|\| !draftRecovery\) return/);
  assert.match(board, /disabled=\{disabled\} onClick=\{restoreDraft\}/);
  assert.match(board, /Локальный черновик сохранён отдельно/);
  assert.match(board, /const validDraft = draft && isBoardDraftContent\(draft\.content\) \? draft : null/);
  assert.match(board, /const latestContent = latestDraft && isBoardDraftContent\(latestDraft\.content\) \? latestDraft\.content : null/);
  assert.match(board, /function isBoardDraftContent\(value: unknown\)/);
  assert.match(board, /!Array\.isArray\(value\.strokes\).*!Array\.isArray\(value\.texts\).*!Array\.isArray\(value\.images\)/);
  assert.match(board, /value\.strokes\.every\(isBoardDraftStroke\)/);
  assert.match(board, /value\.images\.every\(isBoardDraftImage\)/);
  assert.match(board, /const controlsDisabled = disabled \|\| !active \|\| Boolean\(draftRecovery\)/);
});
