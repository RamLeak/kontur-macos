import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("project creation uses an accessible in-app dialog instead of browser prompt", async () => {
  const sidebar = await read("components/sidebar.tsx");

  assert.doesNotMatch(sidebar, /window\.prompt|\bprompt\s*\(/);
  assert.match(sidebar, /aria-haspopup="dialog"/);
  assert.match(sidebar, /role="dialog"/);
  assert.match(sidebar, /aria-modal="true"/);
  assert.match(sidebar, /aria-labelledby="create-project-title"/);
  assert.match(sidebar, /id="create-project-title">Создать проект/);
  assert.match(sidebar, /htmlFor="project-name"/);
  assert.match(sidebar, /id="project-name"/);
  assert.match(sidebar, /ref=\{projectNameInputRef\}/);
  assert.match(sidebar, /projectNameInputRef\.current\?\.focus\(\)/);
  assert.match(sidebar, /event\.key === "Escape"/);
  assert.match(sidebar, /event\.target === event\.currentTarget/);
  assert.match(sidebar, /Отмена/);
  assert.match(sidebar, /type="submit"/);
  assert.match(sidebar, /const name = projectName\.trim\(\);/);
  assert.match(sidebar, /createProject\(\{ name \}\)/);
  assert.match(sidebar, /aria-live="polite"/);
  assert.match(sidebar, /className="project-dialog-error"/);
  assert.match(sidebar, /className="sidebar-error"/);
});
