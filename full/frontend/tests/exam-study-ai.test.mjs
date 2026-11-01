import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const api = await readFile(new URL("../lib/exam-workspace-api.ts", import.meta.url), "utf8");
const view = await readFile(new URL("../components/exams-view.tsx", import.meta.url), "utf8");

test("Study AI client keeps the server-owned exam/item/revision contract", () => {
  for (const endpoint of ["startExamStudyAi", "getExamStudyAi", "cancelExamStudyAi"]) {
    assert.match(api, new RegExp(`export async function ${endpoint}`));
  }
  assert.match(api, /study-ai\/runs/);
  assert.match(api, /workspace_version: number/);
  assert.match(api, /model: input\.model \?\? "auto"/);
  assert.match(api, /effort: input\.effort \?\? "medium"/);
  assert.match(api, /tutor_mode: input\.tutor_mode \?\? "hint"/);
  assert.match(api, /context_snapshot_id: string/);
  assert.doesNotMatch(api, /solve_session_id/);
});

test("exam Study AI flushes saved workspace before sending and preserves stale question drafts", () => {
  assert.match(view, /await flushDirtyAnswers\(\);\s*await flushOpenDrafts\(\);/);
  assert.match(view, /workspace_version:\s*latest\.code\.version/);
  assert.match(view, /saveExamStudyAiDraft\(examDraftScope\(exam\.id, itemId\), message\)/);
  assert.match(view, /Код изменился на сервере/);
  assert.match(view, /readExamStudyAiDraft/);
  assert.match(view, /clearExamStudyAiDraft/);
});

test("Study AI is study-only, pollable, cancellable, and has no technical selector UI", () => {
  assert.match(view, /if \(!isStudy \|\| studyAiBusy/);
  assert.match(view, /getExamStudyAi\(sessionId, currentItem\.id, studyAiRun\.id\)/);
  assert.match(view, /cancelExamStudyAi\(sessionId, currentItem\.id, studyAiRun\.id\)/);
  assert.match(view, /Помощник учебного варианта/);
  assert.match(view, /onClick=\{\(\) => shell\.showPanel\("assistant"\)\}/);
  assert.match(view, /"assistant\.toggle"/);
  assert.match(view, /currentItemIdRef\.current !== itemId/);
  assert.match(view, /Закрыть помощника/);
  assert.match(view, /saveStudyCheckpoint/);
  assert.doesNotMatch(view, /<select[^>]+(?:model|effort|tutor)/i);
  assert.doesNotMatch(view, /rubric|hidden_tests|answer_key/i);
});
