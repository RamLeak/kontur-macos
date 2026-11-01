import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

test("overview gives learners an obvious next action", async () => {
  const source = await readFile(join(root, "components/overview-view.tsx"), "utf8");

  assert.match(source, />Выбрать задание</g);
  assert.match(source, />Повторить ошибки</g);
  assert.match(source, /Продолжить разбор/);
  assert.match(source, /getConversations/);
  assert.match(source, /workspace\?conversation=/);
});

test("overview hides archived conversations and refreshes after history changes", async () => {
  const source = await readFile(join(root, "components/overview-view.tsx"), "utf8");

  assert.match(source, /conversation\.archived !== true/);
  assert.match(source, /HISTORY_CHANGED_EVENT/);
  assert.match(source, /window\.addEventListener\(HISTORY_CHANGED_EVENT/);
  assert.match(source, /setLoadAttempt\(\(current\) => current \+ 1\)/);
});

test("overview keeps service diagnostics out of the learner home", async () => {
  const source = await readFile(join(root, "components/overview-view.tsx"), "utf8");

  assert.doesNotMatch(source, /getCapabilities|capability|Состояние сервисов|Границы возможностей/);
  assert.doesNotMatch(source, /insufficient_due_evidence/);
  assert.match(source, /Начните с одной задачи — план появится по вашим результатам/);
  assert.match(source, /Не удалось загрузить часть данных/);
  assert.match(source, /Открыть банк заданий/);
});

test("overview styles use a quiet responsive layout with reduced-motion support", async () => {
  const styles = await readFile(join(root, "components/overview.module.css"), "utf8");

  assert.match(styles, /var\(--border\)/);
  assert.match(styles, /grid-template-columns: repeat\(2/);
  assert.match(styles, /@media \(max-width: 600px\)/);
  assert.match(styles, /prefers-reduced-motion/);
});
