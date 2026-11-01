import { test } from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import ts from "typescript";

async function files(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = new URL(entry.name + (entry.isDirectory() ? "/" : ""), directory);
    if (entry.isDirectory()) result.push(...await files(path));
    else if (/\.tsx?$/.test(entry.name)) result.push(path);
  }
  return result;
}

test("static production modules have no HTTP client, server API or authentication import", async () => {
  const root = new URL("../", import.meta.url);
  const paths = (await Promise.all(["app/", "components/", "lib/"].map(directory => files(new URL(directory, root))))).flat();
  const violations = [];
  for (const path of paths) {
    const source = ts.createSourceFile(path.pathname, await readFile(path, "utf8"), ts.ScriptTarget.Latest, true, path.pathname.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    function visit(node) {
      if ((ts.isCallExpression(node) || ts.isNewExpression(node)) && /^(fetch|XMLHttpRequest|WebSocket|EventSource|eval|Function|DOMParser)$/.test(node.expression.getText(source))) violations.push(`${path.pathname}: ${node.expression.getText(source)}`);
      if (ts.isJsxAttribute(node) && node.name.getText(source) === "dangerouslySetInnerHTML") violations.push(`${path.pathname}: HTML sink`);
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isPropertyAccessExpression(node.left) && node.left.name.text === "innerHTML") violations.push(`${path.pathname}: HTML sink`);
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier) && /^(https?:|@\/lib\/(api|kontur-api|owner-sync|demo|device-files))/.test(node.moduleSpecifier.text)) violations.push(`${path.pathname}: ${node.moduleSpecifier.text}`);
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(violations, []);
});

test("there is only one local application entry and static export is configured", async () => {
  const appFiles = await files(new URL("../app/", import.meta.url));
  assert.equal(appFiles.filter(path => /\/page\.tsx$/.test(path.pathname)).length, 1);
  assert.equal(appFiles.some(path => /\/(route|middleware)\./.test(path.pathname)), false);
  const config = await readFile(new URL("../next.config.ts", import.meta.url), "utf8");
  assert.match(config, /output:\s*"export"/);
  assert.doesNotMatch(config, /rewrites|redirects|BACKEND|https?:/);
});
