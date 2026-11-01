import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function loadSearch(source) {
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const loaded = { exports: {} };
  new Function("module", "exports", transpiled)(loaded, loaded.exports);
  return loaded.exports;
}

test("settings section search matches labels, descriptions and keywords without changing order", async () => {
  const source = await readFile(join(root, "lib/settings-search.ts"), "utf8");
  const { filterSettingsSections } = loadSearch(source);
  const items = [
    { id: "interface", label: "Интерфейс", description: "Тема и доступность", keywords: ["размер текста"] },
    { id: "workspace", label: "Рабочее пространство", description: "Возврат и редактор", keywords: ["кэш файлов"] },
    { id: "archive", label: "Архив чатов", description: "Сохранённые разговоры", keywords: ["история"] },
  ];

  assert.deepEqual(filterSettingsSections(items, "  ТЕМА ").map((item) => item.id), ["interface"]);
  assert.deepEqual(filterSettingsSections(items, "кэш").map((item) => item.id), ["workspace"]);
  assert.deepEqual(filterSettingsSections(items, "  кэш   файлов ").map((item) => item.id), ["workspace"]);
  assert.deepEqual(filterSettingsSections(items, "история").map((item) => item.id), ["archive"]);
  assert.deepEqual(filterSettingsSections(items, "нет такого"), []);
  assert.deepEqual(filterSettingsSections(items, "").map((item) => item.id), ["interface", "workspace", "archive"]);
});

test("settings view exposes a real section search and empty-result state", async () => {
  const source = await readFile(join(root, "components/settings-view.tsx"), "utf8");
  assert.match(source, /filterSettingsSections\(sectionItems, sectionQuery\)/);
  assert.match(source, /onChange=\{\(event\) => setSectionQuery\(event\.target\.value\)\}/);
  assert.match(source, /Ничего не найдено\. Попробуйте другой запрос/);
  assert.match(source, /type="search"/);
});
