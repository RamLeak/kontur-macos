import { test } from "node:test";
import assert from "node:assert/strict";
import { loadTypescript } from "./load-typescript.mjs";
const { filterTasks, progressIndex, olderAttempts } = await loadTypescript("../lib/catalog.ts");

test("a solved old revision does not solve changed task content", () => {
  const task = { sourceId: "source-1", revision: "new", typeNumber: 8, title: "Слова", statement: "Условие" };
  const progress = progressIndex([{ taskSourceId: "source-1", taskRevision: "old", status: "solved", everSolved: true }]);
  assert.equal(filterTasks([task], "8", "solved", "", progress).length, 0);
  assert.equal(filterTasks([task], "8", "not_started", "", progress).length, 1);
  assert.equal(olderAttempts(task, [{ taskSourceId: "source-1", taskRevision: "old", verdict: "correct" }]), 1);
  assert.equal(olderAttempts(task, [{ taskSourceId: "source-1", taskRevision: null, verdict: "correct" }]), 0);
});

test("a later wrong verdict shows error while previous success remains separate", () => {
  const task = { sourceId: "one", revision: "same", typeNumber: 27, title: "Последовательность", statement: "Числа" };
  const progress = progressIndex([{ taskSourceId: "one", taskRevision: "same", status: "error", everSolved: true, lastVerdict: "wrong" }]);
  assert.deepEqual(filterTasks([task], "27", "error", "числа", progress), [task]);
  assert.equal(filterTasks([task], "", "solved", "", progress).length, 0);
});
