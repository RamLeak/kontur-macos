import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../components/exams-view.tsx", import.meta.url), "utf8");

test("variants screen uses the server difficulty/session contract", () => {
  assert.match(source, /startDifficultyExam\(difficulty, examMode\)/);
  assert.match(source, /getSavedExams\(\)/);
  assert.match(source, /pauseExam\(sessionId\)/);
  assert.match(source, /resumeExam\(sessionId\)/);
  assert.match(source, /status === "paused"/);
  assert.match(source, /return exam\.status === "active" \|\| exam\.status === "paused"/);
  assert.match(source, /Promise\.all\(\[getExamBlueprints\(\), getActiveExam\(\)\]\)/);
  assert.match(source, /initialLoadError/);
  assert.match(source, /Повторить загрузку/);
  assert.match(source, /active \? styles\.variantSectionHidden : styles\.variantSection/);
  assert.match(source, /label: "Сложный уровень"/);
  assert.match(source, /createExamTimerAnchor/);
  assert.match(source, /monotonicNow\(\)/);
  assert.match(source, /timerAnchorRef\.current\.key !== timerSnapshotKey/);
  assert.match(source, /setServerExpired\(false\)/);
  assert.doesNotMatch(source, /Date\.parse\(exam\.server_now\)/);
});

test("runner exposes one-question navigation, protected pause, and draft flush", () => {
  assert.match(source, /aria-label="Номера заданий"/);
  assert.match(source, /currentIndex \+ 1/);
  assert.match(source, /"Пауза"/);
  assert.match(source, /await flushDirtyAnswers\(\); await flushOpenDrafts\(\); onExamUpdated\(await pauseExam/);
  assert.match(source, /disabled=\{Boolean\(report\) \|\| submitting \|\| examExpired \|\| paused \|\| pausing\}/);
  assert.match(source, /disabled=\{submitting \|\| paused \|\| pausing \|\| resuming \|\| !items\.length\}/);
  assert.match(source, /TaskImages/);
  assert.match(source, /canPrepare=\{assetFetchAvailable\}/);
});

test("submitted report is visibly training-only and item-level", () => {
  assert.match(source, /raw\.practice_only === true/);
  assert.match(source, /raw\.primary_score/);
  assert.match(source, /raw\.test_score/);
  assert.match(source, /raw\.score_scale/);
  assert.match(source, /raw\.items/);
  assert.match(source, /expectedAnswer: item\.expected_answer/);
  assert.match(source, /points: numberOrNull\(item\.points\)/);
  assert.match(source, /maxPoints: numberOrNull\(item\.max_points\)/);
  assert.match(source, /item\.position != null \? item\.position \+ 1/);
  assert.match(source, /value === "correct" \? "Верно"/);
  assert.match(source, /value === "partial" \? "Частично"/);
  assert.match(source, /value === "unanswered" \? "Без ответа"/);
  assert.match(source, /Ваш ответ/);
  assert.match(source, /Верный ответ/);
  assert.match(source, /заработано \/ максимум/);
  assert.match(source, /Тестовый балл/);
  assert.doesNotMatch(source, /Технические сведения|Хеш отчёта/);
  assert.match(source, /Старый формат подсчёта: 1 балл за задание/);
  assert.match(source, /Черновики сохраняются автоматически; разбор открывается после сдачи/);
  assert.doesNotMatch(source, /<span className=\{styles\.reportHash\}>hash/);
  assert.match(source, /blocked=\{Boolean\(active\)\}/);
  assert.match(source, /Открыть разбор/);
});
