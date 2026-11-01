import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../lib/local-ai.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;

test("saving a public AI profile forwards only editable fields and an explicitly entered key", async () => {
  const calls = [], events = [], loaded = { exports: {} };
  new Function("module", "exports", "require", javascript)(loaded, loaded.exports, () => ({ request: async (...args) => { calls.push(args); return { schemaVersion: 1, selectedKind: "ollama", profiles: [] }; } }));
  const previous = globalThis.window;
  globalThis.window = { dispatchEvent: event => events.push(event.type) };
  try {
    const profile = { ...loaded.exports.defaultAIProfile("ollama"), id: "server-only-id", hasApiKey: true, api_key_ref: "opaque-server-only-reference" };
    await loaded.exports.saveAIProfile(profile);
    const first = JSON.parse(calls[0][1].body);
    assert.equal(calls[0][0], "/local/ai");
    assert.equal(first.selected, true);
    for (const field of ["id", "hasApiKey", "api_key_ref", "apiKey", "clearApiKey"]) assert.equal(field in first, false);
    await loaded.exports.saveAIProfile(loaded.exports.defaultAIProfile("openai_compatible"), "synthetic-explicit-key");
    assert.equal(JSON.parse(calls[1][1].body).apiKey, "synthetic-explicit-key");
    await loaded.exports.saveAIProfile(loaded.exports.defaultAIProfile("openai_compatible"), undefined, true);
    assert.equal(JSON.parse(calls[2][1].body).clearApiKey, true);
    assert.equal("apiKey" in JSON.parse(calls[2][1].body), false);
    assert.deepEqual(events, ["kontur:ai-settings-changed", "kontur:ai-settings-changed", "kontur:ai-settings-changed"]);
  } finally {
    if (previous === undefined) delete globalThis.window; else globalThis.window = previous;
  }
});
