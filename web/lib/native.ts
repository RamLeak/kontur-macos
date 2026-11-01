import type {
  AssetData, CodexPoll, CodexRequest, CodexStatus, CreateDocument, Document,
  ExportResult, ImportPreview, ImportResult, MarkAttempt, NativeError, ProgressSummary,
  SaveDocument, Snapshot,
} from "./native-types";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
declare global {
  interface Window {
    __TAURI__?: { core: { invoke: Invoke } };
    __KONTUR_LOCAL_TEST_ONLY__?: { invoke: Invoke };
    __KONTUR_LOCAL_REQUEST_CLOSE__?: (request: { nonce: string }) => Promise<void>;
  }
}

export class LocalError extends Error implements NativeError {
  constructor(public code: string, message: string, public conflictRevision?: number) { super(message); }
}

export function safeError(reason: unknown, fallback = "Не удалось выполнить действие. Повторите попытку."): LocalError {
  if (reason instanceof LocalError) return reason;
  if (reason && typeof reason === "object" && "code" in reason && "message" in reason
    && typeof reason.code === "string" && typeof reason.message === "string") {
    const revision = "conflictRevision" in reason && typeof reason.conflictRevision === "number" ? reason.conflictRevision : undefined;
    return new LocalError(reason.code, reason.message, revision);
  }
  return new LocalError("local_unavailable", fallback);
}

export async function invokeNative<T>(command: string, args?: Record<string, unknown>): Promise<T> {
  const invoke = typeof window !== "undefined" ? window.__TAURI__?.core?.invoke : undefined;
  // A QA build must opt in explicitly; production never manufactures browser data.
  const testInvoke = process.env.NEXT_PUBLIC_KONTUR_TEST_ONLY === "1" && typeof window !== "undefined"
    ? window.__KONTUR_LOCAL_TEST_ONLY__?.invoke : undefined;
  if (!invoke && !testInvoke) throw new LocalError("native_unavailable", "Локальное хранилище недоступно. Откройте «Контур Локальный» в установленном приложении.");
  try { return await (invoke ?? testInvoke)!<T>(command, args); }
  catch (reason) { throw safeError(reason); }
}

export const native = {
  snapshot: () => invokeNative<Snapshot>("load_snapshot"),
  create: (input: CreateDocument) => invokeNative<Document>("create_document", { input }),
  document: (documentId: string) => invokeNative<Document>("load_document", { documentId }),
  save: (input: SaveDocument) => invokeNative<Document>("save_document", { input }),
  mark: (input: MarkAttempt) => invokeNative<ProgressSummary>("mark_attempt", { input }),
  // Dialog/token and close envelope are finalized by the native command owner.
  previewImport: () => invokeNative<{ token: string; preview: ImportPreview } | null>("preview_import"),
  confirmImport: (token: string) => invokeNative<ImportResult>("confirm_import", { token }),
  export: () => invokeNative<ExportResult | null>("export_bundle"),
  asset: (assetId: string) => invokeNative<AssetData>("load_asset", { assetId }),
  saveAsset: (assetId: string) => invokeNative<{ fileName: string } | null>("save_asset", { assetId }),
  confirmClose: (nonce: string, savedDocuments: { documentId: string; revision: number }[]) => invokeNative<void>("confirm_close", { nonce, savedDocuments, allFlushed: true }),
  codexStatus: () => invokeNative<CodexStatus>("codex_status"),
  codexStart: (request: CodexRequest) => invokeNative<CodexPoll>("codex_start", { request }),
  codexPoll: (requestId: string) => invokeNative<CodexPoll>("codex_poll", { requestId }),
  codexCancel: (requestId: string) => invokeNative<CodexPoll>("codex_cancel", { requestId }),
  codexLogin: () => invokeNative<CodexStatus>("codex_login"),
};
