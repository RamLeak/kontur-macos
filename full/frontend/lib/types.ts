export type NavKey =
  | "today"
  | "bank"
  | "skills"
  | "errors"
  | "exams"
  | "analytics"
  | "bookmarks"
  | "settings";

export type ModelId = "auto" | "luna" | "terra" | "sol";
export type Effort = "low" | "medium" | "high" | "xhigh";
export const TARGET_EXAM_YEAR = 2027;
export type TaskDifficulty = "easy" | "medium" | "hard";
export type TaskCatalogSort = "number" | "newest" | "difficulty_asc" | "difficulty_desc";
export type TaskCatalogKind = "current" | "recent" | "all";
export type SourceDifficulty = string | number | null;
export type TutorMode =
  | "hint"
  | "hint_ladder"
  | "feynman"
  | "socratic"
  | "debug"
  | "review"
  | "compare"
  | "full_solution";

export interface ModelSelection {
  model: ModelId;
  effort?: Effort;
}

export interface ModelOption {
  model: Exclude<ModelId, "auto">;
  effort: Effort;
  label: string;
  description: string;
}

export interface CapabilityStatus {
  available: boolean;
  reason: string | null;
  summary?: string | null;
}

export interface Capabilities {
  auth?: CapabilityStatus & { mode?: string };
  source_sync: CapabilityStatus;
  source_variant_sync?: CapabilityStatus;
  asset_fetch: CapabilityStatus;
  runner: CapabilityStatus;
  codex: CapabilityStatus;
  demo_mode?: boolean;
}

export interface Project {
  id: string;
  name: string;
  archived?: boolean;
  pinned?: boolean;
  color?: string;
  conversation_count?: number;
  updated_at: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  archived?: boolean;
  pinned?: boolean;
  project_id: string | null;
  updated_at: string;
  task_line: number | null;
}

export type AssetCacheState =
  | "metadata_only"
  | "fetching"
  | "ready"
  | "failed"
  | "quarantined"
  | "unknown";

export interface AssetRef {
  id: string;
  name: string;
  media_type: string;
  size_bytes: number | null;
  download_url: string;
  available: boolean;
  cache_state: AssetCacheState;
  content_hash: string | null;
  rights_status: string | null;
  canonical_url: string | null;
  progress_percent: number | null;
  reason?: string | null;
}

export interface TaskRevision {
  id: string;
  /** Literal upstream KEGE identifier, never a filtered list position. */
  source_id?: string | null;
  content_hash: string;
  source_url: string;
  source_name: "kompege" | "demo";
  retrieved_at: string;
  revision_label: string;
  statement_html: string;
  statement_text?: string;
  /** Year observed in the upstream source record, if the source provides it. */
  exam_year?: number | null;
  /** Source year proven by the frozen catalog policy, if available. */
  source_year?: number | null;
  source_year_basis?: string | null;
  source_created_at?: string | null;
  /** Original source value; never displayed as a normalized learner level. */
  source_difficulty?: SourceDifficulty;
  difficulty?: TaskDifficulty | null;
  source_label?: string | null;
  source_metadata?: Record<string, unknown> | null;
  /** Owner-selected preparation label; this is not upstream provenance. */
  target_exam_year?: number | null;
  rights_status?: string | null;
}

export interface TaskRecord {
  id: string;
  /** Stable source identity; internal UUIDs and list positions are separate. */
  external_id?: string | null;
  line_number: number;
  /** Year observed in the upstream source record. */
  exam_year: number | null;
  /** Source year proven by the frozen catalog policy, if available. */
  source_year?: number | null;
  /** Owner-selected preparation target for catalog/exam planning. */
  target_exam_year: number | null;
  title: string;
  difficulty: TaskDifficulty | null;
  /** Original source value retained for provenance, when present. */
  source_difficulty?: SourceDifficulty;
  source_url: string;
  revision: TaskRevision;
  assets: AssetRef[];
  skills: string[];
  demo?: boolean;
}

export interface TaskCatalogQuery {
  learningStatus?: "neutral" | "started" | "incorrect" | "correct";
  bookmarksOnly?: boolean;
  /** Filter by the upstream observed year. */
  examYear?: number;
  /** Filter by the frozen source year, not the preparation target. */
  sourceYear?: number;
  difficulty?: TaskDifficulty;
  sort?: TaskCatalogSort;
  catalog?: TaskCatalogKind;
  /** Filter by the owner-selected preparation target year. */
  targetExamYear?: number;
  line?: number;
  query?: string;
  hasFiles?: boolean;
  limit?: number;
  cursor?: string | null;
}

export interface TaskCatalogPage {
  items: TaskRecord[];
  next_cursor: string | null;
  total: number | null;
}

export interface TaskCatalogCoverageCell {
  line_number: number;
  difficulty: TaskDifficulty;
  primary_available: number;
  fallback_available: number;
  fallback_allowed: boolean;
  selected_primary: number;
  selected_fallback: number;
  total: number;
  shortfall: number;
}

export interface TaskCatalogCoverage {
  published: boolean;
  policy_version: string | null;
  primary_years: number[];
  fallback_year: number | null;
  fallback_threshold: number | null;
  target_per_cell: number | null;
  total_selected: number;
  quota_met: boolean;
  updated_at: string | null;
  cells: TaskCatalogCoverageCell[];
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  model?: string | null;
}

export interface CodeDocument {
  id: string;
  language: "python";
  content: string;
  version: number;
  updated_at: string;
}

export interface CodeRun {
  id: string;
  code_version?:number|null;
  status: "queued" | "running" | "succeeded" | "completed" | "failed" | "timed_out" | "unavailable";
  stdout: string;
  stderr: string;
  duration_ms: number | null;
  exit_code: number | null;
  created_at: string;
  reason?: string | null;
}

export interface InkAnchorFrame {
  width: number;
  height: number;
}

export interface InkAnchor {
  frame: InkAnchorFrame;
  origin: { x: number; y: number };
  char?: { offset: number };
  style?: { width: number; opacity: number };
}

export interface AnnotationAnchor {
  start: number;
  end: number;
  content_hash: string;
  /** Optional per-stroke replay metadata; older annotations omit it. */
  ink?: InkAnchor;
}

export interface Annotation {
  id: string;
  kind: "highlight" | "ink";
  text?: string;
  color: string;
  anchor?: AnnotationAnchor;
  points?: Array<{ x: number; y: number }>;
}

export interface BoardStroke {
  id: string;
  kind?: "freehand" | "marker" | "line" | "rect" | "ellipse";
  points: Array<{ x: number; y: number }>;
  color: string;
  width: number;
  opacity?: number;
}

export interface BoardText {
  id: string;
  x: number;
  y: number;
  text: string;
}

export interface BoardImage {
  id: string;
  asset_id: string;
  revision_hash: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface BoardDocument {
  id: string;
  version: number;
  strokes: BoardStroke[];
  texts: BoardText[];
  images?: BoardImage[];
  viewport: { x: number; y: number; zoom: number };
  updated_at: string;
}

export interface SolveSession {
  id: string;
  conversation_id?: string | null;
  task: TaskRecord;
  messages: Message[];
  code: CodeDocument;
  runs: CodeRun[];
  attempts: Attempt[];
  board: BoardDocument;
  annotations: Annotation[];
  sealed_exam: boolean;
}

export type AttemptStatus = "in_progress" | "solved" | "failed" | "self_reported";

export interface Attempt {
  id: string;
  status: AttemptStatus;
  user_answer: string | null;
  verified_correct: boolean | null;
  verification_method: string | null;
  independent: boolean;
  hint_count: number;
  error_type: string | null;
  notes: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface CodeSample {
  id: string;
  title: string;
  language: string;
  content: string;
  task_lines: number[];
  tags: string[];
  usage_note: string;
  enabled: boolean;
  version: number;
  content_hash: string | null;
}

export interface ErrorEvent {
  id: string;
  task_line: number | null;
  title: string;
  type: string;
  note: string;
  resolved: boolean;
  next_review: string | null;
  status?: "proposed" | "confirmed" | "rejected";
  source?: string | null;
  attempt_id?: string | null;
  task_revision_id?: string | null;
  solve_session_id?: string | null;
  user_note?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MasteryState {
  id: string;
  name: string;
  status: "stable" | "learning" | "needs_attention" | "insufficient_evidence";
  evidence_count: number;
  independent_successes: number;
  independent_success_rate?: number | null;
  reason: string;
}

export interface ReviewItem {
  id: string;
  label: string;
  task_line: number | null;
  due_label: string;
  reason: string;
  skill_id?: string | null;
  error_pattern_id?: string | null;
  reason_codes?: string[];
  evidence_ids?: string[];
  status?: "due" | "scheduled" | "completed";
  completed_at?: string | null;
}

export interface DailyPlan {
  date: string;
  title: string;
  items: Array<{ id: string; label: string; detail: string; done: boolean }>;
}

export type ExamDifficulty = "basic" | "medium" | "hard";

export interface ExamBlueprint {
  id: string;
  blueprint_id?: string;
  exam_session_id?: string;
  name: string;
  year?: number;
  seed?: string | number;
  blueprint_version?: string;
  template_id?: string | null;
  revision_ids?: string[];
  revision_count?: number;
  scoring_version?: string;
  duration_minutes?: number | null;
  started_at?: string | null;
  deadline_at?: string | null;
  paused_at?: string | null;
  remaining_seconds?: number | null;
  server_now?: string | null;
  difficulty?: ExamDifficulty | null;
  target_difficulty_count?: number | null;
  rng_algorithm?: string;
  rng_version?: string;
  pool_manifest_hash?: string;
  items?: ExamItem[];
  answers?: Record<string, string>;
  status: "ready" | "active" | "paused" | "completed";
}

export interface ExamItem {
  id: string;
  position: number;
  task_revision_id: string;
  line_number: number | null;
  statement: string;
  difficulty?: ExamDifficulty | null;
  source_id?: string;
  assets?: AssetRef[];
}

export interface ExamReport {
  id: string;
  exam_session_id: string;
  scoring_version: string;
  report: Record<string, unknown>;
  report_hash: string;
}

export interface AnalyticsOverview {
  available?: boolean;
  reason?: string | null;
  algorithm_version?: string | null;
  snapshot_hash?: string | null;
  task_count?: number | null;
  solved_count: number | null;
  solved_attempt_count?: number | null;
  attempt_count?: number | null;
  correct_attempt_count?: number | null;
  correct_count?: number | null;
  independent_success_count?: number | null;
  independent_success_rate?: number | null;
  active_skill_count?: number | null;
  skill_coverage_count?: number | null;
  skill_coverage_rate?: number | null;
  mastered_skill_count?: number | null;
  exam_count?: number | null;
  independent_rate: number | null;
  accuracy?: number | null;
  average_minutes: number | null;
  evidence_count: number | null;
  coverage: number | null;
  weekly: Array<{ label: string; value: number }>;
}

export interface SourceHealth {
  source: string;
  origin: string;
  mode: string | null;
  permission_status: string | null;
  policy_version: string | null;
  coverage: Record<string, number>;
  last_sync: {
    id: string;
    status: string;
    counters: Record<string, unknown>;
    failures: unknown[];
  } | null;
}

export interface SourceVariantSyncResult {
  duplicate: boolean;
  source_url: string | null;
  manifest_hash: string | null;
  target_exam_year: number | null;
  count: number | null;
  failures: unknown[];
  tasks: TaskRecord[];
}

export interface AssistantContextPreview {
  content?: string;
  trust_label?: string;
  character_count?: number;
  sections?: Array<{ name: string; trust_label?: string; chars?: number; character_count?: number; included?: boolean }>;
  route?: { reason?: string; model?: string; effort?: string };
  redaction_count?: number;
  redaction_counts?: Record<string, number>;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(
    code: string,
    message: string,
    status = 500,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}
