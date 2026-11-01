import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

async function loadTypeScript(relativePath, stubs = {}) {
  const source = await readFile(join(root, relativePath), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const loaded = { exports: {} };
  const requireStub = (id) => {
    if (id in stubs) return stubs[id];
    throw new Error(`unexpected import: ${id}`);
  };
  new Function("module", "exports", "require", transpiled)(loaded, loaded.exports, requireStub);
  return loaded.exports;
}

const highlight = await loadTypeScript("lib/python-highlight.ts");
const editing = await loadTypeScript("lib/python-editing.ts", { "./python-highlight": highlight });
const { applyPythonEdit, cleanPythonTraceback } = editing;
const { findPythonBracketState, highlightPython } = highlight;
const browserPythonSource = await readFile(join(root, "lib/browser-python.ts"), "utf8");
const workerFormatterStart = browserPythonSource.indexOf(" const cleanTraceback=error=>");
const workerFormatterEnd = browserPythonSource.indexOf("\n try{", workerFormatterStart);
assert.ok(workerFormatterStart >= 0 && workerFormatterEnd > workerFormatterStart);
const workerSandbox = {};
runInNewContext(
  browserPythonSource
    .slice(workerFormatterStart, workerFormatterEnd)
    .trim()
    .replace("const cleanTraceback=", "globalThis.cleanTraceback="),
  workerSandbox,
);
const cleanWorkerTraceback = (error) => workerSandbox.cleanTraceback(error);
const workerTemplateStart = browserPythonSource.indexOf("const WORKER=String.raw`");
const workerExportStart = browserPythonSource.indexOf("export function startBrowserPython", workerTemplateStart);
const workerTemplateEnd = browserPythonSource.lastIndexOf("`", workerExportStart);
assert.ok(workerTemplateStart >= 0 && workerExportStart > workerTemplateStart && workerTemplateEnd > workerTemplateStart);
runInNewContext(
  browserPythonSource.slice(workerTemplateStart + "const WORKER=String.raw`".length, workerTemplateEnd),
  { self: {} },
);

function edit(code, start, key, end = start, extra = {}) {
  return applyPythonEdit({ code, selectionStart: start, selectionEnd: end, key, tabSize: 4, ...extra });
}

test("Enter adds a block indent after a colon, including an inline comment", () => {
  const code = "if ready: # keep this comment";
  const result = edit(code, code.length, "Enter");
  assert.equal(result.value, `${code}\n    `);
  assert.equal(result.selectionStart, result.value.length);
});

test("Enter keeps existing indent on ordinary and multiline syntax lines", () => {
  const code = "    value = (\n        first + second\n    )";
  const ordinary = edit(code, code.indexOf("first") + 5, "Enter");
  assert.equal(ordinary.value, "    value = (\n        first\n         + second\n    )");
  const block = "    if (\n        ready\n    ):";
  const blockResult = edit(block, block.length, "Enter");
  assert.equal(blockResult.value, `${block}\n        `);
});

test("smart Backspace removes one indentation stop and selection edits remain native", () => {
  const code = "        value";
  const result = edit(code, 8, "Backspace");
  assert.equal(result.value, "    value");
  assert.equal(result.selectionStart, 4);
  const selected = edit("one\ntwo", 0, "Backspace", 7);
  assert.equal(selected, null);
});

test("Tab at the caret inserts to the next stop without moving the line", () => {
  assert.equal(edit("print(value)", 6, "Tab").value, "print(  value)");
  assert.equal(edit("    value", 4, "Tab").value, "        value");
  assert.equal(edit("# 😀", 4, "Backspace"), null);
});

test("Tab and Shift+Tab indent the complete selected lines", () => {
  const code = "one\ntwo\nthree";
  const indented = edit(code, 1, "Tab", 7);
  assert.equal(indented.value, "    one\n    two\nthree");
  assert.deepEqual([indented.selectionStart, indented.selectionEnd], [5, 15]);
  const dedented = edit(indented.value, indented.selectionStart, "Tab", indented.selectionEnd, { shiftKey: true });
  assert.equal(dedented.value, code);
  assert.deepEqual([dedented.selectionStart, dedented.selectionEnd], [1, 7]);
});

test("existing closing brackets are skipped only in Python code", () => {
  assert.deepEqual(edit("items[]", 6, "]"), { value: "items[]", selectionStart: 7, selectionEnd: 7 });
  assert.equal(edit("text = ']'", 8, "]"), null);
  assert.equal(edit("value = ]", 8, "]"), null);
});

test("bracket matching ignores strings and comments and reports unmatched closes", () => {
  const code = "result = (items[0]) # )";
  const open = code.indexOf("(") + 1;
  const close = code.indexOf(")");
  assert.deepEqual(findPythonBracketState(code, open), { active: code.indexOf("("), match: close, unmatched: null });
  assert.deepEqual(findPythonBracketState("value = ]", 9), { active: null, match: null, unmatched: 8 });
  const tokens = highlightPython("value = (1) # ]");
  assert.equal(tokens.find((token) => token.text === "(")?.kind, "bracket");
  assert.equal(tokens.find((token) => token.text === "# ]")?.kind, "comment");
});

test("traceback cleaning keeps Python source and caret while removing JS internals", () => {
  const error = new Error(`PythonError: Traceback (most recent call last):\n  File "main.py", line 3, in <module>\n    print(missing)\n          ^^^^^^^\nNameError: name 'missing' is not defined\n    at runPython (pyodide.asm.wasm:1:2)`);
  const result = cleanPythonTraceback(error);
  assert.match(result, /File "main\.py", line 3/);
  assert.match(result, /\^\^\^\^\^\^\^/);
  assert.match(result, /NameError/);
  assert.doesNotMatch(result, /pyodide\.asm|at runPython/);
});

test("traceback cleaning preserves pyodide and wasm words in user source", () => {
  const message = `PythonError: Traceback (most recent call last):\n  File "main.py", line 2, in <module>\n    print(pyodide, "file.wasm")\n          ^^^^^^^\nNameError: name 'pyodide' is not defined\n    at runPython (pyodide.asm.wasm:1:2)`;
  for (const [format, input] of [[cleanPythonTraceback, new Error(message)], [cleanWorkerTraceback, { message }]]) {
    const result = format(input);
    assert.match(result, /print\(pyodide, "file\.wasm"\)/);
    assert.match(result, /NameError: name 'pyodide'/);
    assert.doesNotMatch(result, /at runPython|pyodide\.asm\.wasm/);
  }
});

test("traceback cleaning skips internal Pyodide frames but keeps the user frame", () => {
  const message = `PythonError: Traceback (most recent call last):\n  File "/lib/python3.12/site-packages/_pyodide/_base.py", line 23, in run\n    return await runner()\n  File "main.py", line 4, in <module>\n    print(missing)\n          ^^^^^^^\nNameError: name 'missing' is not defined\n    at runPython (pyodide.asm.wasm:1:2)`;
  for (const [format, input] of [[cleanPythonTraceback, new Error(message)], [cleanWorkerTraceback, { message }]]) {
    const result = format(input);
    assert.doesNotMatch(result, /_pyodide[\\/]_base\.py/);
    assert.match(result, /File "main\.py", line 4/);
    assert.match(result, /print\(missing\)/);
    assert.match(result, /NameError/);
  }
});

test("traceback cleaning keeps SyntaxError source and caret", () => {
  const message = `PythonError:   File "main.py", line 1\n    if (\n       ^\nSyntaxError: '(' was never closed\n    at runPython (pyodide.asm.wasm:1:2)`;
  for (const [format, input] of [[cleanPythonTraceback, new Error(message)], [cleanWorkerTraceback, { message }]]) {
    const result = format(input);
    assert.match(result, /File "main\.py", line 1/);
    assert.match(result, /\n       \^\n/);
    assert.match(result, /SyntaxError: '\(' was never closed/);
    assert.doesNotMatch(result, /at runPython/);
  }
});

test("traceback cleaning preserves chained Python exceptions", () => {
  const message = `PythonError: Traceback (most recent call last):\n  File "main.py", line 2, in <module>\n    raise ValueError("first")\nValueError: first\n\nThe above exception was the direct cause of the following exception:\n\nTraceback (most recent call last):\n  File "/lib/python3.12/site-packages/_pyodide/_base.py", line 23, in run\n    return await runner()\n  File "main.py", line 5, in <module>\n    raise RuntimeError("second")\nRuntimeError: second\n    at runPython (pyodide.asm.wasm:1:2)`;
  for (const [format, input] of [[cleanPythonTraceback, new Error(message)], [cleanWorkerTraceback, { message }]]) {
    const result = format(input);
    assert.match(result, /ValueError: first/);
    assert.match(result, /RuntimeError: second/);
    assert.equal((result.match(/File "main\.py"/g) ?? []).length, 2);
    assert.doesNotMatch(result, /_pyodide[\\/]_base\.py|at runPython/);
  }
});
