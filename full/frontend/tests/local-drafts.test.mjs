import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const draftKey = (scope, kind) => `kontur:pending-draft:v1:${scope}:${kind}`;

async function loadDrafts() {
  const source = await readFile(join(root, "lib/local-drafts.ts"), "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", "require", transpiled)(module, module.exports, require);
  return module.exports;
}

function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    reads: [],
    writes: [],
    removals: [],
    getItem(key) {
      this.reads.push(key);
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      this.writes.push([key, value]);
      values.set(key, value);
    },
    removeItem(key) {
      this.removals.push(key);
      values.delete(key);
    },
    value(key) {
      return values.has(key) ? values.get(key) : null;
    },
  };
}

function withStorage(storage, callback) {
  const previous = globalThis.window;
  globalThis.window = { localStorage: storage };
  try {
    return callback();
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
}

test("offline save and reload preserves exact content under the opaque scope key", async () => {
  const drafts = await loadDrafts();
  const storage = createStorage();
  const scope = "session-7/exam-2:item-11";
  const content = "  print('keep this whitespace')\n";

  withStorage(storage, () => drafts.saveLocalDraft(scope, "code", 4, content));
  assert.equal(storage.writes.length, 1);
  assert.equal(storage.writes[0][0], draftKey(scope, "code"));
  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "code")).content, content);
  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "code")).baseVersion, 4);
  assert.deepEqual(storage.reads, [draftKey(scope, "code"), draftKey(scope, "code")]);
});

test("confirmed content is caller-cleared and key order does not create a false conflict", async () => {
  const drafts = await loadDrafts();
  const storage = createStorage();
  const scope = "session-7/exam-2:item-11";
  const content = { id: "item-11", source: "revision-a", answer: "42" };

  withStorage(storage, () => drafts.saveLocalDraft(scope, "answer", 9, content));
  const draft = withStorage(storage, () => drafts.readLocalDraft(scope, "answer"));
  assert.deepEqual(drafts.assessLocalDraft(draft, 10, { answer: "42", source: "revision-a", id: "item-11" }), { status: "confirmed", content: null });
  assert.ok(withStorage(storage, () => drafts.readLocalDraft(scope, "answer")), "assessment must not clear the caller's draft");
  withStorage(storage, () => drafts.clearLocalDraft(scope, "answer"));
  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "answer")), null);
});

test("same-version edits request restore while a diverged version reports conflict and retains content", async () => {
  const drafts = await loadDrafts();
  const storage = createStorage();
  const scope = "session-7/exam-2:item-11";
  withStorage(storage, () => drafts.saveLocalDraft(scope, "board", 12, { strokes: [{ id: "s1", points: [1, 2] }] }));
  const draft = withStorage(storage, () => drafts.readLocalDraft(scope, "board"));

  assert.deepEqual(drafts.assessLocalDraft(draft, 12, { strokes: [] }), { status: "restore", content: draft.content });
  assert.deepEqual(drafts.assessLocalDraft(draft, 13, { strokes: [] }), { status: "conflict", content: draft.content });
  assert.deepEqual(withStorage(storage, () => drafts.readLocalDraft(scope, "board")), draft, "conflict assessment must retain the original draft");
});

test("storage failures, broken JSON, invalid versions, and oversized values fail closed", async () => {
  const drafts = await loadDrafts();
  const scope = "session-7/exam-2:item-11";
  const storage = createStorage({ [draftKey(scope, "code")]: "{broken" });

  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "code")), null);
  assert.doesNotThrow(() => withStorage(storage, () => drafts.saveLocalDraft(scope, "code", 1, "offline")));
  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "code")).content, "offline");
  withStorage(storage, () => storage.setItem(draftKey(scope, "code"), JSON.stringify({ scope, kind: "code", baseVersion: -1, content: "bad" })));
  assert.equal(withStorage(storage, () => drafts.readLocalDraft(scope, "code")), null);
  assert.equal(storage.reads.every((key) => key === draftKey(scope, "code")), true, "only the pending-draft namespace may be read");

  const oversized = "x".repeat(2 * 1024 * 1024);
  withStorage(storage, () => drafts.saveLocalDraft(scope, "code", 1, oversized));
  assert.equal(storage.value(draftKey(scope, "code")), JSON.stringify({ scope, kind: "code", baseVersion: -1, content: "bad" }));

  const unavailable = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
    removeItem() { throw new Error("blocked"); },
  };
  assert.doesNotThrow(() => withStorage(unavailable, () => drafts.saveLocalDraft(scope, "code", 1, "offline")));
  assert.equal(withStorage(unavailable, () => drafts.readLocalDraft(scope, "code")).content, "offline");
  assert.equal(withStorage(unavailable, () => drafts.isLocalDraftDurable(scope, "code")), false);
  assert.doesNotThrow(() => withStorage(unavailable, () => drafts.clearLocalDraft(scope, "code")));
  assert.equal(withStorage(undefined, () => drafts.readLocalDraft(scope, "code")), null);
});
