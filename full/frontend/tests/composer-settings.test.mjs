import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

async function readComposer() {
  return readFile(join(root, "components/composer.tsx"), "utf8");
}

test("Composer uses persisted settings and keeps technical controls out of the working form", async () => {
  const source = await readComposer();

  assert.match(source, /usePreferences\(\)/);
  assert.match(source, /const \{ model, effort, tutorMode \} = preferences/);
  assert.match(source, /beforeSend\?: \(\) => Promise<unknown>/);
  assert.doesNotMatch(source, /className="model-select"/);
  assert.doesNotMatch(source, /className=\{.*effort-select/);
  assert.doesNotMatch(source, /className="composer-select"/);
  assert.match(source, /Подсказка/);
  assert.match(source, /Объяснить/);
  assert.doesNotMatch(source, /getCodeSamples|previewAssistantContext|composer-additions|Предпросмотр контекста/);
});

test("Composer preserves draft text across remounts and clears it only after success", async () => {
  const source = await readComposer();

  assert.match(source, /kontur:composer-draft:v1/);
  assert.match(source, /localStorage\.getItem\(draftStorageKey\)/);
  assert.match(source, /localStorage\.setItem\(draftStorageKey, text\)/);
  assert.match(source, /await beforeSend\?\.\(\)/);
  assert.ok(source.indexOf("await beforeSend?.()") < source.indexOf("onMessage({"));
  assert.ok(source.indexOf("await beforeSend?.()") < source.indexOf("sendAssistantMessage({"));
  assert.match(source, /if \(reachedTerminal\) \{[\s\S]{0,180}setActiveRunId\(null\)/);
  assert.match(source, /clearComposerDraft\(draftStorageKey\)/);
  assert.match(source, /typeof result\.content === "string" && result\.content\.trim\(\)/);
});

test("Composer keeps keyboard and control affordances bounded", async () => {
  const source = await readComposer();

  assert.match(source, /!event\.nativeEvent\.isComposing/);
  assert.match(source, /disabled=\{sealed \|\| busy \|\| !text\.trim\(\)\}/);
  assert.match(source, /minHeight: 44/);
  assert.match(source, /height: 44, minHeight: 44, minWidth: 44, width: 44/);
  assert.doesNotMatch(source, /Saved|Сохранено/);
});
