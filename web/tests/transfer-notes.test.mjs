import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { transferNotes } = await loadTypescript("../lib/transfer-notes.ts");

test("only actual stored transfer warnings produce user notes, with unknown codes kept safe", () => {
  assert.deepEqual(transferNotes({}), []);
  const notes = transferNotes({ "kontur.legacy12.transferWarnings": ["browser_draft_time_unknown_zero", "browser_draft_time_unknown_zero", "unknown raw diagnostic"] });
  assert.equal(notes.length, 2);
  assert.match(notes[0], /Время/);
  assert.equal(notes.some(note => note.includes("unknown raw diagnostic")), false);
});
