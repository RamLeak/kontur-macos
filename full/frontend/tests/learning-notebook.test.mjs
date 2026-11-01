import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("learning notebook keeps Mastery and Review API flows while editing only prevention notes", async () => {
  const view = await read("components/learning-view.tsx");
  assert.ok(view.includes("getMastery()"));
  assert.ok(view.includes("getErrors()"));
  assert.ok(view.includes("getReviewQueue()"));
  assert.ok(view.includes('status: item.status ?? "proposed"'));
  assert.ok(view.includes("prevention_note: noteDraft"));
  assert.ok(view.includes("note: value.note"));
  assert.equal(view.includes("description:"), false);
  assert.equal(view.includes("completeReview"), false);
  assert.equal(view.includes("completeReview(item.id, true)"), false);
});

test("learning notebook links only pinned revisions and keeps unknown revision action disabled", async () => {
  const view = await read("components/learning-view.tsx");
  assert.ok(view.includes('"/workspace?revision="'));
  assert.ok(view.includes('"&fresh=correction"'));
  assert.ok(view.includes('typeof item.task_revision_id === "string"'));
  assert.ok(view.includes("Для события не передана сохранённая редакция."));
  assert.ok(view.includes("Нет сохранённой редакции для новой сессии"));
  assert.ok(view.includes('"/tasks?line="'));
  assert.ok(view.includes("Перейти к повторению"));
  assert.equal(view.includes("Готово"), false);
});

test("learning notebook keeps historical metadata read-only and exposes compact accessible controls", async () => {
  const view = await read("components/learning-view.tsx");
  const css = await read("components/learning-view.module.css");
  assert.ok(view.includes("История события"));
  assert.ok(view.includes("aria-pressed={selected}"));
  assert.ok(view.includes("Изменить заметку"));
  assert.ok(view.includes("Сохранить заметку"));
  assert.ok(css.includes("--learning-canvas"));
  assert.ok(css.includes("grid-template-columns: minmax(320px"));
  assert.ok(css.includes("min-height: 44px"));
  assert.ok(css.includes("border-radius: 12px"));
  assert.ok(css.includes("@media (max-width: 860px)"));
  assert.ok(css.includes('data-mobile-preview="false"'));
});