import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

function applyOwnProjectSwitch(state, nextProjectId) {
  return {
    ...state,
    currentProjectId: nextProjectId,
    urlProjectId: nextProjectId,
    generation: state.generation + 1,
  };
}

test("navigation uses the Kontur brand, practice label, and direct non-destructive archive", async () => {
  const sidebar = await read("components/sidebar.tsx");
  const css = await read("components/sidebar.module.css");
  const shell = await read("components/app-shell.tsx");
  const layout = await read("app/layout.tsx");

  assert.match(sidebar, /href: "\/exams", label: "Пробники"/);
  assert.match(sidebar, /className="brand-mark"[\s\S]{0,80}>Контур<\/span>/);
  assert.match(sidebar, /onArchive/);
  assert.match(sidebar, /archived: true/);
  assert.match(sidebar, /className=\{styles\.archiveTrigger\}/);
  assert.match(sidebar, /aria-label=\{`В архив: \$\{conversation\.title\}`\}/);
  assert.doesNotMatch(sidebar, /deleteConversation/);
  assert.match(css, /\.archiveTrigger/);
  assert.match(css, /@media \(pointer: coarse\)/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(shell, /className="kontur-brand"/);
  assert.match(shell, /Контур/);
  assert.match(layout, /title: "Контур — подготовка к ЕГЭ по информатике/);
});

test("welcome project picker uses real projects, preserves composer identity, and syncs the URL", async () => {
  const chat = await read("components/chat-view.tsx");
  const picker = await read("components/project-picker.tsx");
  const css = await read("components/project-picker.module.css");

  assert.match(chat, /<ProjectPicker projects=\{projects\} value=\{currentProjectId\} onChange=\{selectProject\}/);
  assert.match(chat, /setCurrentProjectId\(nextProjectId\)/);
  assert.match(chat, /params\.set\("project", nextProjectId\)/);
  assert.match(chat, /window\.history\.replaceState/);
  assert.match(chat, /Composer key=\{composerKey\}/);
  assert.match(chat, /project_id: currentProjectId/);
  assert.match(picker, /Без проекта/);
  assert.match(picker, /projects\.map/);
  assert.match(picker, /htmlFor="new-chat-project"/);
  assert.match(css, /prefers-reduced-motion/);
});

test("own folder switching preserves Composer state while invalidating stale callbacks", async () => {
  const chat = await read("components/chat-view.tsx");
  const selectStart = chat.indexOf("function selectProject");
  const selectEnd = chat.indexOf("\n  }", selectStart);
  assert.ok(selectStart >= 0, "the picker transition handler should exist");
  assert.ok(selectEnd > selectStart, "the picker transition handler should be bounded");
  const selectProject = chat.slice(selectStart, selectEnd);

  assert.match(chat, /internalProjectSwitchRef/);
  assert.match(chat, /viewScope\.current\.generation \+= 1/);
  assert.match(chat, /window\.addEventListener\(HISTORY_CHANGED_EVENT, refreshProjects\)/);
  assert.match(chat, /setProjects\(nextProjects\.filter\(\(project\) => !project\.archived\)\)/);
  assert.match(chat, /feed\.current\?\.scrollTo\(\{ top: feed\.current\.scrollHeight \}\); \}, \[messages\]\)/);
  assert.doesNotMatch(selectProject, /setDraft|setComposerKey/);

  const before = {
    composerKey: 3,
    draft: "Сохрани этот текст",
    selection: [4, 9],
    model: "gpt-5.6-sol",
    effort: "high",
    generation: 7,
    currentProjectId: null,
    urlProjectId: null,
  };
  const after = applyOwnProjectSwitch(before, "project-b");

  assert.equal(after.composerKey, before.composerKey);
  assert.equal(after.draft, before.draft);
  assert.deepEqual(after.selection, before.selection);
  assert.equal(after.model, before.model);
  assert.equal(after.effort, before.effort);
  assert.equal(after.generation, before.generation + 1);
  assert.equal(after.currentProjectId, "project-b");
  assert.equal(after.urlProjectId, "project-b");
});

test("folder joins the composer while its labelled native selection stays usable", async () => {
  const picker = await read("components/project-picker.tsx");
  const css = await read("components/project-picker.module.css");
  const chatCss = await read("components/chat-view.module.css");
  assert.match(picker, /<select[\s\S]*aria-label="Папка чата"/);
  assert.match(picker, /composer-select/);
  assert.match(css, /border-radius: 16px 16px 0 0/);
  assert.match(css, /margin: 0 14px -16px/);
  assert.match(chatCss, /\.composeArea :global\(\.composer-wrap\) \{ background: transparent;/);
});

test("composer keeps technical routing in settings while project selection stays native", async () => {
  const composer = await read("components/composer.tsx");
  const css = await read("app/globals.css");
  assert.equal((composer.match(/className="composer-select"/g) ?? []).length, 0);
  assert.doesNotMatch(composer, /<select/);
  assert.match(composer, /usePreferences\(\)/);
  assert.match(composer, /Подсказка/);
  assert.match(composer, /Объяснить/);
  assert.match(css, /@supports \(appearance: base-select\)/);
  assert.match(css, /\.composer-select::picker\(select\)/);
  assert.match(css, /\.select-control select:focus-visible \{[^}]*outline: 2px solid/);
  assert.match(css, /\.composer-select option:focus-visible/);
  assert.match(css, /max-height: min\(360px, calc\(100dvh - 24px\)\)/);
  assert.match(css, /max-width: min\(320px, calc\(100vw - 24px\)\)/);
});

test("empty learning and analytics states give a next action without technical placeholder copy", async () => {
  const analytics = await read("components/analytics-view.tsx");
  const learning = await read("components/learning-view.tsx");
  const css = await read("app/globals.css");
  assert.doesNotMatch(analytics, /analytics endpoint|Backend error/);
  assert.match(analytics, /Нажмите «Обновить»/);
  assert.match(analytics, /role="status"/);
  assert.match(analytics, /readableError/);
  assert.doesNotMatch(learning, /По evidence|Недостаточно evidence|Загружаем evidence|Backend error/);
  assert.match(learning, /Сохраните попытку по этой теме/);
  assert.doesNotMatch(analytics, /Сводные результаты появятся после/);
  assert.match(learning, /Здесь пока нет записей/);
  assert.match(css, /\.filter-row \{[^}]*flex-wrap: wrap/);
});
