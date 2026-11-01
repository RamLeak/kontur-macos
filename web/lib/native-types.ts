// Camel-case DTOs from native/src/domain.rs and docs/WIRE.md.
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type Verdict = "correct" | "wrong" | "unverified" | "error";
export type ProgressStatus = "not_started" | "in_progress" | "error" | "solved";
export interface NativeError { code: string; message: string; conflictRevision?: number }
export interface Task {
  sourceId: string; revision: string; typeNumber: number; title: string;
  statement: string; blocks: JsonObject | JsonValue[] | null; assetIds: string[];
}
export interface Document {
  id: string; taskSourceId: string | null; taskRevision: string | null;
  title: string; typeNumber: number | null; code: string; statement: string;
  board: JsonObject; revision: number; updatedAt: number; isOwnWork: boolean;
}
export interface Attempt {
  id: string; taskSourceId: string; taskRevision: string | null; verdict: Verdict;
  source: "manual" | "imported"; createdAt: number; code: string; answer: string;
}
export interface ProgressSummary {
  taskSourceId: string; taskRevision: string; status: ProgressStatus; everSolved: boolean;
  lastVerdict: Verdict | null; lastAttemptId: string | null; attemptCount: number; updatedAt: number | null;
}
export interface AssetMetadata { id: string; name: string; mime: string; sizeBytes: number; sha256: string }
export interface AssetData { metadata: AssetMetadata; bytes: number[] }
export interface Snapshot {
  tasks: Task[]; documents: Document[]; attempts: Attempt[]; progress: ProgressSummary[];
  assets: AssetMetadata[]; preferences: { selectedDocumentId: string | null; values: JsonObject };
}
export interface CreateDocument { taskSourceId?: string; taskRevision?: string; title?: string; typeNumber?: number }
export interface SaveDocument {
  documentId: string; expectedRevision: number; title: string; typeNumber: number | null;
  code: string; statement: string; board: JsonObject;
}
export interface MarkAttempt { id?: string; taskSourceId: string; taskRevision: string; verdict: Verdict; code: string; answer: string }
export interface ImportPreview {
  formatVersion: number; contentSha256: string; taskCount: number; attemptCount: number;
  documentCount: number; assetCount: number; newTasks: number; newAttempts: number;
  newDocuments: number; newAssets: number; documentConflicts: number; duplicateRecords: number;
}
export interface ImportResult {
  contentSha256: string; importedTasks: number; importedAttempts: number; importedDocuments: number;
  importedAssets: number; documentConflicts: number; duplicateRecords: number; backupPath: string;
}
export interface ExportResult {
  path: string; contentSha256: string; taskCount: number; attemptCount: number; documentCount: number; assetCount: number;
}
export type ModelSelection = { mode: "cliDefault" } | { mode: "manual"; id: string };
export interface CodexRequest { question: string; code?: string; statement?: string; model: ModelSelection }
export interface CodexStatus {
  state: "checking" | "ready" | "unavailable"; cliVersion?: string;
  loginState: "unknown" | "inProgress" | "required" | "ready";
  manualModelSupported: boolean; manualModels: string[]; activeRequestId?: string; error?: NativeError;
}
export interface CodexPoll {
  requestId: string; state: "starting" | "running" | "completed" | "cancelled" | "failed";
  text: string; model: ModelSelection; error?: NativeError;
}
