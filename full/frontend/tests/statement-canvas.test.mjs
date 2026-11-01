import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("statement canvas keeps ink revision-bound and stores normalized points", async () => {
  const source = await read("components/statement-canvas.tsx");
  assert.match(source, /children: ReactNode/);
  assert.match(source, /annotations: Annotation\[\]/);
  assert.match(source, /revisionHash: string/);
  assert.match(source, /onInk: \(annotation: Annotation\) => Promise<Annotation>/);
  assert.match(source, /onDeleteInk: \(annotationId: string\) => Promise<void>/);
  assert.match(source, /annotation\.kind === "ink" && annotation\.anchor\?\.content_hash === revisionHash/);
  assert.match(source, /x: clamp\(point\.x\), y: clamp\(point\.y\)/);
  assert.match(source, /content_hash: revisionHash/);
  assert.match(source, /text: strokeTool === "marker" \? "marker" : undefined/);
  assert.match(source, /annotation\.text === "marker"/);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML/);
});

test("statement canvas exposes neutral 44px mark controls and leaves text selectable when off", async () => {
  const source = await read("components/statement-canvas.tsx");
  const styles = await read("components/statement-canvas.module.css");
  assert.match(source, /Пометки/);
  assert.match(source, /Перо/);
  assert.match(source, /Маркер/);
  assert.match(source, /Стереть последний черновик/);
  assert.match(source, /Отменить последнюю сохранённую пометку/);
  assert.match(source, /Ластик/);
  assert.match(styles, /height: 44px/);
  assert.match(styles, /flex-wrap: wrap/);
  assert.match(styles, /max-width: 100%/);
  assert.doesNotMatch(styles, /overflow-x: auto/);
  assert.match(styles, /pointer-events: none/);
  assert.match(styles, /touch-action: none/);
  assert.match(source, /enabled && !disabled/);
  assert.match(source, /event\.shiftKey/);
});

test("statement canvas retains failed saves and exposes saved-ink recovery", async () => {
  const source = await read("components/statement-canvas.tsx");
  assert.match(source, /await onInk\(annotation\)/);
  assert.match(source, /setPendingSave\(\{ annotation, status: "error" \}\)/);
  assert.match(source, /Пометку не удалось сохранить/);
  assert.match(source, /Повторить сохранение/);
  assert.match(source, /await deleteInk\(saved, true\)/);
  assert.match(source, /Удалить после проверки/);
  assert.match(source, /onDeleteInk\(annotation\.id\)/);
  assert.match(source, /Пометку не удалось удалить/);
  assert.match(source, /Повторить удаление/);
  assert.match(source, /hitTestInkStroke/);
  assert.match(source, /not[\s\S]{0,80}exact text anchors/i);
});
