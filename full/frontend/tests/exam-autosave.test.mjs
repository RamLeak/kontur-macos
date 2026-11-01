import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const view = await readFile(new URL("../components/exams-view.tsx", import.meta.url), "utf8");
const { resolveExamCodeAck } = await import("../lib/exam-draft-ack.mjs");

test("a code edit typed after the payload leaves the newer draft visible when the ack arrives", async () => {
  let release;
  const request = new Promise((resolve) => { release = resolve; });
  let localCode = "payload";
  const sentCode = localCode;
  const ack = request.then(() => resolveExamCodeAck(localCode, sentCode, "payload"));

  localCode = "typed while saving";
  release();

  assert.deepEqual(await ack, { applied: false, content: "typed while saving" });
});

test("exam workspace uses the ack helper and rebases a newer draft before queueing the next save", () => {
  assert.match(view, /const codeAck = resolveExamCodeAck/);
  assert.match(view, /saveLocalDraft\(examDraftScope\(exam\.id, itemId\), "code", next\.code\.version, codeAck\.content\)/);
  assert.match(view, /dirtyCodeItems\.current\.add\(itemId\);\s*scheduleCodeSave\(itemId\);/);
});
