import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("settings expose understandable profile, assistant, sample and advanced sections", async () => {
  const settings = await read("components/settings-view.tsx");

  for (const label of ["Профиль", "Помощник", "Образцы кода", "Дополнительно", "Архив чатов"]) {
    assert.match(settings, new RegExp(label));
  }
  assert.match(settings, /Даниил/);
  assert.match(settings, /aria-selected=\{item\.id === section\}/);
  assert.match(settings, /role="tablist"/);
  assert.match(settings, /role="tabpanel"/);
  assert.match(settings, /ArrowDown|ArrowRight/);
  assert.match(settings, /event\.key === "Home"/);
  assert.match(settings, /event\.key === "End"/);
  assert.match(settings, /useSearchParams/);
  assert.match(settings, /section=/);
  assert.match(settings, /readableError/);
  assert.match(settings, /Авто/);
  assert.match(settings, /Выбрать задание/);
});

test("settings expose workspace, editor, drawing and cache preferences with section reset actions", async () => {
  const settings = await read("components/settings-view.tsx");
  for (const label of ["Плотность интерфейса", "Автоскрытие навигации", "Размер шрифта редактора", "Перенос строк", "Размер отступа Tab", "Номера строк", "Высота вывода", "Сглаживание пера", "Кэш файлов", "Сбросить раздел"]) {
    assert.match(settings, new RegExp(label));
  }
  assert.match(settings, /clearDeviceFileCache/);
  assert.match(settings, /assistantWidth/);
  assert.match(settings, /fileCacheMb/);
  assert.match(settings, /drawingSmoothing/);
});

test("settings keep archive reachable and use neutral overflow-safe layout", async () => {
  const settings = await read("components/settings-view.tsx");
  const css = await read("components/settings.module.css");

  assert.match(settings, /import \{ ArchivedChats \} from "@\/components\/archived-chats";/);
  assert.match(settings, /id: "archive", label: "Архив чатов"/);
  assert.match(settings, /section === "archive" \? <ArchivedChats \/> : null/);
  assert.match(settings, /value === "archive"/);
  assert.match(css, /grid-template-columns: 190px minmax\(0, 1fr\)/);
  assert.match(css, /background: var\(--surface-strong, #ededed\);/);
  assert.match(css, /overflow-wrap: anywhere;/);
  assert.match(css, /\.sourceActionButtons \{[\s\S]*flex-wrap: wrap;/);
  assert.match(css, /@media \(max-width: 680px\)/);
  assert.match(css, /\.tabs \{[\s\S]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.doesNotMatch(css, /gradient/);
  assert.doesNotMatch(css, /#(?:3d348e|4b429c|e3e1f6|fbfbfe)/i);
});

test("source checks and local file import are explicit and hidden behind advanced settings", async () => {
  const source = await read("components/source-settings.tsx");
  const bank = await read("components/task-bank-view.tsx");

  assert.match(source, /<details className=\{styles\.advancedDetails\}>/);
  assert.doesNotMatch(source, /<details[^>]+open=/);
  assert.doesNotMatch(source, /useEffect/);
  assert.match(source, /onClick=\{\(\) => void checkConnections\(\)\}/);
  assert.match(source, /onClick=\{\(\) => void checkHealth\(\)\}/);
  assert.match(source, /onClick=\{onImport\}/);
  assert.doesNotMatch(source, /onClick=\{\(\) => void syncSourceVariant\(\)\}/);
  const settings = await read("components/settings-view.tsx");
  assert.match(settings, /SourceSettings onImport=\{\(\) => selectSection\("data"\)\}/);
  assert.match(source, /readableError/);
  assert.doesNotMatch(source, /Показать технические сведения|JSON\.stringify/);
  assert.match(source, /Последнее обновление варианта|Заданий/);

  assert.doesNotMatch(bank, /getSourceHealth|syncConfiguredVariant|getCapabilities/);
  assert.doesNotMatch(bank, /source-sync-panel|source-sync-form|source-sync-status/);
  assert.match(bank, /getTaskCatalog/);
});
