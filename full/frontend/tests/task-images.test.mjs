import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("catalog images use bounded device loading without a server-cache download fallback", async () => {
  const source = await read("components/task-images.tsx");
  const styles = await read("components/task-images.module.css");

  assert.match(source, /loadDeviceAsset\(asset, revisionHash, signal\)/);
  assert.match(source, /loadControllerRef/);
  assert.match(source, /signal\?\.aborted/);
  assert.match(source, /deviceAssetUrl\(file\)/);
  assert.match(source, /IntersectionObserver/);
  assert.match(source, /loading="lazy"/);
  assert.doesNotMatch(source, /safeAssetDownloadUrl|\/download/);
  assert.doesNotMatch(source, /prepareAsset/);
  assert.doesNotMatch(source, /<figcaption/);
  assert.doesNotMatch(source, /asset\.name/);
  assert.doesNotMatch(source, /image-0/);
  assert.doesNotMatch(styles, /\.caption|\.note/);
});

test("image cards identify ordinary illustrations without exposing cache metadata", async () => {
  const source = await read("components/task-images.tsx");
  assert.match(source, /Иллюстрации к условию/);
  assert.match(source, /const label = `Иллюстрация \$\{index \+ 1\}`/);
  assert.match(source, /Проверка целостности изображения не пройдена/);
  assert.match(source, /asset\.cache_state === "quarantined"/);
});
