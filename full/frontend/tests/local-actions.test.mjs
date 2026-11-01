import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
async function load(name) { const module = { exports: {} }; const source = await readFile(new URL(`../lib/${name}.ts`, import.meta.url), "utf8"); new Function("module", "exports", ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText)(module, module.exports); return module.exports; }
const { runLocalAction, waitForLocalActions } = await load("local-native");
const { lockLocalInteraction } = await load("local-interaction-lock");
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const tick = () => new Promise(r => setImmediate(r));

test("native close action barrier covers pre-import flush and post-import hydration", async () => {
  const before = deferred(), restored = deferred(); let settled = false;
  const action = runLocalAction(async () => { await before.promise; await restored.promise; });
  const wait = waitForLocalActions().then(() => { settled = true; });
  await tick(); assert.equal(settled, false); before.resolve(); await tick(); assert.equal(settled, false);
  restored.resolve(); await action; await wait; assert.equal(settled, true);
});
test("failed transfer rejects a waiting close instead of acknowledging the data barrier", async () => {
  const release = deferred();
  const action = runLocalAction(async () => { await release.promise; throw new Error("transfer failed"); });
  const rejectedAction = assert.rejects(action, /transfer failed/), rejectedClose = assert.rejects(waitForLocalActions(), /transfer failed/);
  release.resolve(); await rejectedAction; await rejectedClose; await waitForLocalActions();
});
test("nested transfer and close locks preserve original inert state until both finish", () => {
  const originalDocument = globalThis.document, originalElement = globalThis.HTMLElement;
  let blurred = 0; class Element { inert = false; blur() { blurred++; } }
  const first = new Element(), alreadyInert = new Element(); alreadyInert.inert = true;
  globalThis.HTMLElement = Element; globalThis.document = { activeElement: first, querySelectorAll: () => [first, alreadyInert] };
  try {
    const releaseTransfer = lockLocalInteraction(), releaseClose = lockLocalInteraction(); assert.equal(blurred, 1); assert.equal(first.inert, true);
    releaseTransfer(); assert.equal(first.inert, true); releaseTransfer(); assert.equal(first.inert, true);
    releaseClose(); assert.equal(first.inert, false); assert.equal(alreadyInert.inert, true);
  } finally { globalThis.document = originalDocument; globalThis.HTMLElement = originalElement; }
});