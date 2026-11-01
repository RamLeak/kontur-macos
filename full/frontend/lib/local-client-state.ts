"use client";
import { request } from "./api";
import { isLocalDesktop } from "./local-fetch";
import { markDraftStateChanged, registerDraftFlush, registerDraftSaveState } from "./draft-lifecycle";
import { LocalClientStateStore, type ClientStateSnapshot } from "./local-client-state-store";
import { CLIENT_STATE_OUTBOX } from "./local-client-state-schema";
import { applyClientStorage, readClientStorage, observeClientStorage } from "./local-client-storage";

let store: LocalClientStateStore | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let suppress = 0;
let installed = false;
const failedMutations = new Map<string, string | null>();
const initial: ClientStateSnapshot = { ready: false, dirty: true, saving: false, conflict: false, localRecoverable: false, error: null, version: 0 };
function localStore() {
  if (store) return store;
  if (typeof window === "undefined" || !isLocalDesktop()) throw new Error("Локальное хранилище доступно в приложении Контур.");
  const storage = window.localStorage;
  store = new LocalClientStateStore({
    readLocal: () => readClientStorage(storage),
    applyLocal: value => { suppress++; try { applyClientStorage(storage, value); } finally { suppress--; } },
    readOutbox: () => { const raw = storage.getItem(CLIENT_STATE_OUTBOX); return raw === null ? null : JSON.parse(raw); },
    writeOutbox: value => { if (value === null) storage.removeItem(CLIENT_STATE_OUTBOX); else storage.setItem(CLIENT_STATE_OUTBOX, JSON.stringify(value)); },
    readRemote: () => request("/local/client-state"),
    writeRemote: (expectedVersion, value) => request("/local/client-state", { method: "PUT", body: JSON.stringify({ expectedVersion, value }) }),
  });
  store.subscribe(() => {
    markDraftStateChanged();
    const state = store!.snapshot();
    if (state.ready && state.dirty && !state.saving && !state.conflict && !state.error && timer === null) {
      timer = setTimeout(() => { timer = null; void flushLocalClientState().catch(() => undefined); }, 800);
    }
  });
  return store;
}
function installStorageObserver() {
  if (installed) return;
  const current = localStore();
  observeClientStorage(window.localStorage, {
    before: () => current.prepareLocalMutation(),
    after: key => { failedMutations.delete(key); current.markLocalMutation(); },
    failed: (reason, key, value) => { failedMutations.set(key, value); current.failSource(reason); },
  }, () => suppress > 0);
  installed = true;
}
export async function initializeLocalClientState() {
  const current = localStore(); await current.initialize(); await current.flush(); installStorageObserver();
}
export function localClientStateSnapshot() { const state = store?.snapshot() ?? initial; return failedMutations.size ? { ...state, dirty: true, localRecoverable: false, error: state.error ?? "Изменения настроек ещё не записаны на устройство." } : state; }
export function subscribeLocalClientState(listener: () => void) { return localStore().subscribe(listener); }
export async function flushLocalClientState() {
  if (timer !== null) { clearTimeout(timer); timer = null; }
  for (const [key, value] of failedMutations) { if (value === null) window.localStorage.removeItem(key); else window.localStorage.setItem(key, value); }
  await localStore().flush();
}
/** Re-register immediately after original owner lifecycle resets its registry. */
export function registerLocalClientStateBarrier() {
  if (typeof window === "undefined" || !isLocalDesktop()) return;
  registerDraftFlush(flushLocalClientState, "offline-client-state");
  registerDraftSaveState(() => {
    const state = localClientStateSnapshot();
    return { dirty: state.dirty, saving: state.saving, conflict: state.conflict, offline: !!state.error, localRecoverable: state.localRecoverable };
  }, "offline-client-state");
}
export async function reloadLocalClientStateAfterImport() { await localStore().reloadAfterImport(); }
export async function resolveLocalClientStateConflict(choice: "local" | "remote") {
  await localStore().resolveConflict(choice);
  if (choice === "remote") failedMutations.clear();
  installStorageObserver();
  window.dispatchEvent(new Event("kontur:preferences-changed")); window.dispatchEvent(new Event("kontur:shortcuts-changed"));
}