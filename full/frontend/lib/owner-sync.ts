"use client";

import {useEffect, useMemo, useSyncExternalStore} from "react";
import {DEMO_MODE, request, getActiveExam} from "./api";
import {flushOpenDrafts, registerDraftFlush, getOpenDraftSaveState, resetDraftLifecycle, getDraftMutationEpoch, markDraftStateChanged} from "./draft-lifecycle";
import {installDesktopSaveBridge} from "./desktop-save-bridge";
import {isLocalDesktop} from "./local-fetch";
import {registerLocalClientStateBarrier} from "./local-client-state";
import {setPreferenceOwner} from "./kontur-preferences";
import {setLocalDraftOwner} from "./local-drafts";
import {OwnerSyncStore, type SyncContext, type SyncState, type SyncValue} from "./owner-sync-store";

export const OWNER_SYNC_CHANGED = "kontur:owner-sync-changed";
export const OWNER_SESSION_CHANGED = "kontur:owner-session-changed";
const ownerListeners = new Set<() => void>();
const stores = new Map<string, OwnerSyncStore>();
let owner: string | null = null;
let generation = 0;
let stopPolling: (() => void) | null = null;
const unavailable: SyncState = {value: {}, version: 0, remote: null, dirty: false, saving: false,
  conflict: false, offline: false, unsupported: false, ready: false, error: null};
type ChangePage = {cursor: number; latest_cursor: number; has_more: boolean; reset_required: boolean;
  changes: {revision: number; topic: string}[]};

export function stopOwnerSync() {
  generation++;resetDraftLifecycle();registerLocalClientStateBarrier();
  stopPolling?.(); stopPolling = null;
  stores.forEach(store => store.close()); stores.clear();
  owner = null; ownerListeners.forEach(listener => listener());
  setLocalDraftOwner(null);setPreferenceOwner(null);
}

export async function startOwnerSync(): Promise<void> {
  // One private local owner survives route remounts; detached dirty stores must keep flushing.
  if (isLocalDesktop() && owner !== null) return;
  stopOwnerSync();
  if(typeof window!=="undefined")installDesktopSaveBridge(window,getOwnerSaveState,flushOwnerWork,async()=>{
    const expected=owner,before=generation;if(!expected)return false;
    const active=await getActiveExam();if(active!==null)return false;
    // Verify that the authoritative null still belongs to this account.
    const identity=await request<{owner_id:string}>("/auth/session");
    if(identity.owner_id!==expected){stopOwnerSync();window.dispatchEvent(new Event(OWNER_SESSION_CHANGED));return false;}
    return owner===expected&&generation===before;
  });
  if (DEMO_MODE) return;
  const current = generation;
  const identity = await request<{owner_id: string}>("/auth/session");
  if (generation !== current) return;
  if(typeof identity.owner_id!=="string"||!identity.owner_id||identity.owner_id.length>128)throw new Error("Invalid owner identity");
  owner = identity.owner_id; setLocalDraftOwner(owner);setPreferenceOwner(owner);ownerListeners.forEach(listener => listener());
  let cursor = 0; let running = false; let active = true; let supported = true;
  const poll = async () => {
    if (!active || !supported || running || document.hidden) return;
    running = true;
    try {
      const topics = new Set<string>(); let reset = false;
      for (let page = 0; page < 10 && active; page++) {
        const result = await request<ChangePage>(`/sync/changes?after=${cursor}&limit=200`,{headers:{"X-Sync-Owner":identity.owner_id}});
        if (generation !== current || !active) return;
        result.changes.forEach(change => topics.add(change.topic));
        reset ||= result.reset_required;
        cursor = result.cursor;
        if (!result.has_more) break;
      }
      if (reset || topics.size) window.dispatchEvent(new CustomEvent(OWNER_SYNC_CHANGED, {detail: {topics: [...topics], reset}}));
    } catch (reason) {
      const status = (reason as {status?: number}).status;
      if (status === 404) supported = false;
      if (status === 401 || status === 403) {stopOwnerSync();window.dispatchEvent(new Event(OWNER_SESSION_CHANGED));}
    } finally {running = false;}
  };
  const refresh = () => {
    if(!document.hidden)window.dispatchEvent(new CustomEvent(OWNER_SYNC_CHANGED,{detail:{topics:[],reset:true}}));
    void poll(); void refreshSyncRecords();
  };
  const timer = window.setInterval(() => void poll(), 10_000);
  window.addEventListener("focus", refresh); window.addEventListener("online", refresh);
  document.addEventListener("visibilitychange", refresh);
  stopPolling = () => {
    active = false; window.clearInterval(timer);
    window.removeEventListener("focus", refresh); window.removeEventListener("online", refresh);
    document.removeEventListener("visibilitychange", refresh);
  };
  void poll();
}

export async function refreshSyncRecords() {
  await Promise.allSettled([...stores.values()].map(store => store.refresh()));
}

export function getOwnerRecord(context: SyncContext, initial: SyncValue, expectedOwner:string|null=owner):OwnerSyncStore {
  if(!owner||owner!==expectedOwner||DEMO_MODE)throw new Error("Сессия владельца изменилась.");
  const currentOwner=owner;
  const key = `${currentOwner}:${JSON.stringify(context)}`;
  let record = stores.get(key);
  if (!record) {
    const expectedGeneration = generation;
    record = new OwnerSyncStore(currentOwner, context, initial, <T,>(path: string, init?: RequestInit) => {
    if (generation !== expectedGeneration || owner !== currentOwner) return Promise.reject(new Error("Сессия изменилась."));
    const headers=new Headers(init?.headers);headers.set("X-Sync-Owner",currentOwner);
    return request<T>(path, {...init,headers}).catch(reason=>{
      if((reason as {code?:string}).code==="OWNER_SESSION_CHANGED"){
      stopOwnerSync();window.dispatchEvent(new Event(OWNER_SESSION_CHANGED));
      }
      throw reason;
    });
    }, typeof localStorage === "undefined" ? null : localStorage);
    record.subscribe(markDraftStateChanged);
    stores.set(key, record);
  }
  return record;
}

export function useOwnerRecord(context: SyncContext | null, initial: SyncValue) {
  const currentOwner = useSyncExternalStore(
    listener => {ownerListeners.add(listener); return () => {ownerListeners.delete(listener);};},
    () => owner, () => null,
  );
  const contextKey = JSON.stringify(context);
  const store = useMemo(() => {
    if (!currentOwner || !context || DEMO_MODE) return null;
    return getOwnerRecord(context,initial,currentOwner);
  }, [currentOwner, contextKey]);
  const state = useSyncExternalStore(store?.subscribe ?? (() => () => {}),
    store?.snapshot ?? (() => unavailable), () => unavailable);
  useEffect(() => {
    if (!store) return;
    void store.refresh();
    const refresh = (event: Event) => {
      const detail = (event as CustomEvent<{topics: string[]; reset: boolean}>).detail;
      if (detail.reset || detail.topics.includes("sync_records")) void store.refresh();
    };
    window.addEventListener(OWNER_SYNC_CHANGED, refresh);
    const unregister = registerDraftFlush(() => store.flushAll());
    return () => {window.removeEventListener(OWNER_SYNC_CHANGED, refresh); unregister();};
  }, [store]);
  useEffect(() => {
    if (!store || !state.ready || !state.dirty || state.conflict || state.unsupported || state.saving || state.error) return;
    const timer = window.setTimeout(() => {void store.flushAll().catch(() => undefined);}, 800);
    return () => window.clearTimeout(timer);
  }, [store, state.ready, state.dirty, state.conflict, state.unsupported, state.saving, state.value, state.error]);
  return {state, edit: (value: SyncValue) => store?.edit(value),
    resolve: (choice: "local" | "remote") => store?.resolve(choice), flush: () => store?.flushAll() ?? Promise.resolve()};
}

/** Read-only bounded state and async flush acknowledgement for a future trusted host. */
export function getOwnerSaveState() {
  const states = [...stores.values()].map(store => store.snapshot());
  const editors=getOpenDraftSaveState();
  return Object.freeze({dirty:editors.dirty||states.some(state=>state.dirty),saving:editors.saving||states.some(state=>state.saving),
    conflict:editors.conflict||states.some(state=>state.conflict),offline:editors.offline||states.some(state=>state.offline),
    generation,revision:getDraftMutationEpoch(),ready:owner!==null&&states.every(state=>state.ready)});
}
export async function flushOwnerWork() {
  const before=generation;
  let completed=true;
  try {await flushOpenDrafts();}catch{completed=false;}
  const state=getOwnerSaveState();const editors=getOpenDraftSaveState();
  return {serverConfirmed:completed&&before===generation&&state.ready&&!state.dirty&&!state.saving&&!state.conflict,
    localRecoverable:editors.localRecoverable&&[...stores.values()].every(store=>store.localRecoverable()),state};
}

export function useOwnerSyncIdentity() {
  return useSyncExternalStore(listener => {ownerListeners.add(listener); return () => {ownerListeners.delete(listener);};},
    () => owner, () => null);
}
