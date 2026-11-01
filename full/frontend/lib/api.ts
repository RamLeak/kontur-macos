import {
  DEMO_ANALYTICS,
  DEMO_ATTEMPTS,
  DEMO_CAPABILITIES,
  DEMO_CODE,
  DEMO_CODE_SAMPLES,
  DEMO_CONVERSATIONS,
  DEMO_ERRORS,
  DEMO_EXAMS,
  DEMO_MASTERY,
  DEMO_MESSAGES,
  DEMO_PLAN,
  DEMO_PROJECTS,
  DEMO_REVIEW,
  DEMO_SESSION,
  DEMO_TASK,
} from "@/lib/demo";
import { ApiError, TARGET_EXAM_YEAR } from "@/lib/types";
import { normalizeExamAnswers } from "@/lib/exam-answers";
import { resolveSourceNumber, sourceNumberLabel } from "@/lib/source-number";
import { plainStatementMarkup } from "@/lib/sanitize";
import { localFetch } from "./local-fetch";
import { subscribeLocalRunStatus } from "./local-assistant-poll";
import type {
  Annotation,
  AnalyticsOverview,
  Attempt,
  AssistantContextPreview,
  AssetRef,
  BoardDocument,
  Capabilities,
  CodeDocument,
  CodeRun,
  CodeSample,
  ConversationSummary,
  DailyPlan,
  ErrorEvent,
  ExamBlueprint,
  ExamDifficulty,
  ExamItem,
  ExamReport,
  MasteryState,
  Message,
  ModelSelection,
  Project,
  ReviewItem,
  SolveSession,
  SourceHealth,
  SourceVariantSyncResult,
  TaskCatalogCoverage,
  TaskCatalogCoverageCell,
  TaskCatalogPage,
  TaskCatalogQuery,
  TaskDifficulty,
  TaskRecord,
  TutorMode,
} from "@/lib/types";

import {findTestTask,newTestSession,getTestSession,saveTestCode,saveTestBoard,saveTestAnnotation,deleteTestAnnotation,testCatalog} from "./demo-workspace";
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1").replace(/\/$/, "");
export const DEMO_MODE = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

interface ErrorPayload {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
}

interface ItemsEnvelope<T> {
  items?: T[];
  next_cursor?: unknown;
  total?: unknown;
  total_count?: unknown;
}

interface BackendAsset {
  id?: string;
  filename?: string;
  name?: string;
  media_type?: string;
  detected_type?: string | null;
  size_bytes?: number | null;
  content_hash?: string | null;
  cache_state?: string | null;
  state?: string | null;
  status?: string | null;
  reason?: string | null;
  error_code?: string | null;
  progress?: number | null;
  progress_percent?: number | null;
  rights_status?: string | null;
  canonical_url?: string | null;
}

interface BackendRevision {
  id: string;
  task_id?: string;
  revision_number?: number;
  source_url?: string;
  source_id?: string | null;
  line_number?: number;
  exam_year?: number | null;
  source_year?: number | null;
  source_year_basis?: string | null;
  source_created_at?: string | null;
  source_difficulty?: unknown;
  difficulty?: unknown;
  source_label?: string | null;
  source_metadata?: unknown;
  target_exam_year?: number | null;
  statement?: string;
  statement_html?: string;
  content_hash?: string;
  retrieved_at?: string;
  assets?: BackendAsset[];
  source_name?: string | null;
  rights_status?: string | null;
}

interface BackendTask {
  id: string;
  external_id?: string | null;
  source_name?: string | null;
  line_number?: number;
  source_year?: number | null;
  source_difficulty?: unknown;
  difficulty?: unknown;
  source_label?: string | null;
  source_metadata?: unknown;
  target_exam_year?: number | null;
  revision?: BackendRevision;
}

interface BackendSolveSession {
  id: string;
  task_revision_id?: string;
  task?: BackendTask | BackendRevision;
  conversation_id?: string | null;
  sealed_exam?: boolean;
}

interface BackendProject {
  id: string;
  name: string;
  archived?: boolean;
  pinned?: boolean;
  updated_at?: string;
  created_at?: string;
}

interface BackendConversation {
  id: string;
  title?: string | null;
  archived?: boolean;
  pinned?: boolean;
  project_id?: string | null;
  last_message_at?: string | null;
  created_at?: string;
}

interface BackendCode {
  document_id?: string | null;
  solve_session_id?: string;
  language?: string;
  content?: string;
  version?: number;
  updated_at?: string | null;
}

interface BackendBoard {
  document_id?: string | null;
  version?: number;
  snapshot?: unknown;
  updated_at?: string | null;
}

interface BackendAnnotation {
  id: string;
  kind?: string;
  payload?: Record<string, unknown>;
  anchor?: Record<string, unknown>;
  source_revision_hash?: string | null;
}

interface BackendAssistantRun {
  id: string;
  content?: string;
  requested_model?: string | null;
  requested_effort?: string | null;
  effective_model?: string | null;
  effective_effort?: string | null;
  provider_info?: { id: string; kind: string; model: string; effort: string };
  error_code?: string | null;
  status?: string;
  input_digest?: string | null;
}

interface BackendAttempt {
  id: string;
  status?: string;
  user_answer?: string | null;
  verified_correct?: boolean | null;
  verification_method?: string | null;
  independent?: boolean;
  hint_count?: number;
  error_type?: string | null;
  notes?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

interface BackendSourceHealth {
  source?: string;
  origin?: string;
  mode?: string | null;
  permission_status?: string | null;
  policy_version?: string | null;
  coverage?: Record<string, unknown>;
  last_sync?: {
    id?: string;
    status?: string;
    counters?: Record<string, unknown>;
    failures?: unknown[];
  } | null;
}

interface BackendSourceVariantSync {
  duplicate?: boolean;
  source_url?: string;
  manifest_hash?: string;
  target_exam_year?: number | null;
  count?: number;
  failures?: unknown[];
  items?: Array<{ duplicate?: boolean; task?: BackendTask }>;
}

interface BackendTaskCatalogCoverageCell {
  line_number?: unknown;
  difficulty?: unknown;
  primary_available?: unknown;
  fallback_available?: unknown;
  fallback_allowed?: unknown;
  selected_primary?: unknown;
  selected_fallback?: unknown;
  total?: unknown;
  shortfall?: unknown;
}

interface BackendTaskCatalogCoverage {
  published?: unknown;
  policy_version?: unknown;
  primary_years?: unknown;
  fallback_year?: unknown;
  fallback_threshold?: unknown;
  target_per_cell?: unknown;
  total_selected?: unknown;
  quota_met?: unknown;
  updated_at?: unknown;
  cells?: unknown;
}

export interface AssistantRunEvent {
  id?: string;
  sequence?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
  status?: string;
  eof?: boolean;
}

export interface AssistantMessageInput {
  message: string;
  selection: ModelSelection;
  tutorMode?: TutorMode;
  conversationId?: string;
  sessionId?: string;
  sampleIds?: string[];
  idempotencyKey?: string;
  onRunAccepted?: (runId: string) => void;
  onRunStatus?: (status: string) => void;
}

interface BackendExamBlueprint {
  id: string;
  name: string;
  exam_year?: number | null;
  version?: number;
  required_lines?: Record<string, number>;
  constraints?: Record<string, unknown>;
}

interface BackendExam {
  id: string;
  blueprint_id?: string;
  name?: string;
  seed?: string | number | null;
  scoring_version?: string | null;
  ordered_revision_ids?: string[];
  lifecycle?: string;
  sealed?: boolean;
  started_at?: string | null;
  duration_minutes?: number | null;
  deadline_at?: string | null;
  paused_at?: string | null;
  remaining_seconds?: number | null;
  server_now?: string | null;
  difficulty?: ExamDifficulty | null;
  target_difficulty_count?: number | null;
  rng_algorithm?: string;
  rng_version?: string;
  pool_manifest_hash?: string;
  items?: Array<Record<string, unknown>>;
  answers?: Record<string, unknown>;
}

interface BackendTemplateInstantiation {
  blueprint?: BackendExamBlueprint;
  duplicate?: boolean;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    const requestHeaders = new Headers(init?.headers);
    // The private process owns auth; the allowlisted owner header only checks consistency.
    requestHeaders.set("Content-Type", "application/json");
    response = await localFetch(API_BASE + path, {
      ...init,
      headers: requestHeaders,
    });
  } catch (reason: unknown) {
    throw new ApiError(
      "API_UNREACHABLE",
      "Локальное хранилище недоступно. Повторите попытку.",
      0,
      { transport: reason instanceof Error ? reason.message : "network request failed" },
    );
  }
  const data = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) {
    const error = errorPayload(data);
    throw new ApiError(
      error.code ?? "API_ERROR",
      error.message ?? "API error " + response.status,
      response.status,
      error.details ?? {},
    );
  }
  return data as T;
}

/**
 * Give a same-origin reverse proxy a bounded opportunity to establish the
 * owner session. The endpoint is intentionally optional for local development;
 * a production error is surfaced to the shell instead of being converted into
 * demo data or an anonymous session.
 */
export async function bootstrapOwnerSession(): Promise<"ready" | "not_configured"> {
  if (DEMO_MODE || !API_BASE.startsWith("/") || typeof window === "undefined") return "not_configured";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3_000);
  try {
    await request("/auth/session", {
      method: "POST",
      body: "{}",
      signal: controller.signal,
    });
    return "ready";
  } catch (reason: unknown) {
    if (
      process.env.NODE_ENV !== "production"
      && reason instanceof ApiError
      && (reason.status === 404 || reason.status === 401)
    ) {
      return "not_configured";
    }
    throw reason;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function logoutOwnerSession(): Promise<void> {
  if (DEMO_MODE) return;
  await request("/auth/session", { method: "DELETE" });
}

function errorPayload(value: unknown): ErrorPayload {
  if (!value || typeof value !== "object") return {};
  const payload = value as Record<string, unknown>;
  if (payload.detail && typeof payload.detail === "object") return payload.detail as ErrorPayload;
  return payload as ErrorPayload;
}

function idempotencyHeaders(key?: string): HeadersInit | undefined {
  return key ? { "Idempotency-Key": key } : undefined;
}

function unwrapItems<T>(payload: ItemsEnvelope<T> | T[]): T[] {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.items)) return payload.items;
  throw new ApiError("INVALID_RESPONSE", "Backend вернул список без поля items", 200, {});
}

export function isDemoMode(): boolean {
  return DEMO_MODE;
}

export async function getCapabilities(): Promise<Capabilities> {
  if (DEMO_MODE) return DEMO_CAPABILITIES;
  return normalizeCapabilities(await request<Record<string, unknown>>("/capabilities"));
}

export async function getProjects(): Promise<Project[]> {
  if (DEMO_MODE) return DEMO_PROJECTS;
  return unwrapItems(await request<ItemsEnvelope<BackendProject>>("/projects")).map(normalizeProject);
}

export async function createProject(input: { name: string; pinned?: boolean; archived?: boolean; order_index?: number }): Promise<Project> {
  if (DEMO_MODE) return { id: `demo-project-${Date.now()}`, name: input.name, updated_at: new Date().toISOString() };
  return normalizeProject(await request<BackendProject>("/projects", {
    method: "POST",
    body: JSON.stringify({ name: input.name, pinned: input.pinned ?? false, archived: input.archived ?? false, order_index: input.order_index ?? 0 }),
  }));
}

export async function updateProject(projectId: string, patch: { name?: string; pinned?: boolean; archived?: boolean; order_index?: number }): Promise<Project> {
  if (DEMO_MODE) {
    const existing = DEMO_PROJECTS.find((item) => item.id === projectId) ?? DEMO_PROJECTS[0];
    return { ...existing, ...patch, updated_at: new Date().toISOString() };
  }
  return normalizeProject(await request<BackendProject>(`/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }));
}

export async function deleteProject(projectId: string): Promise<void> {
  if (DEMO_MODE) return;
  await request(`/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export async function getConversations(): Promise<ConversationSummary[]> {
  if (DEMO_MODE) return DEMO_CONVERSATIONS;
  return unwrapItems(await request<ItemsEnvelope<BackendConversation>>("/conversations")).map(normalizeConversation);
}

export async function createConversation(input: { title?: string | null; project_id?: string | null }): Promise<ConversationSummary> {
  if (DEMO_MODE) return { id: `demo-conversation-${Date.now()}`, title: input.title ?? "Новый разбор", project_id: input.project_id ?? null, updated_at: new Date().toISOString(), task_line: null };
  return normalizeConversation(await request<BackendConversation>("/conversations", {
    method: "POST",
    body: JSON.stringify({ title: input.title ?? null, project_id: input.project_id ?? null }),
  }));
}

export async function updateConversation(conversationId: string, patch: { title?: string | null; project_id?: string | null; pinned?: boolean; archived?: boolean }): Promise<ConversationSummary> {
  if (DEMO_MODE) {
    const existing = DEMO_CONVERSATIONS.find((item) => item.id === conversationId) ?? DEMO_CONVERSATIONS[0];
    return { ...existing, ...patch, title: patch.title === undefined ? existing.title : patch.title ?? "Без названия", project_id: patch.project_id === undefined ? existing.project_id : patch.project_id, updated_at: new Date().toISOString() };
  }
  return normalizeConversation(await request<BackendConversation>(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }));
}

export async function deleteConversation(conversationId: string): Promise<void> {
  if (DEMO_MODE) return;
  await request(`/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
}

export async function getTask(taskId?: string): Promise<TaskRecord> {
  if (DEMO_MODE) return findTestTask(taskId ?? "revision-DEMO-014-001");
  if (!taskId) throw new ApiError("TASK_REQUIRED", "Для live-каталога требуется идентификатор задачи.", 400, {});
  return normalizeTask(await request<BackendTask>("/tasks/" + encodeURIComponent(taskId)));
}


export async function getTaskRevision(taskId:string,revisionId:string):Promise<TaskRecord>{
 if(DEMO_MODE)return findTestTask(revisionId);
 const [task,history]=await Promise.all([request<BackendTask>("/tasks/"+encodeURIComponent(taskId)),request<{items:BackendRevision[]}>("/tasks/"+encodeURIComponent(taskId)+"/revisions")]);
 const revision=history.items.find(item=>item.id===revisionId);
 if(!revision||revision.task_id&&revision.task_id!==taskId)throw new ApiError("TASK_REVISION_UNAVAILABLE","Сохранённая редакция задания сейчас недоступна.",404,{});
 return normalizeTask({...task,line_number:revision.line_number??task.line_number,source_year:revision.source_year,difficulty:revision.difficulty,source_difficulty:revision.source_difficulty,revision});
}

export async function getTaskCatalog(query: TaskCatalogQuery = {}): Promise<TaskCatalogPage> {
  if (DEMO_MODE) return testCatalog(query);
  const params = taskCatalogParams(query);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return normalizeTaskCatalogPage(await request<ItemsEnvelope<BackendTask> | BackendTask[]>(`/tasks${suffix}`));
}

export async function getTaskCatalogCoverage(): Promise<TaskCatalogCoverage> {
  if (DEMO_MODE) {
    return {
      published: false,
      policy_version: null,
      primary_years: [],
      fallback_year: null,
      fallback_threshold: null,
      target_per_cell: null,
      total_selected: 0,
      quota_met: false,
      updated_at: null,
      cells: [],
    };
  }
  return normalizeTaskCatalogCoverage(await request<BackendTaskCatalogCoverage>("/tasks/coverage"));
}

function taskCatalogParams(query: TaskCatalogQuery): URLSearchParams {
  const params = new URLSearchParams();
  if (query.learningStatus) params.set("learning_status", query.learningStatus);
  if (query.bookmarksOnly) params.set("bookmarked_only", "true");
  if (query.targetExamYear !== undefined) {
    const targetExamYear = query.targetExamYear;
    if (!Number.isInteger(targetExamYear) || targetExamYear < 1) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Целевой год подготовки должен быть положительным целым числом.", 400, { target_exam_year: targetExamYear });
    }
    params.set("target_exam_year", String(targetExamYear));
  }
  if (query.examYear !== undefined) {
    if (!Number.isInteger(query.examYear) || query.examYear < 1) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Год ЕГЭ должен быть положительным целым числом.", 400, { exam_year: query.examYear });
    }
    params.set("exam_year", String(query.examYear));
  }
  if (query.sourceYear !== undefined) {
    if (!Number.isInteger(query.sourceYear) || query.sourceYear < 1) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Год источника должен быть положительным целым числом.", 400, { source_year: query.sourceYear });
    }
    params.set("source_year", String(query.sourceYear));
  }
  if (query.difficulty !== undefined) {
    if (!isTaskDifficulty(query.difficulty)) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Неизвестный уровень сложности задания.", 400, { difficulty: query.difficulty });
    }
    params.set("difficulty", query.difficulty);
  }
  if (query.sort !== undefined) {
    if (!isTaskCatalogSort(query.sort)) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Неизвестный порядок заданий.", 400, { sort: query.sort });
    }
    params.set("sort", query.sort);
  }
  if (query.catalog !== undefined) {
    if (!isTaskCatalogKind(query.catalog)) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Неизвестный каталог заданий.", 400, { catalog: query.catalog });
    }
    params.set("catalog", query.catalog);
  }
  if (query.line !== undefined) {
    if (!Number.isInteger(query.line) || query.line < 1 || query.line > 27) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Линия задания должна быть от 1 до 27.", 400, { line: query.line });
    }
    params.set("line", String(query.line));
  }
  if (query.query !== undefined) {
    if (query.query.length > 500) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Поисковый запрос слишком длинный.", 400, { query_length: query.query.length });
    }
    const value = query.query.trim();
    if (value) params.set("query", value);
  }
  if (query.hasFiles !== undefined) params.set("has_files", String(query.hasFiles));
  if (query.limit !== undefined) {
    if (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > 100) {
      throw new ApiError("CATALOG_FILTER_INVALID", "Размер страницы должен быть от 1 до 100.", 400, { limit: query.limit });
    }
    params.set("limit", String(query.limit));
  }
  if (query.cursor !== undefined && query.cursor !== null) {
    if (!query.cursor.trim()) throw new ApiError("CATALOG_CURSOR_INVALID", "Курсор страницы не может быть пустым.", 400, {});
    params.set("cursor", query.cursor);
  }
  return params;
}

function normalizeTaskCatalogPage(raw: ItemsEnvelope<BackendTask> | BackendTask[]): TaskCatalogPage {
  if (Array.isArray(raw)) return { items: raw.map(normalizeTask), next_cursor: null, total: null };
  if (!raw || !Array.isArray(raw.items)) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул каталог без массива items.", 200, {});
  }
  const nextCursor = raw.next_cursor;
  if (nextCursor !== undefined && nextCursor !== null && typeof nextCursor !== "string") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректный cursor каталога.", 200, {});
  }
  const total = normalizeCatalogTotal(raw.total ?? raw.total_count);
  return { items: raw.items.map(normalizeTask), next_cursor: nextCursor ?? null, total };
}

function normalizeCatalogTotal(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректное общее количество задач.", 200, {});
  }
  return value;
}

function normalizeTaskCatalogCoverage(raw: BackendTaskCatalogCoverage): TaskCatalogCoverage {
  if (!raw || typeof raw !== "object" || typeof raw.published !== "boolean") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул неполное покрытие каталога.", 200, {});
  }
  if (!Array.isArray(raw.cells)) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул покрытие каталога без ячеек.", 200, {});
  }
  const cells = raw.cells.map((cell) => normalizeTaskCatalogCoverageCell(cell));
  if (!raw.published && cells.length) {
    throw new ApiError("INVALID_RESPONSE", "Непубликованный каталог не должен содержать выбранные ячейки.", 200, {});
  }
  return {
    published: raw.published,
    policy_version: optionalString(raw.policy_version),
    primary_years: normalizeCoverageYears(raw.primary_years),
    fallback_year: normalizeCoverageNullableInteger(raw.fallback_year, "fallback_year"),
    fallback_threshold: normalizeCoverageNullableInteger(raw.fallback_threshold, "fallback_threshold"),
    target_per_cell: normalizeCoverageNullableInteger(raw.target_per_cell, "target_per_cell"),
    total_selected: normalizeCoverageInteger(raw.total_selected, "total_selected"),
    quota_met: raw.quota_met === true,
    updated_at: optionalString(raw.updated_at),
    cells,
  };
}

function normalizeTaskCatalogCoverageCell(raw: BackendTaskCatalogCoverageCell): TaskCatalogCoverageCell {
  if (!raw || typeof raw !== "object") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректную ячейку покрытия.", 200, {});
  }
  const lineNumber = normalizeCoverageInteger(raw.line_number, "line_number");
  if (lineNumber < 1 || lineNumber > 27) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул ячейку покрытия вне линий 1–27.", 200, { line_number: lineNumber });
  }
  const difficulty = normalizeDifficulty(raw.difficulty);
  if (difficulty == null) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул ячейку покрытия без уровня сложности.", 200, { line_number: lineNumber });
  }
  if (typeof raw.fallback_allowed !== "boolean") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул ячейку покрытия без правила резерва.", 200, { line_number: lineNumber, difficulty });
  }
  return {
    line_number: lineNumber,
    difficulty,
    primary_available: normalizeCoverageInteger(raw.primary_available, "primary_available"),
    fallback_available: normalizeCoverageInteger(raw.fallback_available, "fallback_available"),
    fallback_allowed: raw.fallback_allowed,
    selected_primary: normalizeCoverageInteger(raw.selected_primary, "selected_primary"),
    selected_fallback: normalizeCoverageInteger(raw.selected_fallback, "selected_fallback"),
    total: normalizeCoverageInteger(raw.total, "total"),
    shortfall: normalizeCoverageInteger(raw.shortfall, "shortfall"),
  };
}

function normalizeCoverageYears(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.map((year) => normalizeCoverageInteger(year, "primary_years")).filter((year) => year > 0);
}

function normalizeCoverageInteger(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new ApiError("INVALID_RESPONSE", `Backend вернул некорректное поле покрытия ${field}.`, 200, { field, value });
  }
  return value;
}

function normalizeCoverageNullableInteger(value: unknown, field: string): number | null {
  if (value === undefined || value === null) return null;
  return normalizeCoverageInteger(value, field);
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function isTaskDifficulty(value: unknown): value is TaskDifficulty {
  return value === "easy" || value === "medium" || value === "hard";
}

function isTaskCatalogSort(value: unknown): value is TaskCatalogQuery["sort"] {
  return value === "number" || value === "newest" || value === "difficulty_asc" || value === "difficulty_desc";
}

function isTaskCatalogKind(value: unknown): value is TaskCatalogQuery["catalog"] {
  return value === "current" || value === "recent" || value === "all";
}

export async function prepareAsset(assetId: string): Promise<AssetRef> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Подготовка файлов недоступна в TEST_ONLY preview.", 503, {});
  if (!assetId.trim()) throw new ApiError("ASSET_REQUIRED", "Для подготовки нужен идентификатор файла.", 400, {});
  const raw = await request<BackendAsset | { asset?: BackendAsset }>(
    `/assets/${encodeURIComponent(assetId)}/prepare`,
    { method: "POST", body: "{}" },
  );
  const candidate = raw && typeof raw === "object" && "asset" in raw && raw.asset && typeof raw.asset === "object"
    ? raw.asset
    : raw as BackendAsset;
  if (!candidate || typeof candidate !== "object") {
    throw new ApiError("INVALID_RESPONSE", "Backend не вернул состояние подготовленного файла.", 200, { asset_id: assetId });
  }
  return normalizeAsset(candidate, assetId);
}

export interface SpreadsheetFile {
  filename: string;
  sheet_name: string;
  size_bytes: number;
  content_hash: string;
  download_url: string;
}

export async function prepareSpreadsheet(assetId: string): Promise<{ asset: AssetRef; files: SpreadsheetFile[] }> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Файлы доступны после подключения банка.", 503, {});
  const raw = await request<{ asset: BackendAsset; files: Array<Omit<SpreadsheetFile, "download_url"> & { sheet: number }> }>(
    `/assets/${encodeURIComponent(assetId)}/prepare-text`, { method: "POST", body: "{}" },
  );
  if (!Array.isArray(raw.files) || raw.files.length === 0 || raw.files.length > 16) {
    throw new ApiError("INVALID_RESPONSE", "Не удалось получить текстовые файлы.", 200, {});
  }
  return { asset: normalizeAsset(raw.asset, assetId), files: raw.files.map(file => {
    if (!/^[A-Za-z0-9._-]+\.txt$/.test(file.filename) || !Number.isInteger(file.sheet) || file.sheet < 0) {
      throw new ApiError("INVALID_RESPONSE", "Некорректный текстовый файл.", 200, {});
    }
    return { ...file, download_url: `${API_BASE}/assets/${encodeURIComponent(assetId)}/text?sheet=${file.sheet}` };
  }) };
}

export async function getSourceHealth(): Promise<SourceHealth> {
  if (DEMO_MODE) {
    return {
      source: "demo",
      origin: "demo://fixture",
      mode: "test-only",
      permission_status: "test_only",
      policy_version: "demo",
      coverage: {},
      last_sync: null,
    };
  }
  return normalizeSourceHealth(await request<BackendSourceHealth>("/source/health"));
}

export async function importTaskFromUrl(sourceUrl: string, idempotencyKey?: string): Promise<TaskRecord> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Синхронизация недоступна в TEST_ONLY preview.", 503, {});
  const raw = await request<{ task?: BackendTask }>("/source/import-url", {
    method: "POST",
    headers: idempotencyHeaders(idempotencyKey),
    body: JSON.stringify({ source_url: sourceUrl, target_exam_year: TARGET_EXAM_YEAR }),
  });
  if (!raw.task) throw new ApiError("INVALID_RESPONSE", "Источник не вернул импортированную задачу.", 200, {});
  return normalizeTask(raw.task);
}

export async function syncConfiguredVariant(maxTasks = 27, idempotencyKey?: string): Promise<SourceVariantSyncResult> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Синхронизация недоступна в TEST_ONLY preview.", 503, {});
  if (!Number.isInteger(maxTasks) || maxTasks < 1 || maxTasks > 27) {
    throw new ApiError("SOURCE_BATCH_INVALID", "Количество заданий варианта должно быть от 1 до 27.", 400, { max_tasks: maxTasks });
  }
  const raw = await request<BackendSourceVariantSync>("/source/sync-variants", {
    method: "POST",
    headers: idempotencyHeaders(idempotencyKey),
    body: JSON.stringify({ max_tasks: maxTasks, target_exam_year: TARGET_EXAM_YEAR }),
  });
  if (!Array.isArray(raw.items) || !Array.isArray(raw.failures) || typeof raw.count !== "number" || !Number.isFinite(raw.count)) {
    throw new ApiError("INVALID_RESPONSE", "Источник вернул вариант без count, failures или items.", 200, {});
  }
  const tasks = raw.items.map((item, index) => {
    if (!item || typeof item !== "object" || !item.task) {
      throw new ApiError("INVALID_RESPONSE", `Источник не вернул задачу для позиции ${index + 1}.`, 200, { index });
    }
    return normalizeTask(item.task);
  });
  return {
    duplicate: raw.duplicate === true,
    source_url: typeof raw.source_url === "string" ? raw.source_url : null,
    manifest_hash: typeof raw.manifest_hash === "string" ? raw.manifest_hash : null,
    target_exam_year: normalizeExamYear(raw.target_exam_year, "source-variant", "target_exam_year"),
    count: raw.count,
    failures: raw.failures,
    tasks,
  };
}

export async function getSolveSession(sessionId?: string): Promise<SolveSession> {
  if (DEMO_MODE) return getTestSession(sessionId);
  if (!sessionId) throw new ApiError("SESSION_REQUIRED", "Выберите рабочую сессию из истории.", 400, {});
  const raw = await request<BackendSolveSession>(
    "/solve-sessions/" + encodeURIComponent(sessionId),
  );
  if (!raw.task) throw new ApiError("TASK_MISSING", "Solve session не содержит task revision", 200, { session_id: sessionId });
  const task = normalizeTask(raw.task);
  if (raw.task_revision_id && task.revision.id !== raw.task_revision_id) {
    throw new ApiError("TASK_REVISION_MISMATCH", "Solve session вернула другую редакцию задачи.", 200, { expected: raw.task_revision_id, received: task.revision.id });
  }
  const [codeRaw, boardRaw, annotationRaw, runsRaw, attemptsRaw, messages] = await Promise.all([
    request<BackendCode>(`/solve-sessions/${encodeURIComponent(sessionId)}/code`),
    request<BackendBoard>(`/solve-sessions/${encodeURIComponent(sessionId)}/board`),
    request<ItemsEnvelope<BackendAnnotation>>(`/solve-sessions/${encodeURIComponent(sessionId)}/annotations`),
    request<ItemsEnvelope<Record<string, unknown>>>(`/solve-sessions/${encodeURIComponent(sessionId)}/code-runs`),
    request<ItemsEnvelope<BackendAttempt>>(`/solve-sessions/${encodeURIComponent(sessionId)}/attempts`),
    raw.conversation_id ? getMessages(raw.conversation_id) : Promise.resolve([] as Message[]),
  ]);
  const versionsRaw = codeRaw.document_id && unwrapItems(runsRaw).length > 0
    ? await request<ItemsEnvelope<Record<string, unknown>>>(`/solve-sessions/${encodeURIComponent(sessionId)}/code/versions`)
    : { items: [] };
  return {
    id: raw.id,
    conversation_id: raw.conversation_id ?? null,
    task,
    messages,
    code: normalizeCode(codeRaw, sessionId),
    runs: unwrapItems(runsRaw).map(item=>normalizeRun({...item,code_version:unwrapItems(versionsRaw).find(version=>version.id===item.code_version_id)?.version??null})), 
    attempts: unwrapItems(attemptsRaw).map(normalizeAttempt),
    board: normalizeBoard(boardRaw, sessionId),
    annotations: unwrapItems(annotationRaw).map(normalizeAnnotation),
    sealed_exam: requireSealedExamFlag(raw.sealed_exam, sessionId),
  };
}

export async function getLatestSolveSession(): Promise<SolveSession> {
  if (DEMO_MODE) return getTestSession();
  const items = unwrapItems(await request<ItemsEnvelope<{ id: string; conversation_id?: string | null }>>("/solve-sessions"));
  const latest = items[0];
  if (!latest) throw new ApiError("SESSION_NOT_FOUND", "Активных рабочих сессий пока нет.", 404, {});
  return getSolveSession(latest.id);
}

function requireSealedExamFlag(value: unknown, sessionId: string): boolean {
  if (typeof value !== "boolean") {
    throw new ApiError("INVALID_RESPONSE", "Solve session не вернула truthful sealed_exam flag.", 200, { session_id: sessionId });
  }
  return value;
}

export async function getLatestSolveSessionForConversation(conversationId: string): Promise<SolveSession> {
  if (DEMO_MODE) return getTestSession(conversationId);
  const items = unwrapItems(await request<ItemsEnvelope<{ id: string; conversation_id?: string | null }>>("/solve-sessions"));
  const match = items.find((item) => item.conversation_id === conversationId);
  if (!match) throw new ApiError("SESSION_NOT_FOUND", "Для этого разговора ещё нет рабочей сессии.", 404, { conversation_id: conversationId });
  return getSolveSession(match.id);
}

export async function createSolveSession(taskRevisionId: string, projectId: string | null = null): Promise<SolveSession> {
  if (DEMO_MODE) return newTestSession(taskRevisionId,true);
  const conversation = await request<{ id: string }>("/conversations", {
    method: "POST",
    body: JSON.stringify({ title: "Новый разбор", project_id: projectId }),
  });
  const created = await request<{ id: string }>("/solve-sessions", {
    method: "POST",
    body: JSON.stringify({ task_revision_id: taskRevisionId, conversation_id: conversation.id }),
  });
  return getSolveSession(created.id);
}

export async function saveCode(sessionId: string, content: string, version: number): Promise<CodeDocument> {
  if (DEMO_MODE) return saveTestCode(sessionId,content,version);
  return normalizeCode(await request<BackendCode>(`/solve-sessions/${encodeURIComponent(sessionId)}/code`, {
    method: "PUT",
    body: JSON.stringify({ content, language: "python", version }),
  }), sessionId);
}

export async function getAttempts(sessionId: string): Promise<Attempt[]> {
  if (DEMO_MODE) return DEMO_ATTEMPTS;
  return unwrapItems(await request<ItemsEnvelope<BackendAttempt>>(`/solve-sessions/${encodeURIComponent(sessionId)}/attempts`)).map(normalizeAttempt);
}

export interface AttemptInput {
  status: Attempt["status"];
  user_answer?: string | null;
  verified_correct?: boolean | null;
  verification_method?: string | null;
  independent?: boolean;
  hint_count?: number;
  error_type?: string | null;
  notes?: string | null;
}

export async function createAttempt(sessionId: string, input: AttemptInput, idempotencyKey?: string): Promise<Attempt> {
  if (DEMO_MODE) {
    return normalizeAttempt({ id: `demo-attempt-${Date.now()}`, ...input, started_at: new Date().toISOString(), completed_at: input.status === "in_progress" ? null : new Date().toISOString() });
  }
  return normalizeAttempt(await request<BackendAttempt>(`/solve-sessions/${encodeURIComponent(sessionId)}/attempts`, {
    method: "POST",
    headers: idempotencyHeaders(idempotencyKey),
    body: JSON.stringify({
      status: input.status,
      user_answer: input.user_answer ?? null,
      verified_correct: input.verified_correct ?? null,
      verification_method: input.verification_method ?? null,
      independent: input.independent ?? true,
      hint_count: input.hint_count ?? 0,
      error_type: input.error_type ?? null,
      notes: input.notes ?? null,
    }),
  }));
}

export async function updateAttempt(sessionId: string, attemptId: string, patch: Partial<AttemptInput>): Promise<Attempt> {
  if (DEMO_MODE) {
    const existing = DEMO_ATTEMPTS.find((item) => item.id === attemptId) ?? DEMO_ATTEMPTS[0];
    return { ...existing, ...patch, user_answer: patch.user_answer === undefined ? existing.user_answer : patch.user_answer ?? null, error_type: patch.error_type === undefined ? existing.error_type : patch.error_type ?? null, notes: patch.notes === undefined ? existing.notes : patch.notes ?? null, completed_at: patch.status && patch.status !== "in_progress" ? new Date().toISOString() : existing.completed_at };
  }
  return normalizeAttempt(await request<BackendAttempt>(`/solve-sessions/${encodeURIComponent(sessionId)}/attempts/${encodeURIComponent(attemptId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }));
}

export async function runCode(sessionId: string, content: string, version: number): Promise<CodeRun> {
  if (DEMO_MODE) return { id: "demo-run-" + Date.now(), status: "unavailable", stdout: "", stderr: "", duration_ms: null, exit_code: null, created_at: new Date().toISOString(), reason: "Демо-режим: production runner не подключён." };
  const raw = await request<Record<string, unknown>>(`/solve-sessions/${encodeURIComponent(sessionId)}/code-runs`, { method: "POST", body: JSON.stringify({ content, version }) });
  return {...normalizeRun(raw),code_version:version};
}

export async function saveBoard(sessionId: string, board: BoardDocument): Promise<BoardDocument> {
  if (DEMO_MODE) return saveTestBoard(sessionId,board);
  const raw = await request<BackendBoard>(`/solve-sessions/${encodeURIComponent(sessionId)}/board`, {
    method: "PUT",
    body: JSON.stringify({ snapshot: toBoardSnapshot(board), version: board.version }),
  });
  return normalizeBoard(raw, sessionId);
}

export async function createAnnotation(sessionId: string, annotation: Annotation): Promise<Annotation> {
  if (DEMO_MODE) return saveTestAnnotation(sessionId,annotation);
  const anchor = annotation.anchor ?? { start: 0, end: 0, content_hash: "" };
  const backendAnchor: Record<string, unknown> = {
    start: anchor.start,
    end: anchor.end,
    quote: annotation.text,
    source_revision_hash: anchor.content_hash,
  };
  if (anchor.ink) backendAnchor.ink = anchor.ink;
  const raw = await request<BackendAnnotation>(`/solve-sessions/${encodeURIComponent(sessionId)}/annotations`, {
    method: "POST",
    headers: {"Idempotency-Key": `annotation:${sessionId}:${annotation.id}`},
    body: JSON.stringify({
      kind: annotation.kind === "highlight" ? "text" : "ink",
      payload: { text: annotation.text, color: annotation.color, points: annotation.points, source_revision_hash: anchor.content_hash },
      anchor: backendAnchor,
    }),
  });
  return normalizeAnnotation(raw);
}

export async function deleteAnnotation(sessionId: string, annotationId: string): Promise<void> {
  if (DEMO_MODE) { deleteTestAnnotation(sessionId, annotationId); return; }
  const ack = await request<{deleted?: boolean; id?: string}>(`/annotations/${encodeURIComponent(annotationId)}`, {method:"DELETE"});
  if (ack.deleted !== true || ack.id !== annotationId) throw new ApiError("ANNOTATION_DELETE_UNCONFIRMED", "Удаление пометки не подтверждено. Повторите попытку.", 200, {});
}

export async function getMessages(conversationId: string): Promise<Message[]> {
  if (DEMO_MODE) return DEMO_MESSAGES;
  return unwrapItems(await request<ItemsEnvelope<Record<string, unknown>>>(`/conversations/${encodeURIComponent(conversationId)}/messages`)).map(normalizeMessage);
}

export async function sendAssistantMessage(input: AssistantMessageInput): Promise<{ id: string; content: string; requested: ModelSelection; effective: ModelSelection | null; provider_label?: string; status: string; error_code?: string | null; input_digest: string | null }> {
  if (DEMO_MODE) return { id: "demo-assistant-" + Date.now(), content: "Демо-режим: Codex gateway недоступен. Запрос не выполнялся; backend action unavailable.", requested: input.selection, effective: null, status: "unavailable", error_code: "DEMO_ONLY", input_digest: null };
  requireConversation(input);
  const raw = await request<BackendAssistantRun>("/assistant/runs", { method: "POST", headers: idempotencyHeaders(input.idempotencyKey), body: JSON.stringify(assistantPayload(input)) });
  const accepted = input.onRunAccepted;
  accepted?.(raw.id);
  input.onRunStatus?.(raw.status ?? "queued");
  const final = await waitForAssistantRun(raw, input.onRunStatus);
  return normalizeAssistantRun(final, input.selection);
}

export async function getAssistantRun(runId: string): Promise<BackendAssistantRun> {
  return request<BackendAssistantRun>(`/assistant/runs/${encodeURIComponent(runId)}`);
}

export async function cancelAssistantRun(runId: string): Promise<BackendAssistantRun> {
  return request<BackendAssistantRun>(`/assistant/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: JSON.stringify({}) });
}

const ASSISTANT_RUN_WAIT_MS = 125_000;
const ASSISTANT_RUN_POLL_MS = 1_000;
const ASSISTANT_RUN_TERMINAL_STATUSES = new Set(["succeeded", "failed", "timed_out", "cancelled", "unavailable"]);

async function waitForAssistantRun(initial: BackendAssistantRun, onStatus?: (status: string) => void): Promise<BackendAssistantRun> {
  let current = initial;
  const deadline = Date.now() + ASSISTANT_RUN_WAIT_MS;
  onStatus?.(current.status ?? "unknown");
  while (!isAssistantRunTerminal(current.status)) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      throw new ApiError(
        "ASSISTANT_RUN_TIMEOUT",
        "Assistant всё ещё выполняется; polling остановлен по безопасному тайм-ауту.",
        504,
        { run_id: initial.id, status: current.status ?? null, timeout_ms: ASSISTANT_RUN_WAIT_MS },
      );
    }
    await new Promise((resolve) => setTimeout(resolve, Math.min(ASSISTANT_RUN_POLL_MS, remaining)));
    current = await getAssistantRun(initial.id);
    onStatus?.(current.status ?? "unknown");
  }
  return current;
}

function isAssistantRunTerminal(status: string | null | undefined): boolean {
  return typeof status === "string" && ASSISTANT_RUN_TERMINAL_STATUSES.has(status);
}

export function subscribeAssistantEvents(
  runId: string,
  onEvent: (event: AssistantRunEvent) => void,
  onTerminal?: (status: string) => void,
  onError?: (error: ApiError) => void,
): () => void {
  return subscribeLocalRunStatus(
    signal => request<BackendAssistantRun>(`/assistant/runs/${encodeURIComponent(runId)}`, { signal }),
    {
      snapshot: run => onEvent({ event_type: "snapshot", status: run.status ?? undefined, payload: { status: run.status } }),
      terminal: status => onTerminal?.(status),
      error: () => onError?.(new ApiError("EVENTS_UNAVAILABLE", "Не удалось обновить статус помощника. Продолжаем ожидание ответа.", 503, {})),
    },
  );
}

export async function previewAssistantContext(input: AssistantMessageInput): Promise<AssistantContextPreview> {
  if (DEMO_MODE) return { content: "Демо preview: контекст не отправлялся и Codex не запускался.", trust_label: "TEST_ONLY / preview", character_count: input.message.length, sections: [{ name: "Условие и редакция", trust_label: "TEST_ONLY", chars: 0, character_count: 0, included: true }, { name: "Сообщение", trust_label: "USER_REQUEST", chars: input.message.length, character_count: input.message.length, included: true }, { name: "Образцы по выбору", trust_label: "PERSONAL_EXAMPLE", chars: input.sampleIds?.length ?? 0, character_count: input.sampleIds?.length ?? 0, included: Boolean(input.sampleIds?.length) }], route: { reason: "preview only; effective route не назначен" }, redaction_count: 0, redaction_counts: { total: 0, sections: 0 } };
  requireConversation(input);
  const raw = await request<Record<string, unknown>>("/assistant/context-preview", { method: "POST", headers: idempotencyHeaders(input.idempotencyKey), body: JSON.stringify(assistantPayload(input)) });
  return normalizeContextPreview(raw);
}

function assistantPayload(input: AssistantMessageInput) {
  return { message: input.message, model_selection: toApiModel(input.selection), effort: input.selection.effort ?? null, tutor_mode: input.tutorMode ?? "hint", conversation_id: input.conversationId, solve_session_id: input.sessionId, selected_sample_ids: input.sampleIds ?? [] };
}

function requireConversation(input: { conversationId?: string }): string {
  if (!input.conversationId) throw new ApiError("CONVERSATION_REQUIRED", "Для tutor нужен связанный conversation; solve-сессия не используется вместо него.", 400, {});
  return input.conversationId;
}

function toApiModel(selection: ModelSelection): string {
  if (selection.model === "auto") return "auto";
  return `gpt-5.6-${selection.model}`;
}

export async function getCodeSamples(): Promise<CodeSample[]> {
  if (DEMO_MODE) return DEMO_CODE_SAMPLES;
  return unwrapItems(await request<ItemsEnvelope<Record<string, unknown>>>("/code-samples")).map(normalizeSample);
}

export async function createCodeSample(input: { title: string; language: string; content: string; task_lines: number[]; tags: string[]; usage_note: string }): Promise<CodeSample> {
  if (DEMO_MODE) return { id: "demo-sample-" + Date.now(), ...input, enabled: true, version: 1, content_hash: "demo-local-preview" };
  return normalizeSample(await request<Record<string, unknown>>("/code-samples", { method: "POST", body: JSON.stringify({ ...input, enabled: true }) }));
}

export async function updateCodeSample(sampleId: string, patch: Partial<Pick<CodeSample, "title" | "content" | "task_lines" | "tags" | "usage_note" | "enabled">>): Promise<CodeSample> {
  if (DEMO_MODE) {
    const existing = DEMO_CODE_SAMPLES.find((sample) => sample.id === sampleId) ?? DEMO_CODE_SAMPLES[0];
    return { ...existing, ...patch, version: existing.version + 1 };
  }
  return normalizeSample(await request<Record<string, unknown>>(`/code-samples/${encodeURIComponent(sampleId)}`, { method: "PATCH", body: JSON.stringify(patch) }));
}

export async function getErrors(): Promise<ErrorEvent[]> {
  if (DEMO_MODE) return DEMO_ERRORS;
  return unwrapItems(await request<ItemsEnvelope<Record<string, unknown>>>("/errors")).map(normalizeError);
}

export async function updateError(errorId: string, patch: { status: "proposed" | "confirmed" | "rejected"; description?: string; prevention_note?: string }): Promise<ErrorEvent> {
  if (DEMO_MODE) {
    const existing = DEMO_ERRORS.find((item) => item.id === errorId) ?? DEMO_ERRORS[0];
    return { ...existing, resolved: false, status: patch.status, note: patch.prevention_note ?? patch.description ?? existing.note };
  }
  const raw = await request<{ error?: Record<string, unknown> }>(`/errors/${encodeURIComponent(errorId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  return normalizeError(raw.error ?? (raw as Record<string, unknown>));
}

export async function getMastery(): Promise<MasteryState[]> {
  if (DEMO_MODE) return DEMO_MASTERY;
  return unwrapItems(await request<ItemsEnvelope<Record<string, unknown>>>("/mastery")).map(normalizeMastery);
}

export async function getReviewQueue(): Promise<ReviewItem[]> {
  if (DEMO_MODE) return DEMO_REVIEW;
  return unwrapItems(await request<ItemsEnvelope<Record<string, unknown>>>("/review-queue")).map(normalizeReview);
}

export async function completeReview(reviewId: string, success: boolean): Promise<ReviewItem> {
  if (DEMO_MODE) {
    const existing = DEMO_REVIEW.find((item) => item.id === reviewId) ?? DEMO_REVIEW[0];
    return { ...existing, reason: success ? "TEST_ONLY: отмечено как успешно" : "TEST_ONLY: повторить позже" };
  }
  return normalizeReview(await request<Record<string, unknown>>(`/review-queue/${encodeURIComponent(reviewId)}/complete`, {
    method: "POST",
    body: JSON.stringify({ success }),
  }));
}

export async function getDailyPlan(): Promise<DailyPlan> {
  if (DEMO_MODE) return DEMO_PLAN;
  return normalizePlan(await request<Record<string, unknown>>("/daily-plan"));
}

export async function rebuildDailyPlan(planDate?: string): Promise<DailyPlan> {
  if (DEMO_MODE) return DEMO_PLAN;
  return normalizePlan(await request<Record<string, unknown>>("/daily-plan/rebuild", {
    method: "POST",
    body: JSON.stringify(planDate ? { plan_date: planDate } : {}),
  }));
}

export async function getExamBlueprints(): Promise<ExamBlueprint[]> {
  if (DEMO_MODE) return DEMO_EXAMS;
  return unwrapItems(await request<ItemsEnvelope<BackendExamBlueprint>>("/exam-blueprints")).map(normalizeBlueprint);
}

export async function instantiateOfficialExamTemplate(): Promise<{ blueprint: ExamBlueprint; duplicate: boolean }> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Официальный шаблон недоступен в TEST_ONLY preview.", 503, {});
  const raw = await request<BackendTemplateInstantiation>("/exam-blueprint-templates/2027/instantiate", {
    method: "POST",
    body: "{}",
  });
  if (!raw.blueprint || typeof raw.blueprint !== "object") {
    throw new ApiError("INVALID_RESPONSE", "Backend не вернул созданный шаблон экзамена.", 200, {});
  }
  return { blueprint: normalizeBlueprint(raw.blueprint), duplicate: raw.duplicate === true };
}

export async function getActiveExam(): Promise<ExamBlueprint | null> {
  if (DEMO_MODE) return null;
  const payload = await request<{ active?: BackendExam | null }>("/exams/active");
  if (!payload || !Object.prototype.hasOwnProperty.call(payload, "active")) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул active exam без поля active.", 200, {});
  }
  if (payload.active === null) return null;
  if (!payload.active || typeof payload.active !== "object") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректную active exam сессию.", 200, {});
  }
  return normalizeExam(payload.active);
}

export async function startExam(blueprintId: string): Promise<ExamBlueprint> {
  if (DEMO_MODE) {
    const exam = DEMO_EXAMS.find((item) => item.id === blueprintId) ?? DEMO_EXAMS[0];
    return { ...exam, status: "active", exam_session_id: `demo-exam-session-${exam.seed}` };
  }
  return normalizeExam(await request<BackendExam>("/exams", { method: "POST", body: JSON.stringify({ blueprint_id: blueprintId, seed: deterministicSeed(blueprintId) }) }));
}

export async function getExam(examSessionId: string): Promise<ExamBlueprint> {
  if (DEMO_MODE) return DEMO_EXAMS.find((item) => item.exam_session_id === examSessionId) ?? DEMO_EXAMS[0];
  return normalizeExam(await request<BackendExam>(`/exams/${encodeURIComponent(examSessionId)}`));
}

export async function getSavedExams(): Promise<ExamBlueprint[]> {
  if (DEMO_MODE) return [];
  return unwrapItems(await request<ItemsEnvelope<BackendExam>>("/exams")).map(normalizeExam);
}

export async function startDifficultyExam(difficulty: ExamDifficulty): Promise<ExamBlueprint> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Варианты доступны в основной рабочей области.", 503, {});
  const seed = crypto.getRandomValues(new Uint32Array(1))[0];
  return normalizeExam(await request<BackendExam>("/variants", { method: "POST", body: JSON.stringify({ difficulty, seed }) }));
}

export async function pauseExam(examSessionId: string): Promise<ExamBlueprint> {
  return normalizeExam(await request<BackendExam>(`/exams/${encodeURIComponent(examSessionId)}/pause`, { method: "POST", body: "{}" }));
}

export async function resumeExam(examSessionId: string): Promise<ExamBlueprint> {
  return normalizeExam(await request<BackendExam>(`/exams/${encodeURIComponent(examSessionId)}/resume`, { method: "POST", body: "{}" }));
}

export async function saveExamAnswer(examSessionId: string, itemId: string, content: string): Promise<void> {
  if (DEMO_MODE) return;
  await request(`/exams/${encodeURIComponent(examSessionId)}/answers/${encodeURIComponent(itemId)}`, { method: "PUT", body: JSON.stringify({ content }) });
}

export async function submitExam(examSessionId: string): Promise<ExamReport> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Отправка недоступна в demo preview.", 503, {});
  return request<ExamReport>(`/exams/${encodeURIComponent(examSessionId)}/submit`, { method: "POST", body: JSON.stringify({}) });
}

export async function getExamReport(examSessionId: string): Promise<ExamReport> {
  if (DEMO_MODE) throw new ApiError("DEMO_ONLY", "Отчёт недоступен в demo preview.", 503, {});
  return request<ExamReport>(`/exams/${encodeURIComponent(examSessionId)}/report`);
}

export async function getAnalytics(): Promise<AnalyticsOverview> {
  if (DEMO_MODE) return DEMO_ANALYTICS;
  return normalizeAnalytics(await request<Record<string, unknown>>("/analytics/overview"));
}

export async function rebuildAnalytics(): Promise<AnalyticsOverview> {
  if (DEMO_MODE) return DEMO_ANALYTICS;
  return normalizeAnalytics(await request<Record<string, unknown>>("/analytics/rebuild", { method: "POST", body: JSON.stringify({}) }));
}

function normalizeAnalytics(raw: Record<string, unknown>): AnalyticsOverview {
  const metrics = (raw.metrics && typeof raw.metrics === "object" ? raw.metrics : {}) as Record<string, unknown>;
  return {
    available: raw.available !== false,
    reason: typeof raw.reason === "string" ? raw.reason : null,
    algorithm_version: typeof raw.algorithm_version === "string" ? raw.algorithm_version : null,
    snapshot_hash: typeof raw.snapshot_hash === "string" ? raw.snapshot_hash : null,
    task_count: numberOrNull(metrics.task_count),
    solved_count: numberOrNull(metrics.solved_count),
    solved_attempt_count: numberOrNull(metrics.solved_attempt_count),
    attempt_count: numberOrNull(metrics.attempt_count),
    correct_attempt_count: numberOrNull(metrics.correct_attempt_count),
    correct_count: numberOrNull(metrics.correct_count),
    independent_success_count: numberOrNull(metrics.independent_success_count),
    independent_success_rate: numberOrNull(metrics.independent_success_rate),
    active_skill_count: numberOrNull(metrics.active_skill_count),
    skill_coverage_count: numberOrNull(metrics.skill_coverage_count),
    skill_coverage_rate: numberOrNull(metrics.skill_coverage_rate),
    mastered_skill_count: numberOrNull(metrics.mastered_skill_count),
    exam_count: numberOrNull(metrics.exam_count),
    independent_rate: metrics.independent_success_rate == null ? null : numberOrNull(Number(metrics.independent_success_rate) * 100),
    accuracy: metrics.accuracy == null ? null : numberOrNull(Number(metrics.accuracy) * 100),
    average_minutes: null,
    evidence_count: numberOrNull(metrics.event_count_before_snapshot),
    coverage: metrics.skill_coverage_rate == null ? null : numberOrNull(Number(metrics.skill_coverage_rate) * 100),
    weekly: [],
  };
}

function normalizeTask(raw: BackendTask | BackendRevision): TaskRecord {
  const candidate = raw as BackendTask & BackendRevision;
  const revision = candidate.revision ?? candidate;
  if (!revision.id) throw new ApiError("INVALID_RESPONSE", "Backend не вернул идентификатор редакции.", 200, {});
  const taskId = candidate.revision ? candidate.id : revision.task_id ?? candidate.id;
  if (!taskId) throw new ApiError("INVALID_RESPONSE", "Backend не вернул идентификатор задачи.", 200, {});
  const line = revision.line_number ?? candidate.line_number;
  if (typeof line !== "number" || !Number.isInteger(line) || line < 1 || line > 27) {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректную линию задания.", 200, { revision_id: revision.id, line });
  }
  const sourceName = candidate.source_name ?? revision.source_name;
  if (sourceName !== "kompege" || (candidate.source_name !== undefined && candidate.source_name !== "kompege") || revision.source_name !== "kompege") {
    throw new ApiError("SOURCE_PROVENANCE_INVALID", "Live-каталог принимает только задания с source_name=kompege.", 200, { task_id: taskId, revision_id: revision.id, source_name: sourceName ?? null });
  }
  const sourceUrl = revision.source_url;
  if (!isCanonicalKegeUrl(sourceUrl)) {
    throw new ApiError("SOURCE_PROVENANCE_INVALID", "Backend вернул задачу без канонического URL КЕГЭ.", 200, { task_id: taskId, revision_id: revision.id });
  }
  const contentHash = revision.content_hash;
  if (typeof contentHash !== "string" || !contentHash || contentHash.startsWith("demo:")) {
    throw new ApiError("SOURCE_PROVENANCE_INVALID", "Backend вернул задачу без валидного content hash.", 200, { task_id: taskId, revision_id: revision.id });
  }
  const examYear = normalizeExamYear(revision.exam_year, revision.id);
  const targetExamYear = normalizeExamYear(
    revision.target_exam_year ?? candidate.target_exam_year,
    revision.id,
    "target_exam_year",
  );
  const sourceYear = normalizeExamYear(
    revision.source_year ?? candidate.source_year,
    revision.id,
    "source_year",
  );
  const rawDifficultyValue = revision.source_difficulty !== undefined
    ? revision.source_difficulty
    : candidate.source_difficulty;
  const sourceDifficulty = normalizeSourceDifficulty(rawDifficultyValue);
  const normalizedDifficultyValue = revision.difficulty !== undefined
    ? revision.difficulty
    : candidate.difficulty;
  const difficulty = normalizeDifficulty(normalizedDifficultyValue ?? sourceDifficulty);
  const sourceYearBasis = optionalString(revision.source_year_basis);
  const sourceCreatedAt = optionalString(revision.source_created_at);
  const sourceLabel = optionalString(revision.source_label ?? candidate.source_label);
  const sourceMetadata = normalizeSourceMetadata(revision.source_metadata ?? candidate.source_metadata);
  let externalId: string | null;
  try { externalId = resolveSourceNumber(candidate.external_id, revision.source_id); }
  catch (reason) { throw new ApiError("SOURCE_PROVENANCE_INVALID", reason instanceof Error ? reason.message : "Номер источника не подтверждён.", 200, {task_id:taskId,revision_id:revision.id}); }
  return {
    id: taskId,
    line_number: line,
    exam_year: examYear,
    source_year: sourceYear,
    target_exam_year: targetExamYear,
    external_id: externalId,
    title: externalId ? sourceNumberLabel(externalId) : `Задание типа ${line}`,
    difficulty,
    source_difficulty: sourceDifficulty,
    source_url: sourceUrl,
    revision: {
      id: revision.id,
      content_hash: contentHash,
      source_id: externalId,
      source_url: sourceUrl,
      source_name: "kompege",
      retrieved_at: revision.retrieved_at ?? "",
      revision_label: `revision ${revision.revision_number ?? "?"}`,
      statement_html: typeof revision.statement === "string" ? plainStatementMarkup(revision.statement) : revision.statement_html ?? "",
      statement_text: typeof revision.statement === "string" ? revision.statement : undefined,
      exam_year: examYear,
      source_year: sourceYear,
      source_year_basis: sourceYearBasis,
      source_created_at: sourceCreatedAt,
      source_difficulty: sourceDifficulty,
      difficulty,
      source_label: sourceLabel,
      source_metadata: sourceMetadata,
      target_exam_year: targetExamYear,
      rights_status: revision.rights_status ?? null,
    },
    assets: (revision.assets ?? []).map((asset) => normalizeAsset(asset)),
    skills: [],
    demo: false,
  };
}

function normalizeSourceDifficulty(value: unknown): string | number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.length <= 128) return value;
  return null;
}

function normalizeDifficulty(value: unknown): TaskDifficulty | null {
  if (isTaskDifficulty(value)) return value;
  if (typeof value === "number" && Number.isInteger(value)) {
    return value === 0 ? "easy" : value === 1 ? "medium" : value === 2 ? "hard" : null;
  }
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (isTaskDifficulty(normalized)) return normalized;
  if (normalized === "0") return "easy";
  if (normalized === "1") return "medium";
  if (normalized === "2") return "hard";
  return null;
}

function normalizeSourceMetadata(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

export function isCanonicalKegeUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    const path = url.pathname;
    const allowedPath = path === "/task"
      || path.startsWith("/task/")
      || path === "/variant"
      || path.startsWith("/variant/")
      || path === "/api/v1/task"
      || path.startsWith("/api/v1/task/")
      || path === "/api/v1/variant/random";
    return url.protocol === "https:" && (host === "kompege.ru" || host === "www.kompege.ru") && !url.username && !url.password && !url.port && allowedPath;
  } catch {
    return false;
  }
}

function normalizeExamYear(value: unknown, revisionId: string, field = "exam_year"): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  if (typeof value === "string" && /^\d{1,4}$/.test(value)) return Number(value);
  throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректный год ЕГЭ.", 200, { revision_id: revisionId, [field]: value });
}

function normalizeSourceHealth(raw: BackendSourceHealth): SourceHealth {
  const coverage = Object.fromEntries(Object.entries(raw.coverage ?? {}).filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1])));
  const sync = raw.last_sync;
  return {
    source: raw.source ?? "kompege",
    origin: raw.origin ?? "https://kompege.ru",
    mode: raw.mode ?? null,
    permission_status: raw.permission_status ?? null,
    policy_version: raw.policy_version ?? null,
    coverage,
    last_sync: sync ? { id: sync.id ?? "unknown", status: sync.status ?? "unknown", counters: sync.counters ?? {}, failures: sync.failures ?? [] } : null,
  };
}

function normalizeProject(raw: BackendProject): Project {
  return {
    id: raw.id,
    name: raw.name,
    archived: raw.archived ?? false,
    pinned: raw.pinned ?? false,
    updated_at: raw.updated_at ?? raw.created_at ?? "",
  };
}

function normalizeConversation(raw: BackendConversation): ConversationSummary {
  return {
    id: raw.id,
    title: raw.title ?? "Без названия",
    archived: raw.archived ?? false,
    pinned: raw.pinned ?? false,
    project_id: raw.project_id ?? null,
    updated_at: raw.last_message_at ?? raw.created_at ?? "",
    task_line: null,
  };
}

function normalizeAsset(asset: BackendAsset, fallbackId?: string): AssetRef {
  if (!asset || typeof asset !== "object") {
    throw new ApiError("INVALID_RESPONSE", "Backend вернул некорректное состояние файла.", 200, {});
  }
  const id = typeof asset?.id === "string" && asset.id ? asset.id : fallbackId;
  if (!id) throw new ApiError("INVALID_RESPONSE", "Backend вернул файл без идентификатора.", 200, {});
  const filename = asset.filename ?? asset.name ?? "Вложение";
  const rawState = asset.cache_state ?? asset.state ?? asset.status;
  const cacheState = normalizeAssetCacheState(rawState);
  const available = cacheState === "ready";
  const progressValue = asset.progress_percent ?? asset.progress;
  const progressPercent = typeof progressValue === "number" && Number.isFinite(progressValue) && progressValue >= 0 && progressValue <= 100
    ? progressValue
    : null;
  const reason = available
    ? null
    : asset.reason ?? (asset.error_code ? `${asset.error_code}: файл недоступен` : rawState ? `Кеш: ${rawState}` : "Кеш: состояние не указано");
  return {
    id,
    name: filename,
    media_type: asset.detected_type ?? asset.media_type ?? "application/octet-stream",
    size_bytes: asset.size_bytes ?? null,
    download_url: `${API_BASE}/assets/${encodeURIComponent(id)}/download`,
    available,
    cache_state: cacheState,
    content_hash: asset.content_hash ?? null,
    rights_status: asset.rights_status ?? null,
    canonical_url: asset.canonical_url ?? null,
    progress_percent: progressPercent,
    reason,
  };
}

function normalizeAssetCacheState(value: unknown): AssetRef["cache_state"] {
  if (value === "metadata_only" || value === "fetching" || value === "ready" || value === "failed" || value === "quarantined") return value;
  return "unknown";
}

function normalizeMessage(raw: Record<string, unknown>): Message {
  return { id: String(raw.id ?? "message"), role: raw.role === "user" ? "user" : "assistant", content: String(raw.content ?? ""), created_at: String(raw.created_at ?? "") };
}

function normalizeCode(raw: BackendCode, sessionId: string): CodeDocument {
  return { id: raw.document_id ?? `code-${sessionId}`, language: "python", content: raw.content ?? "", version: raw.version ?? 0, updated_at: raw.updated_at ?? new Date(0).toISOString() };
}

function normalizeRun(raw: Record<string, unknown>): CodeRun {
  const status = raw.status === "queued" || raw.status === "running" || raw.status === "succeeded" || raw.status === "completed" || raw.status === "failed" || raw.status === "timed_out" || raw.status === "unavailable" ? raw.status : "unavailable";
  return { id: String(raw.id ?? `run-${Date.now()}`), code_version:typeof raw.code_version==="number"&&Number.isInteger(raw.code_version)?raw.code_version:null, status, stdout: String(raw.stdout ?? ""), stderr: String(raw.stderr ?? ""), duration_ms: numberOrNull(raw.duration_ms), exit_code: typeof raw.exit_code === "number" ? raw.exit_code : null, created_at: String(raw.created_at ?? new Date().toISOString()), reason: status === "unavailable" ? String(raw.stderr ?? raw.reason ?? "Runner недоступен") : null };
}

function normalizeBoard(raw: BackendBoard, sessionId: string): BoardDocument {
  const snapshot = raw.snapshot && typeof raw.snapshot === "object" ? raw.snapshot as Record<string, unknown> : {};
  return { id: raw.document_id ?? `board-${sessionId}`, version: raw.version ?? 0, images:Array.isArray(snapshot.images)?snapshot.images as BoardDocument["images"]:[], strokes: Array.isArray(snapshot.strokes) ? snapshot.strokes as BoardDocument["strokes"] : [], texts: Array.isArray(snapshot.texts) ? snapshot.texts as BoardDocument["texts"] : [], viewport: snapshot.viewport && typeof snapshot.viewport === "object" ? snapshot.viewport as BoardDocument["viewport"] : { x: 0, y: 0, zoom: 1 }, updated_at: raw.updated_at ?? new Date(0).toISOString() };
}

function toBoardSnapshot(board: BoardDocument): Record<string, unknown> {
  return { strokes: board.strokes, texts: board.texts, viewport: board.viewport,images:board.images??[] };
}

function normalizeCapabilities(raw: Record<string, unknown>): Capabilities {
  const auth = normalizeCapability(raw.auth, "Owner session не настроена");
  const rawAuth = raw.auth && typeof raw.auth === "object" ? raw.auth as Record<string, unknown> : {};
  const sourceSync = normalizeCapability(raw.source_sync, "Источник задач недоступен");
  const sourceVariantSync = raw.source_variant_sync === undefined
    ? sourceSync
    : normalizeCapability(raw.source_variant_sync, "Синхронизация варианта недоступна");
  return {
    auth: { ...auth, mode: typeof rawAuth.mode === "string" ? rawAuth.mode : undefined },
    source_sync: sourceSync,
    source_variant_sync: sourceVariantSync,
    asset_fetch: normalizeCapability(raw.asset_fetch, "Файлы недоступны"),
    runner: normalizeCapability(raw.runner, "Runner недоступен"),
    codex: normalizeCapability(raw.codex, "Codex gateway недоступен"),
    demo_mode: false,
  };
}

function normalizeCapability(value: unknown, missingReason: string) {
  if (!value || typeof value !== "object") return { available: false, reason: missingReason };
  const status = value as Record<string, unknown>;
  return {
    available: status.available === true,
    reason: typeof status.reason === "string" ? status.reason : status.available === true ? null : missingReason,
    summary: typeof status.mode === "string" ? status.mode : null,
  };
}

function normalizeAnnotation(raw: BackendAnnotation): Annotation {
  const payload = raw.payload ?? {};
  const anchor = raw.anchor ?? {};
  const rawInk = anchor.ink ?? payload.ink;
  const ink = normalizeInkAnchor(rawInk);
  return { id: raw.id, kind: raw.kind === "ink" ? "ink" : "highlight", text: typeof payload.text === "string" ? payload.text : typeof anchor.quote === "string" ? anchor.quote : undefined, color: typeof payload.color === "string" ? payload.color : "#fef08a", points: Array.isArray(payload.points) ? payload.points as Annotation["points"] : undefined, anchor: { start: typeof anchor.start === "number" ? anchor.start : 0, end: typeof anchor.end === "number" ? anchor.end : 0, content_hash: raw.source_revision_hash ?? String(anchor.source_revision_hash ?? ""), ...(ink ? { ink } : {}) } };
}

function normalizeInkAnchor(value: unknown): NonNullable<NonNullable<Annotation["anchor"]>["ink"]> | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  const frame = raw.frame && typeof raw.frame === "object" ? raw.frame as Record<string, unknown> : null;
  const origin = raw.origin && typeof raw.origin === "object" ? raw.origin as Record<string, unknown> : null;
  const width = frame && typeof frame.width === "number" && Number.isFinite(frame.width) && frame.width > 0 ? frame.width : null;
  const height = frame && typeof frame.height === "number" && Number.isFinite(frame.height) && frame.height > 0 ? frame.height : null;
  const x = origin && typeof origin.x === "number" && Number.isFinite(origin.x) ? origin.x : null;
  const y = origin && typeof origin.y === "number" && Number.isFinite(origin.y) ? origin.y : null;
  if (width === null || height === null || x === null || y === null) return undefined;
  const char = raw.char && typeof raw.char === "object" ? raw.char as Record<string, unknown> : null;
  const offset = char && typeof char.offset === "number" && Number.isInteger(char.offset) && char.offset >= 0 ? char.offset : null;
  const style = raw.style && typeof raw.style === "object" ? raw.style as Record<string, unknown> : null;
  const styleWidth = style && typeof style.width === "number" && Number.isFinite(style.width) && style.width > 0 ? style.width : null;
  const styleOpacity = style && typeof style.opacity === "number" && Number.isFinite(style.opacity) && style.opacity >= 0 && style.opacity <= 1 ? style.opacity : null;
  return {
    frame: { width, height },
    origin: { x, y },
    ...(offset === null ? {} : { char: { offset } }),
    ...(styleWidth === null || styleOpacity === null ? {} : { style: { width: styleWidth, opacity: styleOpacity } }),
  };
}

function normalizeAssistantRun(raw: BackendAssistantRun, requested: ModelSelection) {
  const status = raw.status ?? "unknown";
  return { id: raw.id, content: raw.content ?? (raw.error_code ? "Помощник не смог завершить ответ." : status === "succeeded" || status === "completed" ? "Помощник вернул пустой ответ." : ""), requested, effective: raw.effective_model ? fromApiModel(raw.effective_model, raw.effective_effort) : null, ...(raw.provider_info ? { provider_label: raw.provider_info.model } : {}), status, error_code: raw.error_code ?? null, input_digest: typeof raw.input_digest === "string" ? raw.input_digest : null };
}

function fromApiModel(model: string, effort: string | null | undefined): ModelSelection {
  const allowedEffort = effort === "low" || effort === "medium" || effort === "high" || effort === "xhigh";
  if (model === "gpt-5.6-luna" && allowedEffort) {
    return { model: "luna", effort };
  }
  if (model === "gpt-5.6-terra" && allowedEffort) {
    return { model: "terra", effort };
  }
  if (model === "gpt-5.6-sol" && allowedEffort) {
    return { model: "sol", effort };
  }
  throw new ApiError(
    "MODEL_ROUTE_INVALID",
    "Выбранная модель или мощность ответа недоступна.",
    502,
    { model, effort: effort ?? null },
  );
}

function normalizeContextPreview(raw: Record<string, unknown>): AssistantContextPreview {
  const route = raw.route && typeof raw.route === "object" ? raw.route as Record<string, unknown> : {};
  const sections = Array.isArray(raw.sections) ? raw.sections.map((item) => {
    const section = item && typeof item === "object" ? item as Record<string, unknown> : {};
    const content = typeof section.content === "string" ? section.content : "";
    const trustLabel = typeof section.trust_label === "string" ? section.trust_label : undefined;
    const characterCount = numberOrNull(section.character_count) ?? numberOrNull(section.chars) ?? (content ? content.length : 0);
    return { name: String(section.name ?? section.title ?? trustLabel ?? "Раздел"), trust_label: trustLabel, chars: characterCount, character_count: characterCount, included: section.included !== false };
  }) : undefined;
  const counts = raw.redaction_counts && typeof raw.redaction_counts === "object" ? raw.redaction_counts as Record<string, unknown> : {};
  const redactionCounts = Object.fromEntries(Object.entries(counts).filter((entry): entry is [string, number] => typeof entry[1] === "number")) as Record<string, number>;
  const manifest = raw.manifest && typeof raw.manifest === "object" ? raw.manifest as Record<string, unknown> : {};
  const manifestSections = Array.isArray(manifest.sections) ? manifest.sections : [];
  const characterCount = numberOrNull(raw.character_count) ?? numberOrNull(manifest.character_count) ?? (sections ?? manifestSections).reduce((sum, section) => {
    if (!section || typeof section !== "object") return sum;
    const value = section as Record<string, unknown>;
    return sum + (numberOrNull(value.character_count) ?? numberOrNull(value.chars) ?? 0);
  }, 0);
  return {
    content: typeof raw.content === "string" ? raw.content : undefined,
    trust_label: typeof raw.trust_label === "string" ? raw.trust_label : typeof manifest.trust_label === "string" ? manifest.trust_label : undefined,
    character_count: characterCount,
    sections,
    route: { reason: typeof route.reason === "string" ? route.reason : undefined, model: typeof route.resolved_model === "string" ? route.resolved_model : undefined, effort: typeof route.resolved_effort === "string" ? route.resolved_effort : undefined },
    redaction_count: typeof redactionCounts.total === "number" ? redactionCounts.total : Object.values(redactionCounts).reduce((sum, value) => sum + value, 0),
    redaction_counts: redactionCounts,
  };
}

function normalizeSample(raw: Record<string, unknown>): CodeSample {
  return { id: String(raw.id ?? "sample"), title: String(raw.title ?? "Без названия"), language: String(raw.language ?? "python"), content: String(raw.content ?? ""), task_lines: arrayOfNumbers(raw.task_lines), tags: arrayOfStrings(raw.tags), usage_note: String(raw.usage_note ?? ""), enabled: raw.enabled !== false, version: Number(raw.version ?? 0), content_hash: typeof raw.content_hash === "string" ? raw.content_hash : null };
}

function normalizeError(raw: Record<string, unknown>): ErrorEvent {
  const rawStatus = raw.status;
  const status: ErrorEvent["status"] = rawStatus === "confirmed" || rawStatus === "rejected" ? rawStatus : "proposed";
  return { id: String(raw.id ?? "error"), task_line: numberOrNull(raw.task_line), title: String(raw.error_type ?? "Ошибка"), type: String(raw.error_type ?? ""), note: String(raw.prevention_note || raw.reason || raw.description || ""), resolved: false, next_review: null, status, source: typeof raw.source === "string" ? raw.source : null, attempt_id: typeof raw.attempt_id === "string" ? raw.attempt_id : null, task_revision_id: typeof raw.task_revision_id === "string" ? raw.task_revision_id : null, solve_session_id: typeof raw.solve_session_id === "string" ? raw.solve_session_id : null, user_note: typeof raw.prevention_note === "string" ? raw.prevention_note : "", created_at: typeof raw.created_at === "string" ? raw.created_at : null, updated_at: typeof raw.updated_at === "string" ? raw.updated_at : null };
}

function normalizeMastery(raw: Record<string, unknown>): MasteryState {
  const status = String(raw.status ?? "insufficient_evidence");
  const evidence = Number(raw.evidence_count ?? 0);
  const successes = Number(raw.independent_successes ?? 0);
  const hintDependency = numberOrNull(raw.hint_dependency);
  const reasonCodes = Array.isArray(raw.reason_codes) ? raw.reason_codes.filter((item): item is string => typeof item === "string") : [];
  const reason = typeof raw.reason === "string" ? raw.reason : reasonCodes.length ? reasonCodes.join(" · ") : "Состояние по evidence";
  const normalizedStatus: MasteryState["status"] = status === "mastered" ? "stable" : status === "developing" ? "learning" : status === "insufficient_evidence" ? "insufficient_evidence" : "needs_attention";
  return { id: String(raw.skill_id ?? raw.id ?? "skill"), name: String(raw.title ?? raw.name ?? "Навык"), status: normalizedStatus, evidence_count: evidence, independent_successes: successes, independent_success_rate: evidence > 0 ? Math.round((successes / evidence) * 100) : null, reason: hintDependency == null ? reason : `${reason} · зависимость от подсказок ${Math.round(hintDependency * 100)}%` };
}

function normalizeReview(raw: Record<string, unknown>): ReviewItem {
  const due = raw.due_at ? new Date(String(raw.due_at)).toLocaleDateString("ru-RU") : "По расписанию";
  const reasonCodes = Array.isArray(raw.reason_codes) ? raw.reason_codes.filter((item): item is string => typeof item === "string") : [];
  return { id: String(raw.id ?? "review"), label: reasonCodes.length ? reasonCodes.join(" · ") : "Очередь повторения", task_line: numberOrNull(raw.task_line), due_label: due, reason: typeof raw.reason === "string" ? raw.reason : reasonCodes.length ? `Причины: ${reasonCodes.join(" · ")}` : "Очередь построена backend", skill_id: typeof raw.skill_id === "string" ? raw.skill_id : null, error_pattern_id: typeof raw.error_pattern_id === "string" ? raw.error_pattern_id : null, reason_codes: reasonCodes, evidence_ids: Array.isArray(raw.evidence_ids) ? raw.evidence_ids.filter((item): item is string => typeof item === "string") : [], status: raw.status === "scheduled" || raw.status === "completed" ? raw.status : "due", completed_at: typeof raw.completed_at === "string" ? raw.completed_at : null };
}

function normalizePlan(raw: Record<string, unknown>): DailyPlan {
  const items = Array.isArray(raw.items) ? raw.items.map((item, index) => {
    const value = item && typeof item === "object" ? item as Record<string, unknown> : {};
    const reasons = Array.isArray(value.reason_codes) ? value.reason_codes.filter((reason): reason is string => typeof reason === "string") : [];
    const evidence = Array.isArray(value.evidence_ids) ? value.evidence_ids.filter((id): id is string => typeof id === "string") : [];
    return {
      id: String(value.review_item_id ?? value.id ?? `plan-${index}`),
      label: String(value.label ?? value.title ?? (reasons.length ? reasons.join(" · ") : "Повторение")),
      detail: String(value.detail ?? (evidence.length ? `Доказательств: ${evidence.length}` : "Пункт очереди повторения")),
      done: Boolean(value.done ?? value.completed),
    };
  }) : [];
  return { date: String(raw.plan_date ?? ""), title: String(raw.reason ?? "План на сегодня"), items };
}

function normalizeBlueprint(raw: BackendExamBlueprint): ExamBlueprint {
  const required = raw.required_lines ?? {};
  const constraints = raw.constraints ?? {};
  const examConstraints = constraints.exam;
  const reference = constraints.reference;
  const duration = examConstraints && typeof examConstraints === "object"
    ? numberOrNull((examConstraints as Record<string, unknown>).duration_minutes)
    : null;
  const templateId = reference && typeof reference === "object"
    ? typeof (reference as Record<string, unknown>).template_id === "string" ? (reference as Record<string, unknown>).template_id as string : null
    : null;
  return { id: raw.id, name: raw.name, year: raw.exam_year ?? undefined, blueprint_version: raw.version != null ? String(raw.version) : undefined, template_id: templateId, revision_count: Object.values(required).reduce((sum, value) => sum + Number(value), 0), duration_minutes: duration, status: "ready" };
}

function normalizeExam(raw: BackendExam): ExamBlueprint {
  const lifecycle = raw.lifecycle ?? (raw.sealed ? "active" : "ready");
  return { id: raw.blueprint_id ?? raw.id, blueprint_id: raw.blueprint_id, exam_session_id: raw.id, name: raw.name ?? "Тренировочный вариант", seed: raw.seed ?? undefined, revision_ids: raw.ordered_revision_ids ?? [], revision_count: raw.ordered_revision_ids?.length, scoring_version: raw.scoring_version ?? undefined, duration_minutes: raw.duration_minutes ?? null, started_at: raw.started_at ?? null, deadline_at: raw.deadline_at ?? null, paused_at: raw.paused_at ?? null, remaining_seconds: raw.remaining_seconds ?? null, server_now: raw.server_now ?? null, difficulty: raw.difficulty ?? null, target_difficulty_count: raw.target_difficulty_count ?? null, rng_algorithm: raw.rng_algorithm, rng_version: raw.rng_version, pool_manifest_hash: raw.pool_manifest_hash, items: (raw.items ?? []).map(normalizeExamItem), answers: normalizeExamAnswers(raw.answers), status: lifecycle === "completed" || lifecycle === "submitted" ? "completed" : lifecycle === "paused" ? "paused" : lifecycle === "active" || lifecycle === "sealed" || raw.sealed ? "active" : "ready" };
}

function normalizeAttempt(raw: BackendAttempt): Attempt {
  const rawStatus = raw.status;
  const status: Attempt["status"] = rawStatus === "solved" || rawStatus === "failed" || rawStatus === "self_reported" ? rawStatus : "in_progress";
  return { id: raw.id, status, user_answer: raw.user_answer ?? null, verified_correct: typeof raw.verified_correct === "boolean" ? raw.verified_correct : null, verification_method: raw.verification_method ?? null, independent: raw.independent !== false, hint_count: typeof raw.hint_count === "number" && Number.isFinite(raw.hint_count) ? raw.hint_count : 0, error_type: raw.error_type ?? null, notes: raw.notes ?? null, started_at: raw.started_at ?? "", completed_at: raw.completed_at ?? null };
}

function normalizeExamItem(raw: Record<string, unknown>): ExamItem {
  const assets = Array.isArray(raw.assets) ? raw.assets.map((asset) => normalizeAsset(asset as BackendAsset)) : [];
  const difficulty = raw.difficulty === "easy" ? "basic" : raw.difficulty;
  return { id: String(raw.id ?? "exam-item"), position: Number(raw.position ?? 0), task_revision_id: String(raw.task_revision_id ?? ""), line_number: numberOrNull(raw.line_number), statement: String(raw.statement ?? ""), source_id: typeof raw.source_id === "string" ? raw.source_id : undefined, difficulty: difficulty === "basic" || difficulty === "medium" || difficulty === "hard" ? difficulty : null, assets };
}

function deterministicSeed(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function arrayOfNumbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

function arrayOfStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
