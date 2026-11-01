import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

async function loadHistoryHelpers() {
  const source = await read("lib/history-events.ts");
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);
}

test("sidebar keeps project selection, expansion, active history, and safe conversation actions separate", async () => {
  const sidebar = await read("components/sidebar.tsx");

  assert.match(sidebar, /usePathname/);
  assert.match(sidebar, /useSearchParams/);
  assert.match(sidebar, /getActiveProjectId\(projectIdFromUrl, currentConversationProjectId\)/);
  assert.match(sidebar, /href=\{`\/\?project=\$\{encodeURIComponent\(project\.id\)\}`\}/);
  assert.match(sidebar, /aria-expanded=\{expanded\}/);
  assert.match(sidebar, /projectChildren/);
  assert.match(sidebar, /currentConversationId === conversation\.id/);
  assert.match(sidebar, /isRecentConversation/);
  assert.match(sidebar, /isPinnedConversation/);
  assert.match(sidebar, /archived: true/);
  assert.match(sidebar, /pinned: actionConversation\.pinned !== true/);
  assert.match(sidebar, /updateConversation\(conversationId, patch\)/);
  assert.match(sidebar, /notifyHistoryChanged\(\)/);
  assert.match(sidebar, /role="menu"/);
  assert.match(sidebar, /aria-haspopup="menu"/);
  assert.doesNotMatch(sidebar, /deleteConversation/);
  assert.doesNotMatch(sidebar, /method:\s*["']DELETE["']/);
});

test("archive surface only restores archived conversations and never deletes them", async () => {
  const archived = await read("components/archived-chats.tsx");

  assert.match(archived, /getConversations/);
  assert.match(archived, /conversation\.archived === true/);
  assert.match(archived, /updateConversation\(conversation\.id, \{ archived: false \}\)/);
  assert.match(archived, /notifyHistoryChanged\(\)/);
  assert.match(archived, /loadRequestRef/);
  assert.match(archived, /mountedRef/);
  assert.match(archived, /requestId !== loadRequestRef\.current/);
  const restoreBlock = archived.match(/async function restoreConversation[\s\S]*?\n  const content = useMemo/)?.[0] ?? "";
  assert.match(restoreBlock, /loadRequestRef\.current \+= 1/);
  assert.match(restoreBlock, /setLoading\(false\)/);
  assert.match(restoreBlock, /if \(!mountedRef\.current\) return;/);
  assert.match(restoreBlock, /if \(mountedRef\.current\) setRestoringId\(null\)/);
  assert.match(archived, /Архив пуст/);
  assert.match(archived, /Не удалось загрузить архив/);
  assert.match(archived, /Повторить загрузку/);
  assert.match(archived, /Восстановить/);
  assert.doesNotMatch(archived, /deleteConversation/);
  assert.doesNotMatch(archived, /method:\s*["']DELETE["']/);
});

test("history helpers treat archive as a fail-closed visibility flag", async () => {
  const {
    getActiveProjectId,
    isActiveConversation,
    isPinnedConversation,
    isRecentConversation,
    sortConversationsByUpdated,
  } = await loadHistoryHelpers();
  const unprojected = { project_id: null, updated_at: "2026-09-01T00:00:00Z" };

  assert.equal(isActiveConversation(unprojected), true, "missing archived flag stays visible for backwards compatibility");
  assert.equal(isActiveConversation({ ...unprojected, archived: true }), false);
  assert.equal(isRecentConversation(unprojected), true);
  assert.equal(isRecentConversation({ ...unprojected, pinned: true }), false);
  assert.equal(isPinnedConversation({ ...unprojected, pinned: true }), true);
  assert.equal(isPinnedConversation({ ...unprojected, archived: true, pinned: true }), false);
  assert.equal(getActiveProjectId("project-a", "project-b"), "project-a");
  assert.equal(getActiveProjectId(null, "project-b"), "project-b");
  assert.equal(getActiveProjectId("", null), null);

  const sorted = sortConversationsByUpdated([
    { ...unprojected, updated_at: "2026-08-01T00:00:00Z", id: "old" },
    { ...unprojected, updated_at: "2026-09-01T00:00:00Z", id: "new" },
  ]);
  assert.deepEqual(sorted.map((item) => item.id), ["new", "old"]);
});
