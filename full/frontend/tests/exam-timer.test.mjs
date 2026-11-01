import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../lib/exam-timer.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const module = { exports: {} };
new Function("module", "exports", js)(module, module.exports);
const { createExamTimerAnchor, examTimerSnapshotKey, remainingExamSeconds } = module.exports;

test("timer advances from monotonic time despite wall-clock jumps", () => {
  const snapshot = { status: "active", serverNow: "2026-10-09T10:00:00Z", remainingSeconds: 60, deadlineAt: null };
  const key = examTimerSnapshotKey(snapshot);
  const anchor = createExamTimerAnchor(snapshot, key, 1_000_000, 500);
  assert.equal(remainingExamSeconds(snapshot, anchor, 500), 60);
  assert.equal(remainingExamSeconds(snapshot, anchor, 5_501), 55);
  assert.equal(remainingExamSeconds(snapshot, anchor, 60_501), 0);
  assert.equal(anchor.key, key);
});

test("legacy wall deadline is converted once and paused snapshots use server remaining", () => {
  const deadline = new Date(1_000_000 + 20_000).toISOString();
  const active = { status: "active", serverNow: "snapshot-a", remainingSeconds: null, deadlineAt: deadline };
  const anchor = createExamTimerAnchor(active, examTimerSnapshotKey(active), 1_000_000, 2_000);
  assert.equal(remainingExamSeconds(active, anchor, 2_000), 20);
  assert.equal(remainingExamSeconds(active, anchor, 12_001), 10);

  const paused = { status: "paused", serverNow: "snapshot-b", remainingSeconds: 17, deadlineAt: deadline };
  const pausedAnchor = createExamTimerAnchor(paused, examTimerSnapshotKey(paused), 9_999_999, 100);
  assert.equal(remainingExamSeconds(paused, pausedAnchor, 999_999), 17);

  const refreshed = { ...active, serverNow: "snapshot-c", remainingSeconds: 45 };
  const refreshedAnchor = createExamTimerAnchor(refreshed, examTimerSnapshotKey(refreshed), 4_000_000, 8_000);
  assert.equal(remainingExamSeconds(refreshed, refreshedAnchor, 8_000), 45);
});
