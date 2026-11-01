import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("catalog keeps a selected type, server filters, source order, and return checkpoint", async () => {
  const bank = await read("components/task-bank-view.tsx");
  assert.match(bank, /useState\(initialFilters\.line \?\? getPreferences\(\)\.defaultTaskType\)/);
  assert.match(bank, /FILTER_STORAGE_KEY = "kontur:ui:task-bank:filters:v1"/);
  assert.match(bank, /RETURN_STORAGE_KEY = "kontur:ui:task-bank:return:v1"/);
  assert.match(bank, /sort: "number"/);
  assert.match(bank, /learningStatus: learningStatus \|\| undefined/);
  assert.match(bank, /bookmarksOnly: bookmarksOnly \|\| undefined/);
  assert.match(bank, /cursor,/);
  assert.match(bank, /setTasks\(\(current\) => mergeTasks\(current, page\.items\)\)/);
  assert.match(bank, /sequence=\$\{encodeURIComponent\(sequence\)\}/);
});

test("catalog statements and learning state use safe API-owned evidence", async () => {
  const bank = await read("components/task-bank-view.tsx");
  const bookmarksApi = await read("lib/kontur-api.ts");
  assert.match(bank, /statementToText\(task\.revision\.statement_html\)/);
  assert.doesNotMatch(bank, /dangerouslySetInnerHTML|innerHTML/);
  assert.match(bank, /getTaskProgress\(items\.map\(\(task\) => task\.revision\.id\)\)/);
  assert.match(bank, /bookmarkByRevision/);
  assert.match(bookmarksApi, /setBookmark\(taskId: string, revisionId: string, selected: boolean\)/);
  assert.match(bank, /status === "incorrect"/);
});

test("bookmarks route keeps immutable revisions, evidence statuses, and split-view actions", async () => {
  const view = await read("components/bookmarks-view.tsx");
  const page = await read("app/bookmarks/page.tsx");
  const css = await read("components/bookmarks-view.module.css");
  assert.ok(view.includes("getBookmarks()"));
  assert.ok(view.includes("getTaskRevision(bookmark.task_id, bookmark.task_revision_id)"));
  assert.equal(view.includes("getTask(bookmark.task_id)"), false);
  assert.ok(view.includes("Promise.all(bookmarks.map"));
  assert.ok(view.includes("getTaskProgress(resolved.filter((row) => row.task).map"));
  assert.ok(view.includes("setBookmark(row.bookmark.task_id, row.bookmark.task_revision_id, false)"));
  assert.ok(view.includes("statementToText(task.revision.statement_html)"));
  assert.ok(view.includes("aria-pressed={selected}"));
  assert.ok(view.includes("data-mobile-preview"));
  assert.ok(view.includes("/workspace?session="));
  assert.ok(view.includes("/workspace?revision="));
  assert.ok(view.includes("Сохранённая редакция задания сейчас недоступна"));
  assert.match(page, /<AppShell active="bookmarks">/);
  assert.ok(css.includes("grid-template-columns: minmax(300px"));
  assert.ok(css.includes("border-radius: 16px"));
  assert.ok(css.includes("min-height: 44px"));
  assert.ok(css.includes("@media (max-width: 760px)"));
  assert.ok(css.includes('data-mobile-preview="false"'));
  assert.ok(css.includes("statusCorrect"));
  assert.ok(css.includes("statusIncorrect"));
});
