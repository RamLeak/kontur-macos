import type { Revisioned, SaveState } from "./save-queue";

export async function acknowledgeClose(nonce: string, dependencies: {
  waitForPending: () => Promise<unknown>;
  flush: () => Promise<unknown>;
  documents: () => SaveState<Revisioned>[];
  confirm: (nonce: string, savedDocuments: { documentId: string; revision: number }[]) => Promise<void>;
}): Promise<void> {
  if (typeof nonce !== "string" || !nonce) throw new Error("INVALID_CLOSE_CHALLENGE");
  await dependencies.waitForPending();
  await dependencies.flush();
  const documents = dependencies.documents();
  if (!documents.length) throw new Error("STORAGE_NOT_READY");
  const savedDocuments = documents.map(item => {
    if (item.dirty || item.saving || item.error || !item.value.id || !Number.isSafeInteger(item.value.revision) || item.value.revision < 1) throw new Error("UNFLUSHED_DOCUMENT");
    return { documentId: item.value.id, revision: item.value.revision };
  });
  if (new Set(savedDocuments.map(item => item.documentId)).size !== savedDocuments.length) throw new Error("DUPLICATE_DOCUMENT_ACK");
  await dependencies.confirm(nonce, savedDocuments);
}
