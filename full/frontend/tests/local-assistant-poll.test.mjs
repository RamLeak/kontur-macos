import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
const loaded = { exports: {} };
new Function("module", "exports", ts.transpileModule(await readFile(new URL("../lib/local-assistant-poll.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(loaded, loaded.exports);
const { subscribeLocalRunStatus } = loaded.exports;
const tick = () => new Promise(resolve => setImmediate(resolve));
function clock() { const pending = []; return { pending, schedule: callback => { pending.push(callback); return callback; }, cancel: callback => { const index = pending.indexOf(callback); if (index >= 0) pending.splice(index, 1); }, now: () => 0 }; }
test("local assistant snapshots preserve status order and stop at the real terminal result", async () => {
  const time = clock(); const statuses = ["running", "succeeded"]; const seen = []; const terminal = [];
  const stop = subscribeLocalRunStatus(async () => ({ status: statuses.shift() }), { snapshot: run => seen.push(run.status), terminal: value => terminal.push(value), error: assert.fail }, time);
  await tick(); assert.deepEqual(seen, ["running"]); assert.deepEqual(terminal, []);
  time.pending.shift()(); await tick(); assert.deepEqual(seen, ["running", "succeeded"]); assert.deepEqual(terminal, ["succeeded"]); assert.equal(time.pending.length, 0); stop();
});
test("unsubscription aborts the read and suppresses a late completion", async () => {
  let release, signal; const time = clock(); const seen = [];
  const stop = subscribeLocalRunStatus(value => { signal = value; return new Promise(resolve => { release = resolve; }); }, { snapshot: value => seen.push(value), terminal: assert.fail, error: assert.fail }, time);
  stop(); assert.equal(signal.aborted, true); release({ status: "succeeded" }); await tick(); assert.deepEqual(seen, []); assert.equal(time.pending.length, 0);
});
