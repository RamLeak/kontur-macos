import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => readFile(join(root, path), "utf8");

test("model policy keeps only allowlisted manual pairs and rejects invalid routes", async () => {
  const source = await read("lib/model-policy.ts");
  const api = await read("lib/api.ts");
  const transpiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
  const policy = await import(`data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`);
  assert.equal(policy.MODEL_OPTIONS.length, 12);
  for (const model of ["luna", "terra", "sol"]) for (const effort of ["low", "medium", "high", "xhigh"]) {
    assert.deepEqual(policy.normalizeSelection({ model, effort }), { model, effort });
  }
  assert.throws(() => policy.normalizeSelection({ model: "sol", effort: "unknown" }));
  assert.match(source, /throw new Error\("Выбранная комбинация модели и мощности недоступна"\)/);
  assert.doesNotMatch(source, /invalid[\s\S]{0,240}return \{ model: "auto" \}/);
  assert.match(api, /throw new ApiError\([\s\S]{0,120}"MODEL_ROUTE_INVALID"/);
  assert.doesNotMatch(api, /effort === "high" \? "high" : "medium"/);
  assert.doesNotMatch(api, /return \{ model: "auto" \};/);
});

test("API client follows v1 envelopes, ownership payloads and optimistic versions", async () => {
  const source = await read("lib/api.ts");
  assert.match(source, /NEXT_PUBLIC_API_BASE \?\? "\/api\/v1"/);
  assert.match(source, /payload\.detail/);
  assert.match(source, /function unwrapItems/);
  assert.match(source, /task_revision_id: taskRevisionId/);
  assert.match(source, /snapshot: toBoardSnapshot\(board\)/);
  assert.match(source, /version: board\.version/);
  assert.match(source, /selected_sample_ids: input\.sampleIds/);
  assert.match(source, /model_selection: toApiModel/);
  assert.doesNotMatch(source, /body: JSON\.stringify\(\{ annotations \}\)/);
  assert.match(source, /conversation_id: input\.conversationId/);
  assert.doesNotMatch(source, /conversationId\s*\?\?\s*input\.sessionId/);
  assert.match(source, /CONVERSATION_REQUIRED/);
  assert.match(source, /getLatestSolveSessionForConversation/);
  assert.match(source, /sealed_exam\?: boolean/);
  assert.match(source, /requireSealedExamFlag\(raw\.sealed_exam, sessionId\)/);
  assert.match(source, /sealed_exam: requireSealedExamFlag/);
  assert.match(await read("lib/demo.ts"), /sealed_exam: false/);
  assert.doesNotMatch(await read("components/workspace-view.tsx"), /demo-session/);
  assert.match(source, /subscribeLocalRunStatus/);
  assert.match(source, /snapshot: run => onEvent\(/);
  assert.match(source, /terminal: status => onTerminal/);
  assert.doesNotMatch(source, /new EventSource/);
  assert.match(source, /cancelAssistantRun/);
  assert.match(source, /input_digest\?: string \| null/);
  assert.match(source, /input_digest: typeof raw\.input_digest === "string"/);
  assert.match(source, /status === "timed_out"/);
  assert.match(source, /seed: deterministicSeed\(blueprintId\)/);
  assert.match(source, /getActiveExam/);
  assert.match(source, /"\/exams\/active"/);
  assert.match(source, /active\?: BackendExam \| null/);
  assert.match(source, /daily-plan\/rebuild[\s\S]*method: "POST"/);
  assert.match(source, /analytics\/rebuild[\s\S]*method: "POST"/);
  assert.match(source, /updateError/);
  assert.match(source, /completeReview/);
  assert.match(source, /trust_label/);
  assert.match(source, /character_count/);
  assert.match(source, /accuracy: metrics\.accuracy/);
  assert.match(source, /status: "unavailable"/);
  assert.doesNotMatch(source, /effective: input\.selection/);
});

test("statement rendering has no backend HTML sink and marker anchors are content-bound", async () => {
  const workspace = await read("components/workspace-view.tsx");
  const statement = await read("lib/sanitize.ts");
  assert.doesNotMatch(workspace, /dangerouslySetInnerHTML/);
  assert.match(workspace, /range\.commonAncestorContainer/);
  assert.match(workspace, /before\.toString\(\)\.length/);
  assert.match(workspace, /content_hash: task\.revision\.content_hash/);
  assert.match(workspace, /statement-highlight/);
  assert.match(statement, /statementToText/);
});

test("demo data cannot be mistaken for learner-visible KЕГЭ content", async () => {
  const demo = await read("lib/demo.ts");
  const shell = await read("components/app-shell.tsx");
  const bank = await read("components/task-bank-view.tsx");
  assert.match(demo, /source_name: "demo"/);
  assert.match(demo, /demo: true/);
  assert.match(shell, /className="demo-banner"[^>]*role="status"/);
  assert.match(shell, /Демонстрационный набор\s*·\s*не задания КЕГЭ/);
  assert.match(bank, /data-test-only=\{isDemo\}/);
  assert.match(bank, /Демонстрационный набор · исходный порядок заданий/);
});

test("board and sealed exam UI expose the required fail-closed controls", async () => {
  const board = await read("components/infinite-board.tsx");
  const composer = await read("components/composer.tsx");
  const exams = await read("components/exams-view.tsx");
  for (const tool of ["marker", "line", "rect", "ellipse", "text", "eraser"]) assert.match(board, new RegExp(`key: "${tool}"`));
  assert.match(board, /redoHistory/);
  assert.match(board, /Конфликт версии/);
  assert.match(board, /BOARD_QUOTA_EXCEEDED/);
  assert.match(composer, /sealed/);
  assert.match(composer, /Экзамен запечатан: помощник, образцы и подсказки отключены/);
  assert.match(composer, /disabled=\{sealed \|\| busy\}/);
  assert.doesNotMatch(composer, /selectedSampleIds|previewBusy|Предпросмотр контекста|previewAssistantContext/);
  assert.match(composer, /subscribeAssistantEvents/);
  assert.match(composer, /role="status"[\s\S]{0,100}assistantStatusLabel\(runStatus\)/);
  assert.match(exams, /Пока вариант не сдан, помощник, образцы кода и ответы недоступны/);
  assert.match(exams, /Без прокторинга/);
  assert.match(exams, /Revision IDs/);
});

test("learning and history actions are wired to backend-owned resources", async () => {
  const sidebar = await read("components/sidebar.tsx");
  const learning = await read("components/learning-view.tsx");
  const overview = await read("components/overview-view.tsx");
  assert.match(sidebar, /createProject/);
  assert.match(sidebar, /conversation=\$\{encodeURIComponent\(conversation\.id\)\}/);
  assert.match(learning, /updateError/);
  assert.match(learning, /getReviewQueue/);
  assert.doesNotMatch(learning, /completeReview\([^;]*true/);
  assert.match(learning, /fresh=correction/);
  assert.match(learning, /status: "confirmed"/);
  assert.match(overview, /rebuildDailyPlan/);
  assert.doesNotMatch(overview, /Демо-задача 27/);
});

test("assistant retries preserve idempotency, before-send flushing, and draft gates", async () => {
  const api = await read("lib/api.ts");
  const composer = await read("components/composer.tsx");
  assert.match(api, /function idempotencyHeaders\(key\?: string\)/);
  assert.match(api, /headers: idempotencyHeaders\(input\.idempotencyKey\)/g);
  assert.match(composer, /draftKeyRef/);
  assert.match(composer, /idempotencyKey,/);
  assert.match(composer, /const idempotencyKey = getDraftKey\(\)/);
  assert.match(composer, /beforeSend\?: \(\) => Promise<unknown>/);
  assert.match(composer, /await beforeSend\?\.\(\)/);
  assert.ok(composer.indexOf("await beforeSend?.()") < composer.indexOf("sendAssistantMessage({"));
  assert.match(composer, /kontur:composer-draft:v1/);
  assert.match(composer, /localStorage\.getItem\(draftStorageKey\)/);
  assert.match(composer, /clearComposerDraft\(draftStorageKey\)/);
  assert.match(composer, /Повторить/);
  assert.match(composer, /setText\(""\)/);
  assert.match(composer, /invalidateDraftKey\(\)/);
  assert.match(composer, /typeof result\.content === "string" && result\.content\.trim\(\)/);
  assert.doesNotMatch(composer, /previewBusy|previewAssistantContext|selectedSampleIds|Предпросмотр контекста/);
});

test("assistant active runs stay pollable until terminal or bounded timeout", async () => {
  const api = await read("lib/api.ts");
  const composer = await read("components/composer.tsx");
  assert.match(api, /const ASSISTANT_RUN_WAIT_MS = 125_000/);
  assert.match(api, /const ASSISTANT_RUN_POLL_MS = 1_000/);
  assert.match(api, /while \(!isAssistantRunTerminal\(current\.status\)\)/);
  assert.match(api, /ASSISTANT_RUN_TIMEOUT/);
  assert.match(api, /run_id: initial\.id/);
  assert.doesNotMatch(api, /attempt < 20/);
  assert.match(composer, /if \(reachedTerminal\) \{/);
  assert.match(composer, /if \(isTerminalAssistantStatus\(status\)\)/);
  assert.match(composer, /Отменяем запрос\. Дождитесь подтверждения/);
  assert.match(composer, /event\.status \|\| payloadStatus/);
  assert.doesNotMatch(composer, /event\.event_type\) setRunStatus\(event\.event_type\)/);
});

test("composer keeps technical routing in settings and submits only the working request", async () => {
  const composer = await read("components/composer.tsx");
  assert.match(composer, /usePreferences\(\)/);
  assert.match(composer, /const \{ model, effort, tutorMode \} = preferences/);
  assert.match(composer, /beforeSend\?: \(\) => Promise<unknown>/);
  assert.match(composer, /selection,/);
  assert.match(composer, /tutorMode,/);
  assert.match(composer, /disabled=\{sealed \|\| busy \|\| !text\.trim\(\)\}/);
  assert.match(composer, /clearComposerDraft\(draftStorageKey\)/);
  assert.doesNotMatch(composer, /previewBusy|previewAssistantContext|selectedSampleIds|Предпросмотр контекста/);
});

test("learner attempts are a separate backend resource from code runs", async () => {
  const api = await read("lib/api.ts");
  const workspace = await read("components/workspace-view.tsx");
  assert.match(api, /getAttempts\(sessionId: string\)/);
  assert.match(api, /\/solve-sessions\/\$\{encodeURIComponent\(sessionId\)\}\/attempts/);
  assert.match(api, /attempts\/\$\{encodeURIComponent\(attemptId\)\}/);
  assert.match(api, /method: "PATCH"/);
  for (const field of ["user_answer", "verified_correct", "independent", "hint_count", "error_type"]) assert.match(workspace, new RegExp(field));
  assert.match(workspace, /Попытки ученика/);
  assert.match(workspace, /Запуски кода/);
});

test("exam submission waits for answer saves and treats submitted lifecycle as completed", async () => {
  const api = await read("lib/api.ts");
  const exams = await read("components/exams-view.tsx");
  assert.match(exams, /pendingSaves/);
  assert.match(exams, /flushDirtyAnswers/);
  assert.match(exams, /Promise\.allSettled\(\[\.\.\.dirtyItems\.current\]\.map/);
  assert.match(exams, /Promise\.allSettled\(\[\s*(?:\.\.\.pendingSaves\.current\.values\(\)|\.\.\.\s*\[\.\.\.pendingSaves\.current\.values\(\)\])/);
  assert.match(api, /lifecycle === "completed" \|\| lifecycle === "submitted"/);
  assert.match(exams, /status: "completed"/);
  assert.match(exams, /Можно начать следующий вариант/);
});

test("2027 exam exposes server timing and attachments with resilient autosave UI", async () => {
  const api = await read("lib/api.ts");
  const exams = await read("components/exams-view.tsx");
  const types = await read("lib/types.ts");
  assert.match(api, /instantiateOfficialExamTemplate/);
  assert.match(api, /exam-blueprint-templates\/2027\/instantiate/);
  assert.match(api, /started_at\?: string \| null/);
  assert.match(api, /deadline_at\?: string \| null/);
  assert.match(exams, /OFFICIAL_2027_TEMPLATE_ID/);
  assert.match(exams, /Добавить шаблон ЕГЭ-2027/);
  assert.match(exams, /deadline_at/);
  assert.match(exams, /setInterval\(tick, 1000\)/);
  assert.match(exams, /scheduleSave/);
  assert.match(exams, /setTimeout\(/);
  assert.match(exams, /item\.position \+ 1/);
  assert.match(exams, /EXAM_TIME_EXPIRED/);
  assert.match(exams, /serverExpired/);
  assert.match(exams, /markDeadlineExpired/);
  assert.match(exams, /examExpired/);
  assert.match(exams, /disabled=\{Boolean\(report\) \|\| submitting \|\| examExpired \|\| paused \|\| pausing\}/);
  assert.match(exams, /disabled=\{submitting \|\| paused \|\| pausing \|\| resuming \|\| !items\.length\}/);
  assert.match(exams, /document\.visibilityState === "hidden"/);
  assert.match(exams, /document\.addEventListener\("visibilitychange"/);
  assert.match(exams, /void flushDirtyAnswersRef\.current\(\)\.catch/);
  assert.match(exams, /saveExamAnswer\(sessionId, itemId, content\)/);
  assert.match(exams, /Promise\.allSettled\(\[\.\.\.dirtyItems\.current\]\.map/);
  assert.match(types, /started_at\?: string \| null/);
  assert.match(types, /deadline_at\?: string \| null/);
  assert.match(types, /assets\?: AssetRef\[\]/);
});

test("exam expiry stays server-authoritative while lifecycle flush remains best effort", async () => {
  const exams = await read("components/exams-view.tsx");
  const api = await read("lib/api.ts");
  assert.match(exams, /saveExamAnswer\(sessionId, itemId, content\)/);
  assert.match(exams, /document\.visibilityState === "hidden"/);
  assert.match(exams, /document\.addEventListener\("visibilitychange"/);
  assert.match(exams, /void flushDirtyAnswersRef\.current\(\)\.catch\(\(\) => undefined\)/);
  assert.match(exams, /for \(const timer of saveTimers\.current\.values\(\)\) clearTimeout\(timer\)/);
  assert.match(exams, /Promise\.allSettled\(\[\.\.\.dirtyItems\.current\]\.map\(\(itemId\) => save\(itemId\)\)\)/);
  assert.match(exams, /isExamTimeExpired\(reason\)/);
  assert.match(exams, /reason\.code === "EXAM_TIME_EXPIRED"/);
  assert.match(exams, /Время вышло\. Новые ответы не принимаются/);
  assert.match(exams, /disabled=\{Boolean\(report\) \|\| submitting \|\| examExpired \|\| paused \|\| pausing\}/);
  assert.match(exams, /disabled=\{submitting \|\| paused \|\| pausing \|\| resuming \|\| !items\.length\}/);
  assert.match(api, /throw new ApiError\(/);
});

test("code runs await the current saved editor version and stop on conflicts", async () => {
  const api = await read("lib/api.ts");
  const workspace = await read("components/workspace-view.tsx");
  assert.match(workspace, /codeSavePromiseRef/);
  assert.match(workspace, /saved = await ensureCurrentCodeSaved\(session\.id\)/);
  assert.match(workspace, /devicePython\.run\(saved\.content, saved\.version\)/);
  assert.ok(workspace.indexOf("saved = await ensureCurrentCodeSaved(session.id)") < workspace.indexOf("devicePython.run(saved.content, saved.version)"));
  assert.match(workspace, /reason\.code === "VERSION_CONFLICT"/);
  assert.match(workspace, /сохранение и запуск отменены/);
  assert.match(api, /JSON\.stringify\(\{ content, version \}\)/);
  assert.doesNotMatch(workspace, /runCode\(session\.id, code\)/);
});

test("active exam recovery restores server answers before submit", async () => {
  const api = await read("lib/api.ts");
  const answerNormalizer = await read("lib/exam-answers.ts");
  const exams = await read("components/exams-view.tsx");
  assert.match(answerNormalizer, /function normalizeExamAnswers/);
  assert.match(answerNormalizer, /typeof answer === "string"/);
  assert.match(answerNormalizer, /\.content === "string"/);
  assert.match(api, /answers: normalizeExamAnswers\(raw\.answers\)/);
  assert.match(exams, /Promise\.all\(\[getExamBlueprints\(\), getActiveExam\(\)\]\)/);
  assert.match(exams, /mergeActiveExam\(items, active\)/);
  assert.match(exams, /useState<Record<string, string>>\(\(\) => exam\.answers \?\? \{\}\)/);
  assert.match(exams, /const serverAnswers = exam\.answers \?\? \{\}/);
  assert.match(exams, /const restored = \{ \.\.\.serverAnswers \}/);
  assert.match(exams, /answersRef\.current = restored/);
});

test("active exam answer normalizer preserves direct and answer-view content", async () => {
  const source = await read("lib/exam-answers.ts");
  const transpiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", transpiled)(module, module.exports);
  const normalizeExamAnswers = module.exports.normalizeExamAnswers;
  assert.deepEqual(normalizeExamAnswers({ first: "direct", second: { id: "answer-2", content: "restored" }, ignored: { content: 42 } }), { first: "direct", second: "restored" });
  assert.equal(normalizeExamAnswers(null), undefined);
});

test("live learning DTOs preserve unknown values instead of inventing metrics", async () => {
  const api = await read("lib/api.ts");
  const analytics = await read("components/analytics-view.tsx");
  assert.match(api, /task_line: numberOrNull\(raw\.task_line\)/);
  assert.doesNotMatch(api, /Number\(raw\.task_line \?\? 0\)/);
  assert.doesNotMatch(api, /line = revision\.line_number \?\? candidate\.line_number \?\? 0/);
  assert.match(api, /attempt_count: numberOrNull\(metrics\.attempt_count\)/);
  assert.match(api, /independent_success_count: numberOrNull\(metrics\.independent_success_count\)/);
  assert.match(api, /skill_coverage_rate: numberOrNull\(metrics\.skill_coverage_rate\)/);
  assert.match(api, /independent_rate:/);
  assert.match(analytics, /label=\{analytics\?\.solved_count != null \? "Решённые попытки" : "Попытки"\}/);
  assert.match(analytics, /suffix="попыток"/);
  assert.match(analytics, /События обучения/);
  assert.doesNotMatch(analytics, /Количество решённых задач по дням/);
});

test("learning statuses do not turn evidence or classification into resolution", async () => {
  const api = await read("lib/api.ts");
  const types = await read("lib/types.ts");
  const learning = await read("components/learning-view.tsx");
  assert.match(types, /status: "stable" \| "learning" \| "needs_attention" \| "insufficient_evidence"/);
  assert.match(api, /status === "insufficient_evidence" \? "insufficient_evidence"/);
  assert.match(api, /resolved: false/);
  assert.doesNotMatch(api, /resolved: status === "confirmed"/);
  assert.match(learning, /Неподтверждённых/);
  assert.match(learning, /Классификация подтверждена/);
  assert.match(learning, /Пока мало данных/);
  assert.match(learning, /errorStatus" \+ status\.value/);
  assert.match(learning, /status === "insufficient_evidence"/);
  assert.doesNotMatch(learning, /item\.resolved \? "Подтверждена"/);
});

test("solve sessions require the backend task revision and uncertain annotation writes retain their reconciliation intent", async () => {
  const api = await read("lib/api.ts");
  const workspace = await read("components/workspace-view.tsx");
  assert.match(api, /TASK_REVISION_MISMATCH/);
  assert.match(api, /attemptsRaw/);
  assert.match(workspace, /const saved\s*=\s*await createAnnotation\(session\.id,\s*next\)/);
  assert.match(workspace, /setPendingMark\(next\)/);
  assert.match(workspace, /Повторить сохранение выделения/);
});

test("local source import is explicit, save-gated and folders can move conversations", async () => {
  const api = await read("lib/api.ts");
  const bank = await read("components/task-bank-view.tsx");
  const source = await read("components/source-settings.tsx");
  const sidebar = await read("components/sidebar.tsx");
  assert.match(api, /source\/health/);
  assert.match(api, /importTaskFromUrl/);
  assert.match(api, /source\/import-url/);
  assert.match(api, /syncConfiguredVariant/);
  assert.match(api, /source\/sync-variants/);
  assert.match(api, /max_tasks: maxTasks/);
  assert.match(api, /source_variant_sync/);
  assert.match(api, /DEMO_ONLY/);
  assert.match(source, /Проверить состояние/);
  assert.match(source, /Импортировать \.kontur/);
  assert.match(source, /onClick=\{onImport\}/);
  const data = await read("components/local-data-settings.tsx");
  assert.match(data, /await flushOwnerWork\(\)/);
  assert.match(data, /if \(!flushed\.serverConfirmed\) throw/);
  assert.match(data, /local_import_preview/);
  assert.match(data, /local_import_confirm/);
  assert.match(source, /Данные хранятся на этом компьютере/);
  assert.match(source, /result\.count/);
  assert.match(source, /result\.failures/);
  assert.match(source, /source_variant_sync \?\? nextCapabilities\.source_sync/);
  assert.match(source, /if \(!nextSourceCapability\.available\)/);
  assert.doesNotMatch(bank, /getSourceHealth|syncConfiguredVariant/);
  assert.doesNotMatch(bank, /sourceUrl|isAllowedSourceUrl|URL редакции/);
  assert.match(sidebar, /updateConversation/);
  assert.match(sidebar, /project_id/);
  assert.match(sidebar, /getActiveProjectId/);
});

test("task catalog requests server filters and preserves truthful pagination counts", async () => {
  const api = await read("lib/api.ts");
  const bank = await read("components/task-bank-view.tsx");
  assert.match(api, /getTaskCatalog\(query: TaskCatalogQuery = \{\}\)/);
  assert.match(api, /params\.set\("exam_year", String\(query\.examYear\)\)/);
  assert.match(api, /params\.set\("line", String\(query\.line\)\)/);
  assert.match(api, /params\.set\("has_files", String\(query\.hasFiles\)\)/);
  assert.match(api, /params\.set\("cursor", query\.cursor\)/);
  assert.match(api, /total_count/);
  assert.match(api, /normalizeTaskCatalogPage/);
  assert.match(bank, /Array\.from\(\{ length: 27/);
  assert.match(bank, /yearOptions = \["Все годы", "2027"/);
  assert.match(bank, /Показать ещё задания/);
  assert.match(bank, /Загружено \$\{tasks\.length\}/);
  assert.match(bank, /Все доступные задания по текущим фильтрам загружены/);
});

test("2027 preparation target stays distinct from an observed source year", async () => {
  const types = await read("lib/types.ts");
  const api = await read("lib/api.ts");
  const bank = await read("components/task-bank-view.tsx");
  assert.match(types, /exam_year\?: number \| null/);
  assert.match(types, /target_exam_year\?: number \| null/);
  assert.match(types, /target_exam_year: number \| null/);
  assert.match(types, /targetExamYear\?: number/);
  assert.match(api, /target_exam_year\?: number \| null/);
  assert.match(api, /if \(query\.targetExamYear !== undefined\)/);
  assert.match(api, /params\.set\("target_exam_year", String\(targetExamYear\)\)/);
  assert.match(api, /target_exam_year: TARGET_EXAM_YEAR/);
  assert.match(api, /const targetExamYear = normalizeExamYear\([\s\S]{0,120}revision\.target_exam_year/);
  assert.doesNotMatch(bank, /targetExamYear: TARGET_EXAM_YEAR/);
  assert.match(bank, /const sourceYear = task\.source_year \?\? task\.revision\.source_year/);
  assert.match(bank, /task\.exam_year != null/);
  assert.doesNotMatch(bank, /ЕГЭ · \{task\.exam_year \?\?/);
});

test("live task provenance rejects demo or non-canonical records", async () => {
  const api = await read("lib/api.ts");
  assert.match(api, /SOURCE_PROVENANCE_INVALID/);
  assert.match(api, /isCanonicalKegeUrl/);
  assert.match(api, /sourceName !== "kompege"/);
  assert.match(api, /contentHash\.startsWith\("demo:"\)/);
  assert.match(api, /line < 1 \|\| line > 27/);
  assert.match(api, /TASK_REQUIRED/);
  assert.doesNotMatch(api, /getTask\(taskId = DEMO_TASK\.id\)/);
});

test("legacy ready-only asset API remains compatible while workspace downloads use device files", async () => {
  const api = await read("lib/api.ts");
  const workspace = await read("components/workspace-view.tsx");
  const types = await read("lib/types.ts");
  assert.match(api, /prepareAsset\(assetId: string\)/);
  assert.match(api, /assets\/\$\{encodeURIComponent\(assetId\)\}\/prepare/);
  assert.match(api, /cache_state: cacheState/);
  assert.match(api, /cacheState === "ready"/);
  assert.match(types, /"metadata_only"/);
  assert.match(types, /"fetching"/);
  assert.match(types, /"quarantined"/);
  const row = workspace.slice(workspace.indexOf("function AssetRow("), workspace.indexOf("function HighlightedStatement("));
  assert.match(row, /loadDeviceAsset\(asset, revisionHash\)/);
  assert.match(row, /downloadDeviceFile/);
  assert.doesNotMatch(row, /href=|onPrepare\(asset\.id\)/);
  assert.doesNotMatch(workspace, /В браузере нет локальной копии файла до явного скачивания/);
});

test("legacy CSRF helper stays isolated while private IPC never reads browser credentials", async () => {
  const source = await read("lib/csrf.ts");
  const transpiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const module = { exports: {} };
  new Function("module", "exports", transpiled)(module, module.exports);
  const { withCsrfHeader } = module.exports;
  const post = new Headers(withCsrfHeader("POST", { "Idempotency-Key": "draft-1" }, "theme=light; ege_csrf=csrf%20token"));
  assert.equal(post.get("x-csrf-token"), "csrf token");
  assert.equal(post.get("idempotency-key"), "draft-1");
  const get = new Headers(withCsrfHeader("GET", undefined, "ege_csrf=csrf-token"));
  assert.equal(get.get("x-csrf-token"), null);
  const missing = new Headers(withCsrfHeader("PATCH", undefined, "theme=light"));
  assert.equal(missing.get("x-csrf-token"), null);
  const api = await read("lib/api.ts");
  const shell = await read("components/app-shell.tsx");
  assert.match(api, /localFetch\(API_BASE \+ path/);
  assert.doesNotMatch(api, /withCsrfHeader\(|document\.cookie|credentials: "include"/);
  assert.match(api, /bootstrapOwnerSession/);
  assert.match(shell, /bootstrapOwnerSession/);
  assert.doesNotMatch(api, /owner_session_secret|codex_shared_secret|EGE_OWNER_SESSION_SECRET/);
});

test("live pages do not mount before owner session bootstrap completes", async () => {
  const api = await read("lib/api.ts");
  const shell = await read("components/app-shell.tsx");
  assert.match(api, /reason\.status === 404 \|\| reason\.status === 401/);
  assert.match(shell, /type AuthState\s*=\s*"checking"\s*\|\s*"ready"\s*\|\s*"error"\s*\|\s*"signed-out"/);
  assert.match(shell, /authState\s*===\s*"ready"\s*\?\s*children\s*:/);
  assert.match(shell, /setAuthState\("ready"\)/);
  assert.match(shell, /setAuthAttempt\(.*\+\s*1\)/);
  assert.match(shell, /readSignedOutFlag\(\)/);
  assert.match(shell, /sessionStorage\.removeItem\(SIGNED_OUT_KEY\)/);
  assert.match(shell, /authState\s*===\s*"signed-out"/);
  assert.match(shell, /authState\s*!==\s*"checking"\s*&&\s*<button/);
});
