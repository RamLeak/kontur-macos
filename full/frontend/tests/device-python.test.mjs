import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const hook = await readFile(new URL("../lib/use-device-python.ts", import.meta.url), "utf8");
const files = await readFile(new URL("../components/task-spreadsheet-files.tsx", import.meta.url), "utf8");
const row = await readFile(new URL("../components/device-file-row.tsx", import.meta.url), "utf8");
const browser = await readFile(new URL("../lib/browser-python.ts", import.meta.url), "utf8");
const exam = await readFile(new URL("../components/exams-view.tsx", import.meta.url), "utf8");

test("device Python hook prefetches bounded files and keeps run output scoped locally", () => {
  assert.match(hook, /loadTaskDeviceFiles\(assetsRef\.current, lineNumber \?\? 0, revisionHash, controller\.signal, localFilesRef\.current\)/);
  assert.match(hook, /activeJobRef\.current\?\.stop\(\)/);
  assert.match(hook, /AbortController/);
  assert.match(hook, /kontur:device-python-runs:v1:/);
  assert.match(hook, /saveDevicePythonRun\(scope, result\)/);
  assert.match(hook, /loadDeviceAsset\(asset, revisionHash\)/);
  assert.doesNotMatch(hook, /loadDeviceTexts\(asset, revisionHash\)/);
  assert.match(hook, /downloadDeviceFile\(file\)/);
  assert.match(browser, /connect-src blob:/);
});

test("spreadsheet and generic file controls use device cache without exposing runtime paths", () => {
  assert.match(files, /loadDeviceTexts/);
  assert.match(files, /downloadDeviceFile/);
  assert.doesNotMatch(files, /prepareSpreadsheet/);
  assert.doesNotMatch(files, /\/input\//);
  assert.match(row, /onDownload/);
  assert.match(exam, /<DeviceFileRow/);
  assert.match(exam, /devicePython\.download\(asset\)/);
  assert.match(exam, /devicePython\.filesLoading/);
});

test("exam browser runs are marked local and never added to server run history", () => {
  assert.match(exam, /toLocalExamCodeRun/);
  assert.match(exam, /provenance: "browser"/);
  assert.match(exam, /grading: false/);
  assert.doesNotMatch(exam, /runExamCode\(sessionId, itemId/);
  assert.match(exam, /mergeExamCodeRuns/);
});

