"use client";

import { FormEvent, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Icon } from "@/components/icon";
import { cancelAssistantRun, sendAssistantMessage, subscribeAssistantEvents } from "@/lib/api";
import { modelLabel } from "@/lib/model-policy";
import { usePreferences } from "@/lib/kontur-preferences";
import { ApiError } from "@/lib/types";
import { assistantFailureMessage, assistantStatusLabel, readableError } from "@/lib/ui-copy";
import { notifyHistoryChanged } from "@/lib/history-events";
import type { Effort, Message, ModelSelection } from "@/lib/types";

import {useOwnerRecord,useOwnerSyncIdentity,getOwnerRecord} from "@/lib/owner-sync";
import type {SyncContext} from "@/lib/owner-sync-store";
import {SyncConflict} from "./sync-conflict";

import {NewConversationDraft} from "@/lib/new-conversation-draft";
import {registerDraftFlush,registerDraftSaveState,markDraftStateChanged} from "@/lib/draft-lifecycle";
const EMPTY_NEW_DRAFT={text:"",conversationId:null as string|null,dirty:false,pending:false,uncertain:false,error:null as string|null};
const effortOptions: Effort[] = ["low", "medium", "high", "xhigh"];

const COMPOSER_DRAFT_PREFIX = "kontur:composer-draft:v1";

export function Composer({
  conversationId,
  syncContext,
  draftProjectId=null,
  sessionId,
  sealed = false,
  onMessage,
  onStartConversation,
  onConversationReady,
  draft,
  placeholder = "Спросить о решении…",
  beforeSend,
}: {
  conversationId?: string;
  syncContext?: SyncContext;
  draftProjectId?: string|null;
  sessionId?: string;
  sealed?: boolean;
  onMessage: (message: Message) => void;
  onStartConversation?: (title: string) => Promise<string>;
  onConversationReady?: (id: string) => void;
  draft?: { text: string; key: number };
  placeholder?: string;
  beforeSend?: () => Promise<unknown>;
}) {
  const ownerIdentity=useOwnerSyncIdentity();
  const handledExternalDraftRef=useRef<number|null>(null);
  const latestTextRef=useRef("");
  const previousNewDraftRef=useRef<NewConversationDraft|null>(null);
  const [draftConversationId,setDraftConversationId]=useState<string>();
  const newDraft=useMemo(()=>{
    if(!ownerIdentity||conversationId)return null;
    let storage:Storage|null=null;try{storage=window.localStorage;}catch{}
    return new NewConversationDraft(ownerIdentity,draftProjectId,storage,{
      create:async()=>{
        if(!onStartConversation)throw new Error("Для черновика пока не создан чат.");
        // Never send unsent personal text as an unbound create request title.
        return onStartConversation("Новый чат");
      },
      save:async(id,value)=>{
        const record=getOwnerRecord({kind:"composer",conversation_id:id},{text:value},ownerIdentity);
        record.edit({text:value});await record.refresh();await record.flushAll();
        const state=record.snapshot();return state.ready&&!state.dirty&&!state.saving&&!state.conflict&&!state.unsupported;
      },
      ready:id=>{startedConversationRef.current=id;setDraftConversationId(id);onConversationReady?.(id);},
    });
  },[ownerIdentity,draftProjectId]);
  const newDraftState=useSyncExternalStore(newDraft?.subscribe??(()=>()=>{}),newDraft?.snapshot??(()=>EMPTY_NEW_DRAFT),()=>EMPTY_NEW_DRAFT);
  const effectiveDraftConversationId=conversationId??draftConversationId??newDraftState.conversationId??undefined;
  const boundSyncContext=!sealed&&(syncContext?.conversation_id||effectiveDraftConversationId)?syncContext??{kind:"composer" as const,conversation_id:effectiveDraftConversationId}:null;
  const sync=useOwnerRecord(boundSyncContext,{text:""});
  const { preferences, updatePreferences } = usePreferences();
  const { model, effort, tutorMode } = preferences;
  const tutorAction = tutorMode === "feynman" ? "feynman" : "hint";
  const [text, setText] = useState("");latestTextRef.current=text;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [eventsNotice, setEventsNotice] = useState<string | null>(null);
  const draftKeyRef = useRef<string | null>(null);
  const localMessageKeyRef = useRef<string | null>(null);
  const partialMessageRunIdRef = useRef<string | null>(null);
  const hydratedDraftKeyRef = useRef<string | null>(null);
  const draftStorageKeyRef = useRef<string | null>(null);
  const skipDraftPersistRef = useRef(false);
  const startedConversationRef = useRef<string | undefined>(conversationId);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const draftStorageKey = getComposerDraftStorageKey(sessionId, effectiveDraftConversationId)+":"+encodeURIComponent(ownerIdentity??"pending")+":"+encodeURIComponent(draftProjectId??"no-project");
  useEffect(()=>{if(sync.state.ready&&typeof sync.state.value.text==="string"&&(!newDraftState.dirty||sync.state.dirty))setText(sync.state.value.text);
    if(newDraft&&sync.state.ready&&!sync.state.dirty&&!sync.state.conflict&&sync.state.version>0&&typeof sync.state.value.text==="string")newDraft.acknowledge(sync.state.value.text);
  },[sync.state.ready,sync.state.value,sync.state.dirty,sync.state.conflict,sync.state.version]);
  useEffect(()=>{if(newDraft&&newDraftState.dirty)setText(newDraftState.text);},[newDraft,newDraftState.text,newDraftState.dirty]);
  useEffect(()=>{
    const previous=previousNewDraftRef.current;previousNewDraftRef.current=newDraft;
    if(newDraft&&previous&&previous!==newDraft&&latestTextRef.current)newDraft.edit(latestTextRef.current);
  },[newDraft]);
  useEffect(()=>{
    if(!newDraft)return;
    const scope=`new-chat:${ownerIdentity}:${draftProjectId??"no-project"}`;
    const unsubscribe=newDraft.subscribe(markDraftStateChanged);
    const unstate=registerDraftSaveState(()=>{const state=newDraft.snapshot();return {dirty:state.dirty,saving:state.pending,conflict:state.uncertain,
      offline:typeof navigator!=="undefined"&&!navigator.onLine,localRecoverable:newDraft.localRecoverable()};},scope,()=>newDraft.snapshot().text);
    const unflush=registerDraftFlush(()=>newDraft.flush(),scope,()=>newDraft.snapshot().text);
    return()=>{unsubscribe();unstate();unflush();};
  },[newDraft,ownerIdentity,draftProjectId]);
  useEffect(()=>{
    if(!newDraft||!newDraftState.dirty||newDraftState.pending||newDraftState.uncertain||!newDraftState.text.trim()||newDraftState.conversationId)return;
    const timer=window.setTimeout(()=>{void newDraft.flush().catch(()=>undefined);},600);
    return()=>window.clearTimeout(timer);
  },[newDraft,newDraftState.dirty,newDraftState.pending,newDraftState.uncertain,newDraftState.text,newDraftState.conversationId]);

  useEffect(() => { if (conversationId) startedConversationRef.current = conversationId; }, [conversationId]);
  useEffect(() => {
    function resizeInput() {
      const input = inputRef.current;
      if (!input) return;
      input.style.height = "0px";
      input.style.height = `${Math.max(40, Math.min(180, input.scrollHeight))}px`;
    }
    resizeInput();
    window.addEventListener("resize", resizeInput);
    return () => window.removeEventListener("resize", resizeInput);
  }, [text]);
  useEffect(() => {
    if (!draft||handledExternalDraftRef.current===draft.key) return;
    handledExternalDraftRef.current=draft.key;
    skipDraftPersistRef.current = true;
    setText(draft.text);
    sync.edit({text:draft.text});newDraft?.edit(draft.text);
    hydratedDraftKeyRef.current = draftStorageKey;
    draftStorageKeyRef.current = draftStorageKey;
    invalidateDraftKey();
    inputRef.current?.focus();
  }, [draft, draftStorageKey]);

  useEffect(() => {
    if (draft||newDraft||boundSyncContext||!ownerIdentity) return;
    skipDraftPersistRef.current = true;
    let restored = "";
    try {
      restored = window.localStorage.getItem(draftStorageKey) ?? "";
    } catch { /* local drafts are best effort */ }
    setText(restored);
    hydratedDraftKeyRef.current = draftStorageKey;
    draftStorageKeyRef.current = draftStorageKey;
    invalidateDraftKey();
  }, [draft, draftStorageKey]);

  useEffect(() => {
    if(newDraft||boundSyncContext||!ownerIdentity)return;
    if (skipDraftPersistRef.current) {
      skipDraftPersistRef.current = false;
      return;
    }
    if (hydratedDraftKeyRef.current !== draftStorageKey) return;
    try {
      if (text) window.localStorage.setItem(draftStorageKey, text);
      else window.localStorage.removeItem(draftStorageKey);
    } catch { /* local drafts are best effort */ }
  }, [draftStorageKey, text]);

  useEffect(()=>{invalidateDraftKey();},[model,effort,tutorMode]);

  function invalidateDraftKey() {
    draftKeyRef.current = null;
    localMessageKeyRef.current = null;
  }

  function getDraftKey(): string {
    if (!draftKeyRef.current) {
      draftKeyRef.current = globalThis.crypto?.randomUUID?.() ?? `assistant-draft-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    return draftKeyRef.current;
  }

  useEffect(() => {
    if (!activeRunId) return undefined;
    return subscribeAssistantEvents(
      activeRunId,
      (event) => {
        const payloadStatus = event.payload && typeof event.payload.status === "string" ? event.payload.status : null;
        if (event.status || payloadStatus) setRunStatus(event.status ?? payloadStatus ?? null);
        else if (event.event_type && event.event_type !== "snapshot" && event.event_type !== "snapshot_eof") setRunStatus(event.event_type);
      },
      (status) => setRunStatus(status),
      () => setEventsNotice("Обновление статуса прервалось. Продолжаем ждать ответ."),
    );
  }, [activeRunId]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = text.trim();
    if (!message || busy || sealed) return;
    setBusy(true);
    setError(null);
    setEventsNotice(null);
    setRunStatus(null);
    const idempotencyKey = getDraftKey();
    const selection: ModelSelection = model === "auto" ? { model } : { model, effort };
    let reachedTerminal = false;
    try {
      await beforeSend?.();
      let effectiveConversationId = conversationId ?? startedConversationRef.current;
      if(!effectiveConversationId&&newDraft){await newDraft.flush();effectiveConversationId=newDraft.snapshot().conversationId??undefined;}
      if (!effectiveConversationId && onStartConversation) {
        effectiveConversationId = await onStartConversation(message.slice(0, 100));
        startedConversationRef.current = effectiveConversationId;
        onConversationReady?.(effectiveConversationId);
      }
      if (localMessageKeyRef.current !== idempotencyKey) {
        onMessage({
          id: "local-user-" + idempotencyKey,
          role: "user",
          content: message,
          created_at: new Date().toISOString(),
        });
        localMessageKeyRef.current = idempotencyKey;
      }
      const result = await sendAssistantMessage({
        message,
        selection,
        tutorMode,
        conversationId: effectiveConversationId,
        sessionId,
        idempotencyKey,
        onRunAccepted: (id) => { setActiveRunId(id); notifyHistoryChanged(); },
        onRunStatus: setRunStatus,
      });
      reachedTerminal = isTerminalAssistantStatus(result.status);
      if (result.status === "succeeded" || result.status === "completed") {
        onMessage({
          id: result.id,
          role: "assistant",
          content: result.content,
          created_at: new Date().toISOString(),
          model: result.provider_label ?? (result.effective ? modelLabel(result.effective) : null),
        });
        setText("");
        sync.edit({text:""});newDraft?.edit("");
        clearComposerDraft(draftStorageKey);
        draftKeyRef.current = null;
        localMessageKeyRef.current = null;
        partialMessageRunIdRef.current = null;
      } else {
        if ((result.status === "cancelled" || result.status === "timed_out") && result.content.trim()) appendPartialMessage(result.id, result.content);
        setError(assistantFailureMessage(result.error_code) ?? assistantStatusLabel(result.status) + ". Попробуйте отправить запрос ещё раз.");
      }
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось получить ответ. Сообщение сохранено в поле — попробуйте ещё раз."));
      if (reason instanceof ApiError && typeof reason.details.status === "string") {
        setRunStatus(reason.details.status);
        reachedTerminal = isTerminalAssistantStatus(reason.details.status);
      }
    } finally {
      setBusy(false);
      notifyHistoryChanged();
      if (reachedTerminal) {
        draftKeyRef.current = null;
        localMessageKeyRef.current = null;
        setActiveRunId(null);
        setRunStatus(null);
      }
    }
  }

  async function cancelRun() {
    if (!activeRunId) return;
    const runId = activeRunId;
    try {
      const result = await cancelAssistantRun(runId);
      const status = result.status ?? "unknown";
      setRunStatus(status);
      if (typeof result.content === "string" && result.content.trim()) appendPartialMessage(result.id, result.content);
      if (isTerminalAssistantStatus(status)) {
        setActiveRunId(null);
        draftKeyRef.current = null;
        localMessageKeyRef.current = null;
        partialMessageRunIdRef.current = null;
        setError(status === "cancelled" ? "Запрос отменён." : assistantStatusLabel(status));
      } else {
        setError("Отменяем запрос. Дождитесь подтверждения.");
      }
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось отменить запрос. Попробуйте ещё раз."));
    }
  }

  function appendPartialMessage(runId: string, content: string) {
    if (!content.trim() || partialMessageRunIdRef.current === runId) return;
    partialMessageRunIdRef.current = runId;
    onMessage({ id: runId, role: "assistant", content, created_at: new Date().toISOString(), model: null });
  }

  return (
    <div className={"composer-wrap" + (sealed ? " is-sealed" : "")}>
      {sealed ? (
        <div className="sealed-notice">
          <Icon name="lock" size={15} />
          <span>Экзамен запечатан: помощник, образцы и подсказки отключены</span>
        </div>
      ) : null}
      {error ? (
        <div className="composer-error" role="alert">
          <Icon name="warning" size={15} />
          <span>{error}</span>
        </div>
      ) : null}
      {newDraftState.error&&<div className="composer-error" role="alert"><span>{newDraftState.error}{newDraftState.uncertain?" Автоматический повтор создания отключён: проверьте историю чатов.":""}</span>
        {newDraftState.uncertain&&<button type="button" onClick={()=>{newDraft?.allowExplicitRetry();void newDraft?.flush().catch(()=>undefined);}}>Я проверил историю — создать чат повторно</button>}
      </div>}
      <SyncConflict state={sync.state} resolve={sync.resolve}/>
      <form className="composer" onSubmit={submit}>
        <textarea
          aria-label="Сообщение репетитору"
          className="composer-input"
          ref={inputRef}
          disabled={sealed || busy}
          onChange={(event) => {
            invalidateDraftKey();
            setText(event.target.value);
            sync.edit({text:event.target.value});newDraft?.edit(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={sealed ? "Подсказки отключены на время экзамена" : placeholder}
          rows={2}
          style={{ minHeight: 44 }}
          value={text}
        />
        <div className="composer-footer">
          <div aria-label="Как помочь" className="composer-tutor-actions" role="group" style={{ alignItems: "center", display: "inline-flex", gap: 4, minHeight: 44 }}>
            <button aria-pressed={tutorAction === "hint"} className={tutorAction === "hint" ? "outline-button" : "secondary-button"} disabled={sealed || busy} onClick={() => { invalidateDraftKey(); updatePreferences({ tutorMode: "hint" }); }} style={{ minHeight: 44, padding: "0 10px" }} type="button"><Icon name="help" size={15} />Подсказка</button>
            <button aria-pressed={tutorAction === "feynman"} className={tutorAction === "feynman" ? "outline-button" : "secondary-button"} disabled={sealed || busy} onClick={() => { invalidateDraftKey(); updatePreferences({ tutorMode: "feynman" }); }} style={{ minHeight: 44, padding: "0 10px" }} type="button">Объяснить</button>
          </div>
          <span className="composer-footer-spacer" />
          {busy && activeRunId ? <button aria-label="Отменить запрос" title="Отменить запрос" className="send-button cancel-button" onClick={cancelRun} style={{ height: 44, minHeight: 44, minWidth: 44, width: 44 }} type="button"><Icon name="stop" size={19} /></button> : <button aria-label={error ? "Повторить отправку" : "Отправить сообщение"} title={error ? "Повторить" : "Отправить"} className="send-button" disabled={sealed || busy || !text.trim()} style={{ height: 44, minHeight: 44, minWidth: 44, width: 44 }} type="submit">{busy ? <span className="button-spinner" /> : <Icon name="arrow-up" size={21} />}</button>}
        </div>
      </form>
      {runStatus ? <p className="composer-caption" role="status"><Icon name="refresh" size={13} />{assistantStatusLabel(runStatus)}</p> : null}
      {eventsNotice ? <p className="composer-caption"><Icon name="info" size={13} />{eventsNotice}</p> : null}
    </div>
  );
}

function isTerminalAssistantStatus(status: string | null | undefined): boolean {
  return status === "succeeded" || status === "completed" || status === "failed" || status === "timed_out" || status === "cancelled" || status === "unavailable";
}

function getComposerDraftStorageKey(sessionId?: string, conversationId?: string): string {
  const sessionPart = sessionId ? `session-${encodeURIComponent(sessionId)}` : "session-none";
  const conversationPart = conversationId ? `conversation-${encodeURIComponent(conversationId)}` : "conversation-new";
  return `${COMPOSER_DRAFT_PREFIX}:${sessionPart}:${conversationPart}`;
}

function clearComposerDraft(storageKey: string): void {
  try { window.localStorage.removeItem(storageKey); } catch { /* local drafts are best effort */ }
}
