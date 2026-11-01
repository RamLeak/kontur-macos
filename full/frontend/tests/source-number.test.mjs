import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../lib/source-number.ts", import.meta.url), "utf8");
const { resolveSourceNumber, sourceNumberLabel } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText).toString("base64")}`);

test("source identity is literal, independent of the type and filtered position", () => {
  assert.equal(resolveSourceNumber("0002382", "0002382"), "0002382");
  assert.equal(resolveSourceNumber(undefined, "9873"), "9873");
  assert.equal(resolveSourceNumber("9873", null), "9873");
  assert.equal(sourceNumberLabel("2382"), "КЕГЭ № 2382");
  assert.equal(sourceNumberLabel(null), "Номер источника не указан");
});

test("a conflicting or malformed identity is not silently replaced by a list position", () => {
  assert.throws(() => resolveSourceNumber("2382", "2383"));
  for (const invalid of [2382, "", " 2382", "2382\n", "x".repeat(256)]) {
    assert.throws(() => resolveSourceNumber(invalid, "2382"));
  }
  assert.equal(resolveSourceNumber(null, undefined), null);
});
