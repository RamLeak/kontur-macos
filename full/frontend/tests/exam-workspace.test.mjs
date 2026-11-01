import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const view = await readFile(new URL("../components/exams-view.tsx", import.meta.url), "utf8");
const api = await readFile(new URL("../lib/exam-workspace-api.ts", import.meta.url), "utf8");
const css = await readFile(new URL("../components/exams-view.module.css", import.meta.url), "utf8");

test("exam start records an explicit strict or study mode and keeps legacy recovery compatible", () => {
  assert.match(api, /export type ExamMode = "legacy" \| "strict" \| "study"/);
  assert.match(api, /body: JSON\.stringify\(\{ blueprint_id: blueprintId, seed: deterministicSeed\(blueprintId\), mode \}\)/);
  assert.match(api, /body: JSON\.stringify\(\{ difficulty, seed, mode \}\)/);
  assert.match(api, /template_id: typeof reference\.template_id === "string"/);
  assert.match(view, /useState<Exclude<ExamMode, "legacy">>\("strict"\)/);
  assert.match(view, /setExamMode\("study"\)/);
  assert.match(view, /startExam\(id, examMode\)/);
});

test("each exam item has an owner-scoped versioned code and board workspace", () => {
  for (const endpoint of ["getExamWorkspace", "saveExamWorkspaceCode", "saveExamWorkspaceBoard"]) assert.match(api, new RegExp(`export async function ${endpoint}`));
  assert.match(api, /\/exams\/\$\{encodeURIComponent\(examId\)\}\/items/);
  assert.match(api, /version: number/);
  assert.match(view, /getExamWorkspace\(sessionId, itemId\)/);
  assert.match(view, /saveExamWorkspaceCode\(sessionId, itemId, content, workspace\.code\.version\)/);
  assert.match(view, /saveExamWorkspaceBoard\(sessionId, itemId, document\)/);
  assert.match(view, /examDraftScope\(exam\.id, currentItem\.id\)/);
  assert.match(view, /assessLocalDraft/);
  assert.match(view, /draftScope=\{draftScope\}/);
  assert.match(view, /Восстановить локальный/);
  assert.match(view, /persist=\{persistBoard\}/);
  assert.match(view, /key=\{`\$\{sessionId\}:\$\{itemId\}`\}/);
});

test("workspace snapshots retain typed board images while code runs stay local to the browser", () => {
  assert.match(api, /boardWithImages/);
  assert.match(api, /images: boardWithImages\.images/);
  assert.match(api, /export async function runExamCode/);
  assert.match(api, /export async function listExamCodeRuns/);
  assert.match(view, /useDevicePython/);
  assert.match(view, /browser-local/);
  assert.doesNotMatch(view, /runExamCode\(sessionId, itemId/);
  assert.match(view, /Вывод программы не является проверкой ответа/);
  assert.match(view, /Остановить/);
  assert.match(view, /Выполнение вашей программы/);
  assert.doesNotMatch(view, /Запись попытки сохранена со статусом/);
});

test("exam navigation stays neutral before final grading and supports board-first lines, flags, and finish confirmation", () => {
  assert.match(view, /new Set\(\[4, 7, 11, 12\]\)/);
  assert.match(view, /setFlaggedByItem/);
  assert.match(view, /hasAnswer/);
  assert.match(view, /styles\.draft/);
  assert.match(view, /Последняя/);
  assert.doesNotMatch(view, /Сервер подтвердил сохранение ответа/);
  assert.doesNotMatch(view, /Автосохранение включено/);
  assert.match(view, /saveLocalDraft\(examDraftScope\(exam\.id, currentItem\.id\), "answer", 0/);
  assert.match(view, /resolveExamCodeAck/);
  assert.match(view, /styles\.answerStatus/);
  assert.doesNotMatch(view, /Сохраняем черновик/);
  assert.match(view, /Завершить вариант\?/);
  assert.match(view, /finalizeAtDeadline/);
  assert.match(view, /onReport\(await submitExam\(sessionId\)\)/);
  assert.match(view, /ReportWorkspaceReview/);
  assert.match(view, /История рабочих мест/);
  assert.match(view, /raw\.mode/);
  assert.match(view, /Учебный режим/);
  assert.match(view, /active=\{false\} disabled/);
  assert.match(view, /read_only/);
  assert.match(view, /styles\.workspaceReadOnly/);
  assert.match(view, /role="timer" aria-live="off"/);
  assert.match(css, /\.solutionPane/);
  assert.match(css, /\.questionButton\.flagged/);
  assert.match(css, /\.questionButton\.draft/);
});

test("strict mode disables pause while study and legacy preserve a pause path", () => {
  assert.match(view, /const canPause = exam\.mode === "legacy" \|\| exam\.mode === "study"/);
  assert.match(view, /if \(!canPause \|\| pausing \|\| paused \|\| resuming \|\| submitting \|\| confirmFinish \|\| examExpired \|\| !sessionId\) return/);
  assert.match(view, /Строгий режим: пауза закрыта/);
  assert.match(view, /canPause \? <button/);
});

test("P4 exposes four frozen preparation kinds and separates preview from timer start", () => {
  for (const kind of ["full", "thematic", "custom", "errors"]) assert.match(api, new RegExp(`\\"${kind}\\"`));
  assert.match(api, /export interface ExamPrepareInput/);
  assert.match(api, /export type ExamPrepareDifficulty = "any" \| "easy" \| "medium" \| "hard"/);
  assert.match(api, /export async function prepareExam/);
  assert.match(api, /export async function startPreparedExam/);
  assert.match(api, /\/exams\/prepare/);
  assert.match(api, /\/exams\/\$\{encodeURIComponent\(examSessionId\)\}\/start/);
  assert.match(view, /const PREPARE_KINDS/);
  assert.match(view, /Полный/);
  assert.match(view, /Тематический/);
  assert.match(view, /Пользовательский/);
  assert.match(view, /По ошибкам/);
  assert.match(view, /<option value="easy">Базовая<\/option>/);
  assert.match(view, /function PreparedExamPreview/);
  assert.match(view, /Таймер запустится, когда вы нажмёте/);
  assert.match(view, /Проверим материалы перед стартом/);
  assert.match(view, /missing\.length === 0/);
});

test("prepared exam preloads only the frozen selected manifest and reconciles device readiness before start", () => {
  assert.match(api, /export async function markExamAssetsReady/);
  assert.match(api, /\/exams\/\$\{encodeURIComponent\(examSessionId\)\}\/assets-ready/);
  assert.match(api, /headers: \{ "Idempotency-Key": key \}/);
  assert.match(view, /function selectedPreparedAssets/);
  assert.match(view, /MAX_PREPARED_ASSET_WORKERS|PREPARED_ASSET_WORKERS/);
  assert.match(view, /loadDeviceAsset\(asset, revisionHash, signal\)/);
  assert.match(view, /loadDeviceTexts\(asset, revisionHash, signal\)/);
  assert.match(view, /MAX_FILE_BYTES/);
  assert.match(view, /deviceCacheLimitBytes\(\)/);
  assert.match(view, /лимит файлов в Настройках/);
  assert.match(view, /loadedBytes/);
  assert.match(view, /PREPARED_ASSET_ATTEMPTS/);
  const preload = view.indexOf("await preloadPreparedExamAssets(prepared, controller.signal)");
  const reconcile = view.indexOf("await markExamAssetsReady(examSessionId");
  const start = view.indexOf("await startPreparedExam(examSessionId");
  assert.ok(preload >= 0 && preload < reconcile && reconcile < start);
  assert.match(view, /ui-assets-ready:\$\{examSessionId\}/);
  assert.match(view, /preparedAssetsAbortRef\.current\?\.abort\(\)/);
});

test("browser code runs save and verify the current workspace before attaching local output", () => {
  const save = view.indexOf("await saveCode(itemId, content)");
  const execute = view.indexOf("await devicePython.run(savedContent, savedVersion)");
  assert.ok(save >= 0 && save < execute);
  assert.match(view, /workspace\.code\.content !== content/);
  assert.match(view, /workspace_version: run\.code_version \?\? workspaceVersion/);
  assert.match(view, /canCommitCodeRun\(requestId, itemId, taskRevisionId\)/);
  assert.match(view, /if \(codeRunRequestRef\.current === requestId\) setCodeRunBusy\(false\)/);
  assert.match(view, /function invalidateCodeRun\(\)/);
  assert.match(view, /invalidateCodeRun\(\); setConfirmFinish\(true\)/);
  assert.match(view, /!lifecycle\.paused/);
  assert.match(view, /!lifecycle\.submitting/);
  assert.match(view, /!deadlineExpiredRef\.current/);
  assert.match(view, /Локальная история browser Python/);
  assert.match(view, /function browserExamRunScope\(examId: string, sessionId: string, itemId: string\)/);
  assert.match(view, /readDevicePythonRuns\(browserExamRunScope\(examId, sessionId, item\.itemId\)\)/);
  assert.match(view, /function ReportLocalCodeRunHistory/);
  assert.match(view, /const reportExamId = report \? saved\.find/);
  assert.match(view, /<ReportCodeRunHistory runs=\{item\.codeRuns\} \/><ReportLocalCodeRunHistory/);
  assert.match(view, /после загрузки варианта/);
  assert.match(view, /сохранённой версии/);
  assert.match(view, /Формат подсчёта не указан/);
  assert.match(view, /const raw = report\.report/);
  assert.doesNotMatch(view, /readReport\([^)]*codeRunsByItem/);
});

test("ordinary exam setup does not expose seed or revision identifiers", () => {
  assert.doesNotMatch(view, /<Fact label="Seed"/);
  assert.doesNotMatch(view, /Revision IDs:/);
  assert.match(view, /Состав и порядок фиксируются сервером при запуске/);
});
