export interface FullSaveState { dirty: boolean; saving: boolean; conflict: boolean; ready: boolean }
export interface FullFlushResult { serverConfirmed: boolean; state: FullSaveState }

/** A durable frontend barrier, followed by the native full-backend checkpoint. */
export async function performLocalClose(nonce: string, dependencies: {
  mutations: () => Promise<void>;
  flush: () => Promise<FullFlushResult>;
  read: () => FullSaveState;
  confirm: (nonce: string) => Promise<void>;
}): Promise<void> {
  if (typeof nonce !== "string" || !nonce || nonce.length > 128) throw new Error("Некорректный запрос закрытия.");
  await dependencies.mutations();
  const result = await dependencies.flush();
  const state = dependencies.read();
  if (!result.serverConfirmed || !state.ready || state.dirty || state.saving || state.conflict) {
    throw new Error("Работа ещё не сохранена. Устраните ошибку сохранения и повторите закрытие.");
  }
  await dependencies.confirm(nonce);
}
