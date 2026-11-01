import { DEMO_EXAMS } from "@/lib/demo";
import { DEMO_MODE, request } from "@/lib/api";
import { ApiError } from "@/lib/types";
import type { AssetRef, BoardDocument, ExamBlueprint, ExamDifficulty, ExamItem, ExamReport } from "@/lib/types";

export type ExamMode = "legacy" | "strict" | "study";
export type ExamPrepareKind = "full" | "thematic" | "custom" | "errors";
export type ExamLifecycle = "ready" | "active" | "paused" | "completed" | "submitted";
export type ExamPrepareDifficulty = "any" | "easy" | "medium" | "hard";

export interface ExamPrepareInput {
  kind: ExamPrepareKind;
  mode: Exclude<ExamMode, "legacy">;
  required_lines: Record<string, number>;
  difficulty: ExamPrepareDifficulty;
  duration_minutes: number;
  exclude_solved: boolean;
  exclude_recent: boolean;
  source_year: number | null;
  include_corrected_errors: boolean;
}

export interface ExamCriticalFile {
  revision_id: string | null;
  asset_id: string;
  filename: string;
  reason: string;
}

export interface ExamPrepareOptions extends ExamPrepareInput {
  profile_id?: string;
  profile_label?: string;
  source_year_basis?: string;
  recent_exclusion_days?: number | null;
  error_revision_count?: number | null;
}

export type ExamItemRecord = ExamItem & {
  task_type?: string;
  critical_files_ready?: boolean;
};

export type ExamRecord = Omit<ExamBlueprint, "items"> & {
  mode: ExamMode;
  sealed?: boolean;
  lifecycle?: ExamLifecycle;
  kind?: ExamPrepareKind;
  prepare_options?: ExamPrepareOptions;
  missing_critical_files?: ExamCriticalFile[];
  critical_files_ready?: boolean;
  items?: ExamItemRecord[];
};

export interface ExamWorkspaceCode {
  document_id: string | null;
  language: "python";
  content: string;
  version: number;
  content_hash: string | null;
  updated_at: string | null;
}

export interface ExamWorkspaceBoard {
  document_id: string | null;
  version: number;
  snapshot: Record<string, unknown>;
  payload_hash: string | null;
  updated_at: string | null;
}

export interface ExamWorkspace {
  exam_id: string;
  item_id: string;
  mode: ExamMode;
  read_only: boolean;
  deadline_at: string | null;
  server_now: string | null;
  code: ExamWorkspaceCode;
  board: ExamWorkspaceBoard;
}

export type ExamCodeRunStatus = "queued" | "running" | "succeeded" | "completed" | "failed" | "timed_out" | "unavailable";

export interface ExamCodeRun {
  id: string;
  exam_id: string;
  item_id: string;
  task_revision_id: string;
  workspace_version: number;
  code_hash: string;
  status: ExamCodeRunStatus;
  executor_status: string;
  stdout: string;
  stderr: string;
  exit_code: number | null;
  timed_out: boolean;
  duration_ms: number | null;
  resource: Record<string, unknown>;
  input_manifest: Record<string, unknown>;
  created_at: string;
  completed_at: string | null;
}

export type ExamStudyAiStatus = "queued" | "running" | "cancelling" | "succeeded" | "failed" | "timed_out" | "cancelled" | "unavailable";

export interface ExamStudyAiRun {
  id: string;
  exam_id: string;
  item_id: string;
  task_revision_id: string;
  workspace_version: number;
  context_snapshot_id: string;
  status: ExamStudyAiStatus;
  content: string;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
}

interface RawExam {
  id?: unknown;
  blueprint_id?: unknown;
  name?: unknown;
  seed?: unknown;
  scoring_version?: unknown;
  ordered_revision_ids?: unknown;
  lifecycle?: unknown;
  sealed?: unknown;
  mode?: unknown;
  started_at?: unknown;
  duration_minutes?: unknown;
  deadline_at?: unknown;
  paused_at?: unknown;
  remaining_seconds?: unknown;
  server_now?: unknown;
  difficulty?: unknown;
  target_difficulty_count?: unknown;
  rng_algorithm?: unknown;
  rng_version?: unknown;
  pool_manifest_hash?: unknown;
  items?: unknown;
  answers?: unknown;
  kind?: unknown;
  prepare_options?: unknown;
  missing_critical_files?: unknown;
  critical_files_ready?: unknown;
}

interface RawWorkspace {
  exam_id?: unknown;
  item_id?: unknown;
  mode?: unknown;
  read_only?: unknown;
  deadline_at?: unknown;
  server_now?: unknown;
  code?: unknown;
  board?: unknown;
}

interface RawExamCodeRun {
  id?: unknown;
  exam_id?: unknown;
  item_id?: unknown;
  task_revision_id?: unknown;
  workspace_version?: unknown;
  code_hash?: unknown;
  status?: unknown;
  executor_status?: unknown;
  stdout?: unknown;
  stderr?: unknown;
  exit_code?: unknown;
  timed_out?: unknown;
  duration_ms?: unknown;
  resource?: unknown;
  input_manifest?: unknown;
  created_at?: unknown;
  completed_at?: unknown;
}

interface RawExamStudyAiRun {
  id?: unknown;
  exam_id?: unknown;
  item_id?: unknown;
  task_revision_id?: unknown;
  workspace_version?: unknown;
  context_snapshot_id?: unknown;
  status?: unknown;
  content?: unknown;
  error_code?: unknown;
  created_at?: unknown;
  completed_at?: unknown;
}

const demoWorkspaces = new Map<string, ExamWorkspace>();

export async function getExamBlueprints(): Promise<ExamBlueprint[]> {
  if (DEMO_MODE) return DEMO_EXAMS;
  const payload = await request<{ items?: Array<Record<string, unknown>> }>("/exam-blueprints");
  return Array.isArray(payload.items) ? payload.items.map(normalizeBlueprint) : [];
}

export async function getActiveExam(): Promise<ExamRecord | null> {
  if (DEMO_MODE) return null;
  const payload = await request<{ active?: RawExam | null }>("/exams/active");
  if (!Object.prototype.hasOwnProperty.call(payload, "active")) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул active exam без поля active.", 200, {});
  }
  return payload.active ? normalizeExam(payload.active) : null;
}

export async function getSavedExams(): Promise<ExamRecord[]> {
  if (DEMO_MODE) return [];
  const payload = await request<{ items?: RawExam[] }>("/exams");
  return Array.isArray(payload.items) ? payload.items.map(normalizeExam) : [];
}

export async function getSavedExam(examId: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "История вариантов доступна в основной рабочей области.", 503, {});
  return normalizeExam(await request<RawExam>(`/exams/${encodeURIComponent(examId)}`));
}

export async function prepareExam(input: ExamPrepareInput, idempotencyKey?: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Подготовка варианта доступна в основной рабочей области.", 503, {});
  const key = idempotencyKey ?? `exam-prepare:${crypto.randomUUID()}`;
  return normalizeExam(await request<RawExam>("/exams/prepare", {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body: JSON.stringify(input),
  }));
}

/**
 * Reconcile the frozen critical-asset manifest for a prepared exam. The
 * device delivery endpoint records the bytes; this request only asks the
 * server to verify the observed digests before the exam can start.
 */
export async function markExamAssetsReady(examSessionId: string, idempotencyKey?: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Подготовка файлов доступна в основной рабочей области.", 503, {});
  const key = idempotencyKey ?? `exam-assets-ready:${examSessionId}`;
  return normalizeExam(await request<RawExam>(`/exams/${encodeURIComponent(examSessionId)}/assets-ready`, {
    method: "POST",
    headers: { "Idempotency-Key": key },
  }));
}

export async function startPreparedExam(examSessionId: string, idempotencyKey?: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Запуск варианта доступен в основной рабочей области.", 503, {});
  const key = idempotencyKey ?? `exam-start:${examSessionId}`;
  return normalizeExam(await request<RawExam>(`/exams/${encodeURIComponent(examSessionId)}/start`, {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body: "{}",
  }));
}

export async function startExam(blueprintId: string, mode: Exclude<ExamMode, "legacy">): Promise<ExamRecord> {
  if (DEMO_MODE) {
    const exam = DEMO_EXAMS.find((item) => item.id === blueprintId) ?? DEMO_EXAMS[0];
    return { ...exam, mode, sealed: mode === "strict", status: "active", exam_session_id: `demo-exam-session-${exam.seed}` };
  }
  return normalizeExam(await request<RawExam>("/exams", {
    method: "POST",
    body: JSON.stringify({ blueprint_id: blueprintId, seed: deterministicSeed(blueprintId), mode }),
  }));
}

export async function startDifficultyExam(
  difficulty: ExamDifficulty,
  mode: Exclude<ExamMode, "legacy">,
): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Варианты доступны в основной рабочей области.", 503, {});
  const seed = crypto.getRandomValues(new Uint32Array(1))[0];
  return normalizeExam(await request<RawExam>("/variants", {
    method: "POST",
    body: JSON.stringify({ difficulty, seed, mode }),
  }));
}

export async function pauseExam(examSessionId: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Пауза недоступна в TEST_ONLY preview.", 503, {});
  return normalizeExam(await request<RawExam>(`/exams/${encodeURIComponent(examSessionId)}/pause`, { method: "POST", body: "{}" }));
}

export async function resumeExam(examSessionId: string): Promise<ExamRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Продолжение недоступно в TEST_ONLY preview.", 503, {});
  return normalizeExam(await request<RawExam>(`/exams/${encodeURIComponent(examSessionId)}/resume`, { method: "POST", body: "{}" }));
}

export async function saveExamAnswer(examSessionId: string, itemId: string, content: string): Promise<void> {
  if (DEMO_MODE) return;
  await request(`/exams/${encodeURIComponent(examSessionId)}/answers/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

export async function submitExam(examSessionId: string): Promise<ExamReport> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Отправка недоступна в demo preview.", 503, {});
  return request<ExamReport>(`/exams/${encodeURIComponent(examSessionId)}/submit`, { method: "POST", body: "{}" });
}

export async function getExamReport(examSessionId: string): Promise<ExamReport> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Отчёт недоступен в demo preview.", 503, {});
  return request<ExamReport>(`/exams/${encodeURIComponent(examSessionId)}/report`);
}

export async function getExamWorkspace(examId: string, itemId: string): Promise<ExamWorkspace> {
  const key = workspaceKey(examId, itemId);
  if (DEMO_MODE) return cloneWorkspace(demoWorkspaces.get(key) ?? createDemoWorkspace(examId, itemId));
  return normalizeWorkspace(await request<RawWorkspace>(workspacePath(examId, itemId)));
}

export async function saveExamWorkspaceCode(
  examId: string,
  itemId: string,
  content: string,
  version: number,
): Promise<ExamWorkspace> {
  const key = workspaceKey(examId, itemId);
  if (DEMO_MODE) {
    const current = demoWorkspaces.get(key) ?? createDemoWorkspace(examId, itemId);
    if (version !== current.code.version) throw new ApiError("VERSION_CONFLICT", "Черновик кода изменён в другой вкладке.", 409, {});
    const next = cloneWorkspace(current);
    next.code = { ...next.code, content, version: version + 1, content_hash: null, updated_at: new Date().toISOString() };
    demoWorkspaces.set(key, next);
    return cloneWorkspace(next);
  }
  return normalizeWorkspace(await request<RawWorkspace>(`${workspacePath(examId, itemId)}/code`, {
    method: "PUT",
    body: JSON.stringify({ content, language: "python", version }),
  }));
}

export async function saveExamWorkspaceBoard(
  examId: string,
  itemId: string,
  board: BoardDocument,
): Promise<ExamWorkspace> {
  const key = workspaceKey(examId, itemId);
  // Images are persisted as typed asset references only.  The backend resolves
  // the owner-scoped download URL when rendering; never put a blob/object URL
  // into the immutable exam workspace snapshot.
  const boardWithImages = board as BoardDocument & { images?: unknown[] };
  const snapshot = {
    strokes: board.strokes,
    texts: board.texts,
    viewport: board.viewport,
    ...(Array.isArray(boardWithImages.images) ? { images: boardWithImages.images } : {}),
  };
  if (DEMO_MODE) {
    const current = demoWorkspaces.get(key) ?? createDemoWorkspace(examId, itemId);
    if (board.version !== current.board.version) throw new ApiError("VERSION_CONFLICT", "Черновик доски изменён в другой вкладке.", 409, {});
    const next = cloneWorkspace(current);
    next.board = { ...next.board, snapshot, version: board.version + 1, payload_hash: null, updated_at: new Date().toISOString() };
    demoWorkspaces.set(key, next);
    return cloneWorkspace(next);
  }
  return normalizeWorkspace(await request<RawWorkspace>(`${workspacePath(examId, itemId)}/board`, {
    method: "PUT",
    body: JSON.stringify({ snapshot, version: board.version }),
  }));
}

export async function listExamCodeRuns(examId: string, itemId: string): Promise<ExamCodeRun[]> {
  if (DEMO_MODE) return [];
  const payload = await request<{ items?: RawExamCodeRun[] }>(codeRunsPath(examId, itemId));
  return Array.isArray(payload.items) ? payload.items.map(normalizeExamCodeRun) : [];
}

export async function runExamCode(
  examId: string,
  itemId: string,
  version: number,
  idempotencyKey?: string,
): Promise<ExamCodeRun> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Запуск кода доступен в основной рабочей области.", 503, {});
  const key = idempotencyKey ?? `exam-code-run:${examId}:${itemId}:${version}:${Date.now()}`;
  return normalizeExamCodeRun(await request<RawExamCodeRun>(codeRunsPath(examId, itemId), {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body: JSON.stringify({ version }),
  }));
}

export async function startExamStudyAi(
  examId: string,
  itemId: string,
  input: { message: string; workspace_version: number; model?: "auto" | "gpt-5.6-luna" | "gpt-5.6-terra" | "gpt-5.6-sol"; effort?: "low" | "medium" | "high" | "xhigh"; tutor_mode?: "hint" | "hint_ladder" | "feynman" | "socratic" | "debug" | "review" },
  idempotencyKey?: string,
): Promise<ExamStudyAiRun> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Помощник доступен только в учебном варианте основного режима.", 503, {});
  const key = idempotencyKey ?? `exam-study-ai:${examId}:${itemId}:${Date.now()}`;
  return normalizeExamStudyAiRun(await request<RawExamStudyAiRun>(studyAiPath(examId, itemId), {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body: JSON.stringify({
      message: input.message,
      workspace_version: input.workspace_version,
      model: input.model ?? "auto",
      effort: input.effort ?? "medium",
      tutor_mode: input.tutor_mode ?? "hint",
    }),
  }));
}

export async function getExamStudyAi(examId: string, itemId: string, runId: string): Promise<ExamStudyAiRun> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Помощник доступен только в учебном варианте основного режима.", 503, {});
  return normalizeExamStudyAiRun(await request<RawExamStudyAiRun>(`${studyAiPath(examId, itemId)}/${encodeURIComponent(runId)}`));
}

export async function cancelExamStudyAi(examId: string, itemId: string, runId: string): Promise<ExamStudyAiRun> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Помощник доступен только в учебном варианте основного режима.", 503, {});
  return normalizeExamStudyAiRun(await request<RawExamStudyAiRun>(`${studyAiPath(examId, itemId)}/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    body: "{}",
  }));
}

const STUDY_AI_DRAFT_PREFIX = "kontur:exam-study-ai-question:v1:";

export function readExamStudyAiDraft(scope: string): string {
  if (!scope || typeof window === "undefined") return "";
  try {
    const value = window.localStorage.getItem(`${STUDY_AI_DRAFT_PREFIX}${scope}`);
    return typeof value === "string" && value.length <= 20_000 ? value : "";
  } catch {
    return "";
  }
}

export function saveExamStudyAiDraft(scope: string, content: string): void {
  if (!scope || typeof window === "undefined") return;
  try {
    if (!content) window.localStorage.removeItem(`${STUDY_AI_DRAFT_PREFIX}${scope}`);
    else if (content.length <= 20_000) window.localStorage.setItem(`${STUDY_AI_DRAFT_PREFIX}${scope}`, content);
  } catch {
    // Server-owned workspace remains authoritative when local recovery is unavailable.
  }
}

export function clearExamStudyAiDraft(scope: string): void {
  if (!scope || typeof window === "undefined") return;
  try { window.localStorage.removeItem(`${STUDY_AI_DRAFT_PREFIX}${scope}`); } catch { /* best effort */ }
}

function workspacePath(examId: string, itemId: string): string {
  return `/exams/${encodeURIComponent(examId)}/items/${encodeURIComponent(itemId)}/workspace`;
}

function codeRunsPath(examId:string,itemId:string):string {
  return `/exams/${encodeURIComponent(examId)}/items/${encodeURIComponent(itemId)}/code-runs`;
}

function studyAiPath(examId: string, itemId: string): string {
  return `/exams/${encodeURIComponent(examId)}/items/${encodeURIComponent(itemId)}/study-ai/runs`;
}

function workspaceKey(examId: string, itemId: string): string {
  return `${examId}:${itemId}`;
}

function normalizeBlueprint(raw: Record<string, unknown>): ExamBlueprint {
  const required = isRecord(raw.required_lines) ? raw.required_lines : {};
  const constraints = isRecord(raw.constraints) ? raw.constraints : {};
  const exam = isRecord(constraints.exam) ? constraints.exam : {};
  const reference = isRecord(constraints.reference) ? constraints.reference : {};
  const year = numberOrNull(raw.exam_year);
  return {
    id: stringOr(raw.id, "exam-blueprint"),
    name: stringOr(raw.name, "Тренировочный вариант"),
    year: year ?? undefined,
    blueprint_version: raw.version != null ? String(raw.version) : undefined,
    template_id: typeof reference.template_id === "string" ? reference.template_id : null,
    revision_count: Object.values(required).reduce<number>((sum: number, value: unknown) => sum + (typeof value === "number" ? value : 0), 0),
    duration_minutes: numberOrNull(exam.duration_minutes),
    status: "ready",
  };
}

function normalizeExam(raw: RawExam): ExamRecord {
  const lifecycle = stringOr(raw.lifecycle, raw.sealed === true ? "active" : "ready");
  const mode = raw.mode === "strict" || raw.mode === "study" || raw.mode === "legacy" ? raw.mode : raw.sealed === false ? "study" : "legacy";
  return {
    id: stringOr(raw.blueprint_id, stringOr(raw.id, "exam")),
    blueprint_id: typeof raw.blueprint_id === "string" ? raw.blueprint_id : undefined,
    exam_session_id: typeof raw.id === "string" ? raw.id : undefined,
    name: stringOr(raw.name, "Тренировочный вариант"),
    seed: typeof raw.seed === "string" || typeof raw.seed === "number" ? raw.seed : undefined,
    revision_ids: arrayOfStrings(raw.ordered_revision_ids),
    revision_count: arrayOfStrings(raw.ordered_revision_ids).length,
    scoring_version: stringOrNull(raw.scoring_version) ?? undefined,
    duration_minutes: numberOrNull(raw.duration_minutes),
    started_at: stringOrNull(raw.started_at),
    deadline_at: stringOrNull(raw.deadline_at),
    paused_at: stringOrNull(raw.paused_at),
    remaining_seconds: numberOrNull(raw.remaining_seconds),
    server_now: stringOrNull(raw.server_now),
    difficulty: isDifficulty(raw.difficulty) ? raw.difficulty : null,
    target_difficulty_count: numberOrNull(raw.target_difficulty_count),
    rng_algorithm: stringOrNull(raw.rng_algorithm) ?? undefined,
    rng_version: stringOrNull(raw.rng_version) ?? undefined,
    pool_manifest_hash: stringOrNull(raw.pool_manifest_hash) ?? undefined,
    items: Array.isArray(raw.items) ? raw.items.filter(isRecord).map(normalizeItem) : [],
    answers: normalizeAnswers(raw.answers),
    status: lifecycle === "completed" || lifecycle === "submitted" ? "completed" : lifecycle === "paused" ? "paused" : lifecycle === "active" || lifecycle === "sealed" ? "active" : "ready",
    mode,
    sealed: raw.sealed === true,
    lifecycle: lifecycle as ExamLifecycle,
    kind: isPrepareKind(raw.kind) ? raw.kind : undefined,
    prepare_options: normalizePrepareOptions(raw.prepare_options),
    missing_critical_files: Array.isArray(raw.missing_critical_files)
      ? raw.missing_critical_files.filter(isRecord).map(normalizeCriticalFile)
      : [],
    critical_files_ready: raw.critical_files_ready === true,
  };
}

function normalizeItem(raw: Record<string, unknown>): ExamItemRecord {
  const difficulty = raw.difficulty === "easy" ? "basic" : raw.difficulty;
  return {
    id: stringOr(raw.id, "exam-item"),
    position: numberOrNull(raw.position) ?? 0,
    task_revision_id: stringOr(raw.task_revision_id, ""),
    line_number: numberOrNull(raw.line_number),
    statement: stringOr(raw.statement, ""),
    source_id: stringOrNull(raw.source_id) ?? undefined,
    task_type: stringOrNull(raw.task_type) ?? undefined,
    critical_files_ready: raw.critical_files_ready === true,
    difficulty: isDifficulty(difficulty) ? difficulty : null,
    assets: Array.isArray(raw.assets) ? raw.assets.filter(isRecord).map(normalizeAsset) : [],
  };
}

function normalizePrepareOptions(raw: unknown): ExamPrepareOptions | undefined {
  if (!isRecord(raw) || !isPrepareKind(raw.kind) || (raw.mode !== "strict" && raw.mode !== "study")) return undefined;
  const difficulty = isPrepareDifficulty(raw.difficulty) ? raw.difficulty : "any";
  const required = isRecord(raw.required_lines)
    ? Object.fromEntries(Object.entries(raw.required_lines).flatMap(([line, count]) => typeof count === "number" ? [[line, count]] : []))
    : {};
  return {
    kind: raw.kind,
    mode: raw.mode,
    required_lines: required,
    difficulty,
    duration_minutes: numberOrNull(raw.duration_minutes) ?? 0,
    exclude_solved: raw.exclude_solved === true,
    exclude_recent: raw.exclude_recent === true,
    source_year: numberOrNull(raw.source_year),
    include_corrected_errors: raw.include_corrected_errors === true,
    profile_id: stringOrNull(raw.profile_id) ?? undefined,
    profile_label: stringOrNull(raw.profile_label) ?? undefined,
    source_year_basis: stringOrNull(raw.source_year_basis) ?? undefined,
    recent_exclusion_days: numberOrNull(raw.recent_exclusion_days),
    error_revision_count: numberOrNull(raw.error_revision_count),
  };
}

function normalizeCriticalFile(raw: Record<string, unknown>): ExamCriticalFile {
  return {
    revision_id: stringOrNull(raw.revision_id),
    asset_id: stringOr(raw.asset_id, "asset"),
    filename: stringOr(raw.filename, "Файл"),
    reason: stringOr(raw.reason, "not_ready"),
  };
}

function normalizeAsset(raw: Record<string, unknown>): AssetRef {
  const cache = raw.cache_state;
  const cacheState = cache === "metadata_only" || cache === "fetching" || cache === "ready" || cache === "failed" || cache === "quarantined" ? cache : "unknown";
  return {
    id: stringOr(raw.id, "asset"),
    name: stringOr(raw.name, stringOr(raw.filename, "Файл")),
    media_type: stringOr(raw.media_type, "application/octet-stream"),
    size_bytes: numberOrNull(raw.size_bytes),
    download_url: stringOr(raw.download_url, ""),
    available: raw.available === true,
    cache_state: cacheState,
    content_hash: stringOrNull(raw.content_hash),
    rights_status: stringOrNull(raw.rights_status),
    canonical_url: stringOrNull(raw.canonical_url),
    progress_percent: numberOrNull(raw.progress_percent),
    reason: stringOrNull(raw.reason),
  };
}

function normalizeAnswers(raw: unknown): Record<string, string> {
  if (!isRecord(raw)) return {};
  return Object.fromEntries(Object.entries(raw).flatMap(([id, value]) => {
    if (typeof value === "string") return [[id, value]];
    if (isRecord(value) && typeof value.content === "string") return [[id, value.content]];
    return [];
  }));
}

function normalizeWorkspace(raw: RawWorkspace): ExamWorkspace {
  const code = isRecord(raw.code) ? raw.code : {};
  const board = isRecord(raw.board) ? raw.board : {};
  const mode = raw.mode === "strict" || raw.mode === "study" || raw.mode === "legacy" ? raw.mode : "legacy";
  return {
    exam_id: stringOr(raw.exam_id, ""),
    item_id: stringOr(raw.item_id, ""),
    mode,
    read_only: raw.read_only === true,
    deadline_at: stringOrNull(raw.deadline_at),
    server_now: stringOrNull(raw.server_now),
    code: {
      document_id: stringOrNull(code.document_id),
      language: "python",
      content: stringOr(code.content, ""),
      version: numberOrNull(code.version) ?? 0,
      content_hash: stringOrNull(code.content_hash),
      updated_at: stringOrNull(code.updated_at),
    },
    board: {
      document_id: stringOrNull(board.document_id),
      version: numberOrNull(board.version) ?? 0,
      snapshot: isRecord(board.snapshot) ? board.snapshot : {},
      payload_hash: stringOrNull(board.payload_hash),
      updated_at: stringOrNull(board.updated_at),
    },
  };
}

function normalizeExamCodeRun(raw: RawExamCodeRun): ExamCodeRun {
  const status = raw.status === "queued" || raw.status === "running" || raw.status === "succeeded"
    || raw.status === "completed" || raw.status === "failed" || raw.status === "timed_out" || raw.status === "unavailable"
    ? raw.status
    : "unavailable";
  return {
    id: stringOr(raw.id, "exam-code-run"),
    exam_id: stringOr(raw.exam_id, ""),
    item_id: stringOr(raw.item_id, ""),
    task_revision_id: stringOr(raw.task_revision_id, ""),
    workspace_version: numberOrNull(raw.workspace_version) ?? 0,
    code_hash: stringOr(raw.code_hash, ""),
    status,
    executor_status: stringOr(raw.executor_status, "unavailable"),
    stdout: typeof raw.stdout === "string" ? raw.stdout : "",
    stderr: typeof raw.stderr === "string" ? raw.stderr : "",
    exit_code: numberOrNull(raw.exit_code),
    timed_out: raw.timed_out === true,
    duration_ms: numberOrNull(raw.duration_ms),
    resource: isRecord(raw.resource) ? raw.resource : {},
    input_manifest: isRecord(raw.input_manifest) ? raw.input_manifest : {},
    created_at: stringOr(raw.created_at, new Date(0).toISOString()),
    completed_at: stringOrNull(raw.completed_at),
  };
}

function normalizeExamStudyAiRun(raw: RawExamStudyAiRun): ExamStudyAiRun {
  const status = raw.status;
  const allowed: ExamStudyAiStatus[] = ["queued", "running", "cancelling", "succeeded", "failed", "timed_out", "cancelled", "unavailable"];
  return {
    id: stringOr(raw.id, "study-ai-run"),
    exam_id: stringOr(raw.exam_id, ""),
    item_id: stringOr(raw.item_id, ""),
    task_revision_id: stringOr(raw.task_revision_id, ""),
    workspace_version: numberOrNull(raw.workspace_version) ?? 0,
    context_snapshot_id: stringOr(raw.context_snapshot_id, ""),
    status: allowed.includes(status as ExamStudyAiStatus) ? status as ExamStudyAiStatus : "unavailable",
    content: stringOr(raw.content, ""),
    error_code: stringOrNull(raw.error_code),
    created_at: stringOr(raw.created_at, ""),
    completed_at: stringOrNull(raw.completed_at),
  };
}

function createDemoWorkspace(examId: string, itemId: string): ExamWorkspace {
  const now = new Date().toISOString();
  const workspace: ExamWorkspace = {
    exam_id: examId,
    item_id: itemId,
    mode: "legacy",
    read_only: false,
    deadline_at: null,
    server_now: now,
    code: { document_id: null, language: "python", content: "", version: 0, content_hash: null, updated_at: null },
    board: { document_id: null, version: 0, snapshot: {}, payload_hash: null, updated_at: null },
  };
  demoWorkspaces.set(workspaceKey(examId, itemId), workspace);
  return workspace;
}

function cloneWorkspace(value: ExamWorkspace): ExamWorkspace {
  return JSON.parse(JSON.stringify(value)) as ExamWorkspace;
}

function deterministicSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function isDifficulty(value: unknown): value is ExamDifficulty {
  return value === "basic" || value === "medium" || value === "hard";
}

function isPrepareKind(value: unknown): value is ExamPrepareKind {
  return value === "full" || value === "thematic" || value === "custom" || value === "errors";
}

function isPrepareDifficulty(value: unknown): value is ExamPrepareDifficulty {
  return value === "any" || value === "easy" || value === "medium" || value === "hard";
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
