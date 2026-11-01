import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

test("Composer exposes all four manual effort levels without forcing a model-specific effort", async () => {
  const source = await readFile(join(root, "components/composer.tsx"), "utf8");

  assert.match(source, /const effortOptions: Effort\[\] = \["low", "medium", "high", "xhigh"\]/);
  assert.doesNotMatch(source, /if \(model === "sol"\) return \["high"\]/);
  assert.doesNotMatch(source, /if \(model === "terra"\) return \["medium"\]/);
});
