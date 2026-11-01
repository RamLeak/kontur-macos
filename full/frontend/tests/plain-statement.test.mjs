import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../lib/sanitize.ts", import.meta.url), "utf8");
const { plainStatementMarkup } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText).toString("base64")}`);

test("canonical plain-text comparisons and literal entities survive the markup boundary", () => {
  assert.equal(plainStatementMarkup("a<b>c & &le;"), "a&lt;b&gt;c &amp; &amp;le;");
  assert.equal(plainStatementMarkup("2¹⁰ + 101₂\n(1 + x)/(3)"), "2¹⁰ + 101₂\n(1 + x)/(3)");
});

test("HTML-like canonical text cannot create browser nodes", () => {
  assert.equal(plainStatementMarkup('<img src=x onerror="bad()">'), '&lt;img src=x onerror="bad()"&gt;');
  assert.equal(plainStatementMarkup("<script>bad()</script>"), "&lt;script&gt;bad()&lt;/script&gt;");
});
