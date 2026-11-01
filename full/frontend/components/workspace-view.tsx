"use client";
import {archiveDraftConflict} from "@/lib/local-drafts";

import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useSearchParams,useRouter } from "next/navigation";
import {createPortal} from "react-dom";
import {useKonturShell} from "./kontur-shell";
import {StatementCanvas} from "./statement-canvas";
import {OWNER_SYNC_CHANGED} from "@/lib/owner-sync";
import {PracticeAnswer} from "./practice-answer";
import {getPreferences,usePreferences} from "@/lib/kontur-preferences";
import {useShortcuts} from "@/lib/kontur-shortcuts";
import {getTaskProgress,setBookmark} from "@/lib/kontur-api";
import {readCatalogSequence,rememberCatalogSequence,sequencePosition,type CatalogSequence} from "@/lib/catalog-sequence";
import Link from "next/link";
import { Composer } from "@/components/composer";
import { Icon } from "@/components/icon";
import { CodeEditor } from "@/components/code-editor";
import { InfiniteBoard } from "@/components/infinite-board";
import { TaskSpreadsheetFiles, isSpreadsheetName } from "@/components/task-spreadsheet-files";
import { TaskImages } from "@/components/task-images";
import { ChatView } from "@/components/chat-view";
import { DeviceFileRow } from "@/components/device-file-row";
import { downloadDeviceFile, loadDeviceAsset, MAX_DEVICE_FILES, MAX_FILE_BYTES, readLocalDeviceFile, type DeviceFile } from "@/lib/device-files";
import { ResizableWorkspace } from "@/components/resizable-workspace";
import { notifyHistoryChanged } from "@/lib/history-events";
import {
  DEMO_MODE,
  createAnnotation,
  deleteAnnotation,
  createAttempt,
  createSolveSession,
  getCapabilities,
  getSolveSession,
  getTaskCatalog,
  getLatestSolveSession,
  getLatestSolveSessionForConversation,
  prepareAsset,
  saveCode,
  updateAttempt,
} from "@/lib/api";
import { DEMO_CAPABILITIES, DEMO_TASK } from "@/lib/demo";
import { statementToText } from "@/lib/sanitize";
import {flushOpenDrafts} from "@/lib/draft-lifecycle";
import { registerDraftFlush,registerDraftSaveState,acceptDraftHandoff } from "@/lib/draft-lifecycle";
import {assessLocalDraft,readLocalDraft,saveLocalDraft,clearLocalDraft,isLocalDraftDurable} from "@/lib/local-drafts";
import { readableError } from "@/lib/ui-copy";
import { ApiError } from "@/lib/types";
import { readDevicePythonRuns, useDevicePython } from "@/lib/use-device-python";
import type {
  Annotation,
  AssetRef,
  Attempt,
  Capabilities,
  CodeDocument,
  CodeRun,
  Message,
  SolveSession,
} from "@/lib/types";

type WorkspaceTab = "code" | "files" | "board";

const tabs: Array<{ key: WorkspaceTab; label: string; icon: "code" | "file" | "board" | "attempts" }> = [
  { key: "code", label: "Код", icon: "code" },
  { key: "files", label: "Файлы", icon: "file" },
  { key: "board", label: "Доска", icon: "board" },

];
const EMPTY_ASSETS: AssetRef[] = [];

export function WorkspaceView() {
  const params = useSearchParams();
  const revisionId = params.get("revision");
  const conversationId = params.get("conversation");
  const sessionId=params.get("session");
  if(!revisionId&&!conversationId&&!sessionId)return <main className="page-state"><h1>Выберите задание</h1><p>Откройте задание в каталоге, чтобы начать решение.</p><Link href="/tasks" className="primary-button">Банк заданий</Link></main>;
  return <WorkspaceSession key={sessionId ?? revisionId ?? conversationId ?? "empty"} freshCorrection={params.get("fresh")==="correction"||params.get("fresh")==="new"} sessionId={sessionId} revisionId={revisionId} conversationId={conversationId} projectId={params.get("project")} />;
}

function WorkspaceSession({freshCorrection,sessionId, revisionId, conversationId, projectId }: {freshCorrection:boolean;sessionId:string|null; revisionId: string | null; conversationId: string | null; projectId: string | null }) {
  const router=useRouter();const shell=useKonturShell();const {preferences}=usePreferences();
  const [sequence,setSequence]=useState<CatalogSequence|null>(null);
  const [navigating,setNavigating]=useState(false);const [navigationError,setNavigationError]=useState<string|null>(null);
  const [bookmarked,setBookmarked]=useState(false);const [bookmarkBusy,setBookmarkBusy]=useState(false);
  const [boardImport,setBoardImport]=useState<{assetId:string;revisionHash:string;requestId:string}|undefined>(undefined);
  const [questionDraft,setQuestionDraft]=useState<{text:string;key:number}|undefined>(undefined);
  useEffect(()=>setSequence(readCatalogSequence()),[]);

  const [chatOnly, setChatOnly] = useState(false);
  const loadPromise = useRef<Promise<[SolveSession, Capabilities]> | null>(null);
  const [session, setSession] = useState<SolveSession | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tab, setTab] = useState<WorkspaceTab>("code");
  const [focusedPane, setFocusedPane] = useState<"split" | "task" | "tools">("split");
  const [mobilePane, setMobilePane] = useState<"task" | "tools">("task");
  const taskModeRef = useRef<HTMLButtonElement>(null);
  const [code, setCode] = useState("");
  const [codeVersion, setCodeVersion] = useState(1);
  const [codeSaving, setCodeSaving] = useState(false);
  const [codeError, setCodeError] = useState<string | null>(null);
  const [codeConflict,setCodeConflict]=useState<string|null>(null);
  const codeConflictRef=useRef<string|null>(null);
  const [run, setRun] = useState<CodeRun | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [pendingMark, setPendingMark] = useState<Annotation | null>(null);
  const pendingMarkRef=useRef<Annotation|null>(null);pendingMarkRef.current=pendingMark;
  const [markBusy, setMarkBusy] = useState(false);
  const [markError, setMarkError] = useState<string | null>(null);
  const markBusyRef = useRef(false);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [assetBusyId, setAssetBusyId] = useState<string | null>(null);
  const [assetError, setAssetError] = useState<string | null>(null);
  const [localFiles, setLocalFiles] = useState<DeviceFile[]>([]);
  const [localFileError, setLocalFileError] = useState<string | null>(null);
  const [localFileBusy, setLocalFileBusy] = useState(false);
  const [selectionMessage, setSelectionMessage] = useState<string | null>(null);
  const codeSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const initialCode = useRef(true);
  const codeRef = useRef("");
  const codeVersionRef = useRef(1);
  const savedCodeRef = useRef<CodeDocument | null>(null);
  const codeSessionIdRef = useRef<string | null>(null);
  const codeSavePromiseRef = useRef<Promise<CodeDocument> | null>(null);
  const codeFlushPromiseRef = useRef<Promise<CodeDocument | null> | null>(null);
  const mountedRef = useRef(false);
  const devicePythonScope = `practice:${session?.id ?? sessionId ?? revisionId ?? "pending"}:browser`;
  const devicePythonEnabled = Boolean(session && !session.sealed_exam && !chatOnly);
  const devicePythonScopeRef = useRef(devicePythonScope);
  devicePythonScopeRef.current = devicePythonScope;
  const practiceRunRef = useRef<{scope: string} | null>(null);
  const devicePython = useDevicePython({
    assets: session?.task.assets ?? EMPTY_ASSETS,
    localFiles,
    lineNumber: session?.task.line_number,
    revisionHash: session?.task.revision.content_hash ?? "",
    scope: devicePythonScope,
    enabled: devicePythonEnabled,
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  async function openRevision(id:string){if(freshCorrection)return createSolveSession(id,projectId);const progress=await getTaskProgress([id]);return getPreferences().restoreWorkspace&&progress[0]?.solve_session_id?getSolveSession(progress[0].solve_session_id):createSolveSession(id,projectId);}
  useEffect(() => {
    let cancelled = false;
    if (!loadPromise.current) loadPromise.current = Promise.all([sessionId ? getSolveSession(sessionId) : revisionId ? openRevision(revisionId) : conversationId ? getLatestSolveSessionForConversation(conversationId) : getLatestSolveSession(), getCapabilities()]);
    loadPromise.current
      .then(([nextSession, nextCapabilities]) => {
        if (cancelled) return;
        setSession(nextSession);
        setCapabilities(nextCapabilities);
        const scope="practice:"+nextSession.id;
        const local=readLocalDraft<string>(scope,"code");
        const recovery=typeof local?.content==="string"?assessLocalDraft(local,nextSession.code.version,nextSession.code.content):{status:"none",content:null};
        const restored=recovery.status==="restore"&&!nextSession.sealed_exam?recovery.content!:nextSession.code.content;
        if(recovery.status==="confirmed")clearLocalDraft(scope,"code");
        codeConflictRef.current=recovery.status==="conflict"||(recovery.status==="restore"&&nextSession.sealed_exam)?recovery.content:null;
        setCodeConflict(codeConflictRef.current);
        setCode(restored);
        setCodeVersion(nextSession.code.version);
        codeRef.current = restored;
        codeVersionRef.current = nextSession.code.version;
        savedCodeRef.current = nextSession.code;
        codeSessionIdRef.current = nextSession.id;
        codeSavePromiseRef.current = null;
        setRun(nextSession.runs.at(-1) ?? null);
        setMessages(nextSession.messages);
        const pendingHighlight=readLocalDraft<Annotation>(scope,"mark")?.content;
        const validHighlight=pendingHighlight?.kind==="highlight"&&pendingHighlight.anchor?.content_hash===nextSession.task.revision.content_hash?pendingHighlight:null;
        pendingMarkRef.current=validHighlight;setPendingMark(validHighlight);
        setAnnotations(validHighlight?[...nextSession.annotations,validHighlight]:nextSession.annotations);
        setAttempts(nextSession.attempts);
        notifyHistoryChanged();
        let surface:WorkspaceTab=[4,7,11,12].includes(nextSession.task.line_number)?"board":"code";
        if(preferences.restoreWorkspace){try{const saved=sessionStorage.getItem("kontur:surface:"+nextSession.id);if(saved==="board"||saved==="code"||saved==="files")surface=saved;}catch{}}
        setTab(surface);
        if(revisionId){const url=new URL(window.location.href);url.searchParams.delete("revision");url.searchParams.set("session",nextSession.id);window.history.replaceState(null,"",url.pathname+url.search);}
        void getTaskProgress([nextSession.task.revision.id]).then(items=>setBookmarked(items[0]?.bookmarked??false)).catch(()=>undefined);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          if (conversationId && reason instanceof ApiError && reason.code === "SESSION_NOT_FOUND") {
            setChatOnly(true);
            return;
          }
          setLoadError(readableError(reason, "Не удалось открыть разбор. Вернитесь в банк заданий и выберите задачу ещё раз."));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, projectId, revisionId,sessionId,freshCorrection]);

  useEffect(()=>{
    if(!session)return;
    let active=true;let reading=false;
    const refresh=async(event:Event)=>{
      const detail=(event as CustomEvent<{topics:string[];reset:boolean}>).detail;
      if(reading||(!detail.reset&&!detail.topics.some(topic=>["code","board","annotations","messages","progress","exams"].includes(topic))))return;
      reading=true;
      try{
        const latest=await getSolveSession(session.id);
        if(!active||codeSessionIdRef.current!==session.id)return;
        if(latest.code.version>codeVersionRef.current&&!codeSavePromiseRef.current&&!codeFlushPromiseRef.current){
          const dirty=codeRef.current!==savedCodeRef.current?.content;
          savedCodeRef.current=latest.code;codeVersionRef.current=latest.code.version;setCodeVersion(latest.code.version);
          if(dirty||codeConflictRef.current!==null){codeConflictRef.current=codeRef.current;setCodeConflict(codeRef.current);}
          else if(codeRef.current!==latest.code.content){codeRef.current=latest.code.content;setCode(latest.code.content);}
        }
        setMessages(latest.messages);setAttempts(latest.attempts);
        if(!markBusyRef.current)setAnnotations(latest.annotations);
        setSession(current=>current?.id===latest.id?{...current,board:latest.board,sealed_exam:latest.sealed_exam}:current);
      }catch{/* Keep local work; reconnect/focus retries via the shared invalidation poll. */}
      finally{reading=false;}
    };
    window.addEventListener(OWNER_SYNC_CHANGED,refresh);
    return()=>{active=false;window.removeEventListener(OWNER_SYNC_CHANGED,refresh);};
  },[session?.id]);

  const codeDraftKey=()=>codeConflictRef.current??codeRef.current;
  useEffect(()=>registerDraftSaveState(()=>{
    const dirty=codeRef.current!==savedCodeRef.current?.content||codeConflictRef.current!==null;
    const local=readLocalDraft<string>("practice:"+codeSessionIdRef.current,"code");
    return {dirty,saving:Boolean(codeSavePromiseRef.current||codeFlushPromiseRef.current),conflict:codeConflictRef.current!==null,
      offline:typeof navigator!=="undefined"&&!navigator.onLine,localRecoverable:!dirty||(isLocalDraftDurable("practice:"+codeSessionIdRef.current,"code")&&local?.content===codeRef.current)};
  },"practice:"+session?.id+":code",codeDraftKey),[session?.id]);

  useEffect(()=>{
    if(!session)return;
    const scope="practice:"+session.id;
    const unstate=registerDraftSaveState(()=>({dirty:Boolean(pendingMarkRef.current||readLocalDraft(scope,"mark")),saving:markBusyRef.current,
      conflict:false,offline:typeof navigator!=="undefined"&&!navigator.onLine,
      localRecoverable:!pendingMarkRef.current||(isLocalDraftDurable(scope,"mark")&&readLocalDraft<Annotation>(scope,"mark")?.content.id===pendingMarkRef.current.id)}),scope+":mark");
    const unflush=registerDraftFlush(async()=>{
      const local=readLocalDraft<Annotation>(scope,"mark")?.content??pendingMarkRef.current;
      if(local&&!session.sealed_exam)await persistMark(local);
      if(pendingMarkRef.current||readLocalDraft(scope,"mark"))throw new Error("Пометка ещё не подтверждена сервером.");
    },scope+":mark");
    return()=>{unstate();unflush();};
  },[session?.id,session?.sealed_exam]);

  function persistCurrentCode(sessionId: string, content: string): Promise<CodeDocument> {
    const previous = codeSavePromiseRef.current;
    const operation = (previous ? previous.catch(() => undefined) : Promise.resolve()).then(async () => {
      if(codeConflictRef.current!==null)throw new ApiError("DRAFT_CONFLICT","Выберите, какой черновик продолжить.",409,{});
      let saved:CodeDocument;
      try { saved = await saveCode(sessionId, content, codeVersionRef.current); }
      catch(reason){
        if(reason instanceof ApiError&&reason.code==="VERSION_CONFLICT"&&codeSessionIdRef.current===sessionId){
          const latest=await getSolveSession(sessionId);
          savedCodeRef.current=latest.code;codeVersionRef.current=latest.code.version;
          codeConflictRef.current=codeRef.current;
          if(mountedRef.current){setCodeConflict(codeRef.current);setCodeVersion(latest.code.version);setSession(current=>current?.id===sessionId?{...current,code:latest.code}:current);}
        }
        throw reason;
      }
      if (saved.content !== content) {
        throw new ApiError("CODE_CONTENT_MISMATCH", "Не удалось подтвердить сохранение кода. Повторите попытку.", 200, { session_id: sessionId });
      }
      if (codeSessionIdRef.current === sessionId) {
        savedCodeRef.current = saved;
        codeVersionRef.current = saved.version;
        if(codeRef.current===saved.content)clearLocalDraft("practice:"+sessionId,"code");
        else saveLocalDraft("practice:"+sessionId,"code",saved.version,codeRef.current);
        if (mountedRef.current) {
          setCodeVersion(saved.version);
          setSession((current) => (current?.id === sessionId ? { ...current, code: saved } : current));
        }
      }
      return saved;
    });
    codeSavePromiseRef.current = operation;
    void operation.finally(() => {
      if (codeSavePromiseRef.current === operation) codeSavePromiseRef.current = null;
    }).catch(() => undefined);
    return operation;
  }

  async function ensureCurrentCodeSaved(sessionId: string): Promise<CodeDocument> {
    if (codeSessionIdRef.current !== sessionId) {
      throw new ApiError("SESSION_CHANGED", "Рабочая сессия изменилась; запуск отменён.", 409, { session_id: sessionId });
    }
    setCodeSaving(true);
    while (true) {
      const pending = codeSavePromiseRef.current;
      let saved = savedCodeRef.current;
      if (pending) saved = await pending;
      const latestContent = codeRef.current;
      if (!saved || saved.content !== latestContent) {
        saved = await persistCurrentCode(sessionId, latestContent);
      }
      if (codeSessionIdRef.current !== sessionId) {
        throw new ApiError("SESSION_CHANGED", "Рабочая сессия изменилась; запуск отменён.", 409, { session_id: sessionId });
      }
      if (codeRef.current === saved.content) {
        setCodeSaving(false);
        return saved;
      }
    }
  }

  function flushCurrentCode(): Promise<CodeDocument | null> {
    const ongoing = codeFlushPromiseRef.current;
    if (ongoing) return ongoing;
    const sessionId = codeSessionIdRef.current;
    if (!sessionId) return Promise.resolve(null);
    if(codeConflictRef.current!==null)return Promise.reject(new ApiError("DRAFT_CONFLICT","Выберите, какой черновик продолжить.",409,{}));
    if (codeSaveTimer.current) {
      clearTimeout(codeSaveTimer.current);
      codeSaveTimer.current = null;
    }
    const flush = (async () => {
      const pending = codeSavePromiseRef.current;
      if (pending) await pending;
      const latestContent = codeRef.current;
      const saved = savedCodeRef.current;
      if (saved && saved.content === latestContent) return saved;
      if (mountedRef.current) {
        setCodeSaving(true);
        setCodeError(null);
      }
      try {
        const confirmed = await persistCurrentCode(sessionId, latestContent);
        if (mountedRef.current && codeSessionIdRef.current === sessionId && codeRef.current === confirmed.content) {
          setCodeSaving(false);
        }
        return confirmed;
      } catch (reason: unknown) {
        if (mountedRef.current && codeSessionIdRef.current === sessionId) {
          setCodeSaving(false);
          setCodeError(codeSaveErrorMessage(reason));
        }
        throw reason;
      }
    })();
    codeFlushPromiseRef.current = flush;
    void flush.finally(() => {
      if (codeFlushPromiseRef.current === flush) codeFlushPromiseRef.current = null;
    }).catch(() => undefined);
    return flush;
  }

  useEffect(() => {
    if (initialCode.current) {
      initialCode.current = false;
      return;
    }
    if (!session || code === session.code.content || codeConflictRef.current!==null || session.sealed_exam) return;
    saveLocalDraft("practice:"+session.id,"code",codeVersionRef.current,code);
    if (codeSaveTimer.current) clearTimeout(codeSaveTimer.current);
    setCodeSaving(true);
    setCodeError(null);
    const contentAtSchedule = code;
    const sessionId = session.id;
    codeSaveTimer.current = setTimeout(() => {
      codeSaveTimer.current = null;
      void persistCurrentCode(sessionId, contentAtSchedule)
        .then((saved) => {
          if (mountedRef.current && codeSessionIdRef.current === sessionId && codeRef.current === saved.content) setCodeSaving(false);
        })
        .catch((reason: unknown) => {
          if (mountedRef.current && codeSessionIdRef.current === sessionId && codeRef.current === contentAtSchedule) setCodeSaving(false);
          if (mountedRef.current) setCodeError(codeSaveErrorMessage(reason));
        });
    }, 700);
    return () => {
      if (codeSaveTimer.current) clearTimeout(codeSaveTimer.current);
    };
  }, [code, session, codeConflict]);

  useEffect(() => {
    return registerDraftFlush(flushCurrentCode,"practice:"+session?.id+":code",codeDraftKey);
  }, [session?.id]);

  useEffect(() => {
    function flushWhenHidden() {
      if (document.visibilityState === "hidden") void flushCurrentCode().catch(() => undefined);
    }
    document.addEventListener("visibilitychange", flushWhenHidden);
    return () => {
      document.removeEventListener("visibilitychange", flushWhenHidden);
      void flushCurrentCode().catch(() => undefined);
    };
  }, [session?.id]);

  const task = session?.task ?? (DEMO_MODE ? DEMO_TASK : null);
  const statementText = useMemo(
    () => (task ? (task.revision.statement_text ?? statementToText(task.revision.statement_html)) : ""),
    [task],
  );
  const runnerStatus = { available: devicePythonEnabled, reason: devicePython.fileError };
  const assetFetchAvailable = !DEMO_MODE && (capabilities?.asset_fetch ?? DEMO_CAPABILITIES.asset_fetch).available;

  useEffect(() => {
    if (!session || !devicePythonEnabled) {
      setRun(null);
      return;
    }
    setRun(readDevicePythonRuns(devicePythonScope)[0] ?? null);
  }, [devicePythonEnabled, devicePythonScope, session?.id]);

  async function handleRun() {
    if (!session) return;
    if (devicePython.running) {
      devicePython.stop();
      return;
    }
    if (practiceRunRef.current?.scope === devicePythonScope) return;
    const request = {scope: devicePythonScope};
    practiceRunRef.current = request;
    const isCurrent = () => mountedRef.current && practiceRunRef.current === request && devicePythonScopeRef.current === request.scope;
    setCodeError(null);
    try {
      const saved = await ensureCurrentCodeSaved(session.id);
      if (!isCurrent()) return;
      if (codeRef.current !== saved.content) {
        setCodeSaving(true);
        setCodeError("Код изменился во время сохранения; запуск отменён.");
        return;
      }
      if (!devicePythonEnabled) {
        setCodeError("Локальный запуск недоступен в запечатанном режиме.");
        setTab("code");
        return;
      }
      if (devicePython.filesLoading) {
        setCodeError("Подготавливаем файлы задания. Повторите запуск через несколько секунд.");
        setTab("code");
        return;
      }
      if (devicePython.fileError) {
        setCodeError(devicePython.fileError);
        setTab("code");
        return;
      }
      setRun(null);
      const result = await devicePython.run(saved.content, saved.version);
      if (isCurrent()) setRun(result);
    } catch (reason: unknown) {
      if (isCurrent()) {
        setRun(null);
        setCodeError(codeSaveErrorMessage(reason));
        setTab("code");
      }
    } finally {
      if (practiceRunRef.current === request) practiceRunRef.current = null;
    }
  }

  function handleMessage(message: Message) {
    setMessages((current) => [...current, message]);
  }

  async function handlePrepareAsset(assetId: string) {
    if (!session || assetBusyId) return;
    setAssetBusyId(assetId);
    setAssetError(null);
    try {
      const prepared = await prepareAsset(assetId);
      handleAssetPrepared(prepared);
    } catch (reason: unknown) {
      setAssetError(readableError(reason, "Файл пока недоступен. Проверьте подключение файлов в настройках."));
    } finally {
      setAssetBusyId(null);
    }
  }

  async function handleAttachLocalFile(file: File) {
    if (!session || session.sealed_exam || localFileBusy) return;
    setLocalFileBusy(true);
    setLocalFileError(null);
    try {
      const next = await readLocalDeviceFile(file);
      const name = next.name.toLowerCase();
      if (localFiles.length >= MAX_DEVICE_FILES) throw new Error("Слишком много локальных файлов. Удалите один файл и повторите.");
      if (localFiles.some((item) => item.name.toLowerCase() === name)) throw new Error("Этот локальный файл уже прикреплён. Удалите его или выберите другое имя.");
      const total = localFiles.reduce((sum, item) => sum + item.bytes.byteLength, 0) + next.bytes.byteLength;
      if (total > MAX_FILE_BYTES) throw new Error("Локальные файлы превышают общий лимит 64 МБ. Удалите один файл и повторите.");
      setLocalFiles((current) => [...current, next]);
    } catch (reason: unknown) {
      setLocalFileError(reason instanceof Error ? reason.message : "Не удалось прикрепить локальный файл. Выберите его ещё раз.");
    } finally {
      setLocalFileBusy(false);
    }
  }

  function handleRemoveLocalFile(file: DeviceFile) {
    setLocalFiles((current) => current.filter((item) => item.sha256 !== file.sha256 || item.name !== file.name));
    setLocalFileError(null);
  }

  function handleClearLocalFiles() {
    setLocalFiles([]);
    setLocalFileError(null);
  }

  function handleAssetPrepared(prepared: AssetRef) {
    setSession((current) => {
      if (!current) return current;
      return {
        ...current,
        task: {
          ...current.task,
          assets: current.task.assets.map((asset) => asset.id === prepared.id ? { ...asset, ...prepared } : asset),
        },
      };
    });
  }

  async function handleMark(selection: { start: number; end: number; text: string }) {
    if (!task || !session || session.sealed_exam || pendingMark || markBusyRef.current) return;
    const next: Annotation = {
      id: "annotation-" + crypto.randomUUID(),
      kind: "highlight",
      text: selection.text,
      color: "#fef08a",
      anchor: {
        start: selection.start,
        end: selection.end,
        content_hash: task.revision.content_hash,
      },
    };
    setAnnotations((current) => [...current, next]);
    pendingMarkRef.current=next;setPendingMark(next);
    setSelectionMessage(null);
    void persistMark(next);
  }

  async function persistMark(next: Annotation) {
    if (!session || session.sealed_exam || markBusyRef.current) return;
    saveLocalDraft("practice:"+session.id,"mark",0,next);
    markBusyRef.current=true;setMarkBusy(true);setMarkError(null);
    try {
      const saved=await createAnnotation(session.id,next);
      clearLocalDraft("practice:"+session.id,"mark");
      pendingMarkRef.current=null;
      if(mountedRef.current){
        setAnnotations(current=>[...current.filter(item=>item.id!==next.id&&item.id!==saved.id),saved]);
        pendingMarkRef.current=null;setPendingMark(null);
      }
    } catch(reason:unknown){
      // An uncertain POST may already be committed. Keep the same stroke/key
      // for reconciliation; removing only the optimistic copy would lie.
      if(mountedRef.current)setMarkError(reason instanceof Error?reason.message:"Пометка пока не подтверждена. Повторите сохранение.");
    } finally {markBusyRef.current=false;if(mountedRef.current)setMarkBusy(false);}
  }

  function changeTab(next:WorkspaceTab){setTab(next);if(session){try{sessionStorage.setItem("kontur:surface:"+session.id,next);}catch{}}}
  const assistantActive = shell.panelVisible && shell.panelMode === "assistant";
  function toggleAssistant(){
    if(assistantActive){shell.hidePanel();setFocusedPane("split");requestAnimationFrame(()=>taskModeRef.current?.focus());}
    else {setMobilePane("task");setFocusedPane("split");shell.showPanel("assistant");}
  }
  function toggleWorkspaceFocus(){if(assistantActive)shell.hidePanel();setFocusedPane("split");shell.toggleFocus();}
  function togglePane(pane:"task"|"tools"){if(assistantActive&&pane==="tools")shell.hidePanel();setFocusedPane(value=>value===pane?"split":pane);}
  function askFragment(text:string){if(!text.trim())return;setQuestionDraft({text:"Помоги разобраться в этом фрагменте:\n"+text,key:Date.now()});setMobilePane("task");setFocusedPane("split");shell.showPanel("assistant");}
  const position=task?sequencePosition(sequence,task.revision.id):-1;
  async function moveTask(direction:number){
   if(!session||!sequence||position<0||navigating)return;
   let nextSequence=sequence;const nextIndex=position+direction;
   if(nextIndex<0)return;setNavigating(true);setNavigationError(null);
   try{
    await flushOpenDrafts();
    if(nextIndex>=nextSequence.revisionIds.length&&nextSequence.nextCursor){
     const page=await getTaskCatalog({...nextSequence.query,cursor:nextSequence.nextCursor});
     nextSequence={...nextSequence,revisionIds:[...nextSequence.revisionIds,...page.items.map(item=>item.revision.id)],nextCursor:page.next_cursor,total:page.total};
     rememberCatalogSequence(nextSequence);setSequence(nextSequence);
    }
    const revision=nextSequence.revisionIds[nextIndex];if(!revision)return;
    const progress=await getTaskProgress([revision]);
    router.push(progress[0]?.solve_session_id?"/workspace?session="+encodeURIComponent(progress[0].solve_session_id):"/workspace?revision="+encodeURIComponent(revision));
   }catch(reason){setNavigationError(readableError(reason,"Не удалось перейти к следующему заданию. Работа осталась открытой."));}
   finally{setNavigating(false);}
  }
  async function toggleBookmark(){
   if(!task||bookmarkBusy)return;setBookmarkBusy(true);
   try{await setBookmark(task.id,task.revision.id,!bookmarked);setBookmarked(value=>!value);}
   catch(reason){setNavigationError(readableError(reason,"Не удалось изменить закладку."));}finally{setBookmarkBusy(false);}
  }
  useEffect(()=>{
    if(!assistantActive)return;
    const close=(event:KeyboardEvent)=>{if(event.key!=="Escape")return;event.preventDefault();shell.hidePanel();setFocusedPane("split");requestAnimationFrame(()=>taskModeRef.current?.focus());};
    document.addEventListener("keydown",close,true);
    return()=>document.removeEventListener("keydown",close,true);
  },[assistantActive,shell]);
  useShortcuts(tab==="code"?"editor":"workspace",{
   "assistant.toggle":toggleAssistant,"focus.toggle":toggleWorkspaceFocus,
   "surface.toggle":()=>changeTab(tab==="board"?"code":"board"),
   "task.previous":()=>void moveTask(-1),"task.next":()=>void moveTask(1),
   "task.bookmark":()=>void toggleBookmark(),...(tab==="code"?{"code.run":()=>void handleRun()}:{}),
  });
  if (chatOnly && conversationId) return <ChatView initialConversationId={conversationId} />;

  if (loadError && !session) {
    return (
      <main className="page-state">
        <div className="state-icon state-icon-error">
          <Icon name="warning" size={24} />
        </div>
        <h1>Рабочая сессия недоступна</h1>
        <p>{loadError}</p>
        <Link className="outline-button" href="/tasks">Выбрать задание</Link>
      </main>
    );
  }

  if (!task || !session) {
    return (
      <main className="page-state">
        <span className="button-spinner large" />
        <p>Загружаем рабочую сессию…</p>
      </main>
    );
  }

  const assistantPanel = (
    <section className="kontur-assistant workspace-task-assistant" aria-labelledby="workspace-assistant-title" hidden={!assistantActive} aria-hidden={!assistantActive}>
      <header className="kontur-assistant-header"><strong id="workspace-assistant-title">Помощник</strong><button className="icon-button" aria-label="Проекты и чаты" title="Проекты и чаты" onClick={()=>shell.showPanel("navigation")} type="button"><Icon name="folder" size={18}/></button><button className="icon-button" aria-label="Закрыть помощника" onClick={toggleAssistant} type="button"><Icon name="close" size={18}/></button></header>
      <div className="kontur-ai-context"><span>Тип {task.line_number} · {task.title}</span><span>Условие и текущий код учитываются при отправке.</span></div>
      <div className="kontur-chat-scroll">{messages.length>0&&<ConversationFeed messages={messages}/>} {!messages.length&&<div className="kontur-ai-empty"><h2>Разберёмся вместе</h2><p>Спросите о задаче или выделите фрагмент кода.</p></div>}</div>
      <Composer syncContext={{kind:"composer",solve_session_id:session.id,conversation_id:session.conversation_id??undefined,task_id:session.task.id,task_revision_id:session.task.revision.id,content_hash:session.task.revision.content_hash}} conversationId={session.conversation_id??undefined} sealed={session.sealed_exam} sessionId={session.id} onMessage={handleMessage} beforeSend={flushOpenDrafts} draft={questionDraft}/>
    </section>
  );

  return (
    <main className="workspace-page">
      {shell.toolbarHost&&createPortal(<div className="kontur-workspace-actions">
       <Link href="/tasks" className="outline-button kontur-back-catalog" title="Вернуться к выборке" aria-label="Вернуться к выборке"><Icon name="chevron-left" size={18}/><span>Каталог</span></Link>
       <span className="kontur-position">{position>=0?`${position+1} / ${sequence?.total??sequence?.revisionIds.length}`:"Задание"}</span>
       <button className="icon-button" onClick={()=>void moveTask(-1)} disabled={navigating||position<=0} aria-label="Предыдущее задание" type="button"><Icon name="chevron-left" size={18}/></button>
       <button className="icon-button" onClick={()=>void moveTask(1)} disabled={navigating||position<0||(!sequence?.nextCursor&&position>=((sequence?.revisionIds.length??0)-1))} aria-label="Следующее задание" type="button"><Icon name="chevron-right" size={18}/></button>
      </div>,shell.toolbarHost)}
      {navigationError&&<p role="alert" className="panel-error">{navigationError}</p>}
      <div className="workspace-mobile-switch" aria-label="Область рабочего места">
        <button aria-pressed={mobilePane === "task"} onClick={() => setMobilePane("task")} type="button">Условие</button>
        <button aria-pressed={mobilePane === "tools"} onClick={() => setMobilePane("tools")} type="button">Решение</button>
        <button aria-pressed={shell.panelVisible&&shell.panelMode==="assistant"} onClick={toggleAssistant} type="button">Помощник</button>
      </div>
      <ResizableWorkspace focusedPane={focusedPane} mobilePane={mobilePane} assistantMode={assistantActive} assistantWidth={preferences.assistantWidth}>
        <section className="task-column">
          <header className="workspace-pane-header task-pane-header">
            <div className="task-mode-switch" role="tablist" aria-label="Режим правой панели">
              <button id="workspace-task-tab" ref={taskModeRef} aria-selected={!assistantActive} aria-controls="workspace-task-mode" className={"task-mode-button" + (!assistantActive ? " is-active" : "")} onClick={()=>{if(assistantActive)toggleAssistant();}} role="tab" tabIndex={!assistantActive ? 0 : -1} type="button">Задание</button>
              <button id="workspace-assistant-tab" aria-selected={assistantActive} aria-controls="workspace-assistant-mode" className={"task-mode-button" + (assistantActive ? " is-active" : "")} onClick={toggleAssistant} role="tab" tabIndex={assistantActive ? 0 : -1} type="button">Помощник</button>
            </div>
            <span className="workspace-task-meta">Тип {task.line_number}<span className="breadcrumb-dot">·</span>{task.demo?"Демонстрация":task.title}</span>
            <button type="button" className="icon-button" disabled={bookmarkBusy} aria-pressed={bookmarked} aria-label={bookmarked?"Убрать из закладок":"Добавить в закладки"} onClick={()=>void toggleBookmark()}><Icon name="bookmark" size={18}/></button>
            <button aria-label={focusedPane === "task" ? "Показать инструменты" : assistantActive ? "Развернуть помощника" : "Развернуть условие"} className="icon-button compact pane-expand" onClick={() => togglePane("task")} title={focusedPane === "task" ? "Показать инструменты" : assistantActive ? "Развернуть помощника" : "Развернуть условие"} type="button"><Icon name="panel" size={17} /></button>
          </header>
          <div id="workspace-task-mode" className="task-mode-panel task-mode-task" role="tabpanel" aria-labelledby="workspace-task-tab" hidden={assistantActive} aria-hidden={assistantActive}>
          <div className="task-scroll">
          <TaskDocument
            recoveryScope={"practice:"+session.id}
            task={task}
            statementText={statementText}
            annotations={annotations}
            onMark={handleMark}
            markBusy={markBusy}
            markPending={pendingMark!==null}
            markError={markError}
            onRetryMark={()=>{if(pendingMark)void persistMark(pendingMark);}}
            onAsk={askFragment}
            disabled={session.sealed_exam}
            onCopyDiagram={()=>{const asset=task.assets.find(item=>["image/png","image/jpeg","image/gif","image/webp"].includes(item.media_type));if(asset){setBoardImport({assetId:asset.id,revisionHash:task.revision.content_hash,requestId:crypto.randomUUID()});changeTab("board");setMobilePane("tools");}}}
            onInk={async(annotation)=>{const saved=await createAnnotation(session.id,annotation);if(mountedRef.current)setAnnotations(current=>current.some(item=>item.id===saved.id)?current:[...current,saved]);return saved;}}
            onDeleteInk={async(annotationId)=>{await deleteAnnotation(session.id,annotationId);if(mountedRef.current)setAnnotations(current=>current.filter(item=>item.id!==annotationId));}}
            assetBusyId={assetBusyId}
            assetError={assetError}
            assetFetchAvailable={assetFetchAvailable}
            onPrepareAsset={handlePrepareAsset}
            onAssetPrepared={handleAssetPrepared}
            onSelectionMessage={setSelectionMessage}
            selectionMessage={selectionMessage}
          />
          <PracticeAnswer sessionId={session.id} task={session.task} attempts={attempts} sealed={session.sealed_exam} beforeCheck={flushOpenDrafts}/>
          </div>
          </div>
          <div id="workspace-assistant-mode" role="tabpanel" aria-labelledby="workspace-assistant-tab" hidden={!assistantActive} aria-hidden={!assistantActive}>
            {assistantPanel}
          </div>
        </section>
        <aside className="workspace-rail">
          <div className="rail-tabs" role="tablist" aria-label="Рабочие инструменты">
            {tabs.map((item) => (
              <button
                aria-selected={tab === item.key}
                aria-controls={"workspace-panel-" + item.key}
                id={"workspace-tab-" + item.key}
                className={"rail-tab" + (tab === item.key ? " is-active" : "")}
                key={item.key}
                onClick={() => changeTab(item.key)}
                onKeyDown={(event) => {
                  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                  event.preventDefault();
                  const index = tabs.findIndex((entry) => entry.key === tab);
                  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
                  changeTab(tabs[next].key);
                  document.getElementById("workspace-tab-" + tabs[next].key)?.focus();
                }}
                role="tab"
                tabIndex={tab === item.key ? 0 : -1}
                type="button"
              >
                <span>{item.label}</span>
              </button>
            ))}
            <span className="rail-tabs-spacer" />
            {tab==="code"&&devicePython.running ? <button className="primary-button kontur-run" onClick={devicePython.stop} title="Остановить выполнение" type="button"><Icon name="stop" size={16}/>Остановить</button> : null}
            {tab==="code"&&!devicePython.running ? <button className="primary-button kontur-run" onClick={()=>void handleRun()} disabled={!runnerStatus.available||devicePython.filesLoading||Boolean(devicePython.fileError)||!code.trim()||codeConflict!==null||session.sealed_exam} title={devicePython.filesLoading?"Подготавливаем файлы задания…":devicePython.fileError??"Запустить код"} type="button"><Icon name="play" size={16}/>Запустить</button> : null}
            <button aria-label={focusedPane === "tools" ? "Показать условие" : "Развернуть инструменты"} className="icon-button compact pane-expand" onClick={() => togglePane("tools")} title={focusedPane === "tools" ? "Показать условие" : "Развернуть инструменты"} type="button"><Icon name="panel" size={17} /></button>
          </div>
          <div className="workspace-tool-panel">
          <div className="workspace-tab-content" role="tabpanel" id="workspace-panel-code" aria-labelledby="workspace-tab-code" hidden={tab !== "code"}>
            <CodePanel
              code={code}
              codeDirty={code !== session.code.content}
              codeVersion={codeVersion}
              codeError={codeError}
              conflict={codeConflict}
              canRestore={!session.sealed_exam}
              readOnly={session.sealed_exam||codeConflict!==null}
              onResolveConflict={(keepLocal)=>{
                const content=keepLocal?codeConflictRef.current:savedCodeRef.current?.content;
                if(content==null)return;
                acceptDraftHandoff("practice:"+session.id+":code",codeDraftKey());
                archiveDraftConflict("practice:"+session.id,"code",codeConflictRef.current,savedCodeRef.current);
                codeConflictRef.current=null;setCodeConflict(null);setCodeError(null);codeRef.current=content;setCode(content);
                if(keepLocal)saveLocalDraft("practice:"+session.id,"code",codeVersionRef.current,content);
                else clearLocalDraft("practice:"+session.id,"code");
              }}
              codeSaving={codeSaving}
              onChange={(value) => {
                if(codeConflictRef.current!==null)return;
                saveLocalDraft("practice:"+session.id,"code",codeVersionRef.current,value);
                codeRef.current = value;
                setCode(value);
              }}
              onFlush={flushCurrentCode}
              onRun={handleRun}
              run={run}
              runnerStatus={runnerStatus}
              onSelection={askFragment}
              draftKey={session.id}
              editorTheme={preferences.editorTheme}
              editorFontSize={preferences.editorFontSize}
              editorWrap={preferences.editorWrap}
              editorTabSize={preferences.editorTabSize}
              editorLineNumbers={preferences.editorLineNumbers}
              outputHeight={preferences.outputHeight}
            />
          </div>
          <div className="workspace-tab-content" role="tabpanel" id="workspace-panel-files" aria-labelledby="workspace-tab-files" hidden={tab !== "files"}>
            <FilesPanel assetBusyId={assetBusyId} assetError={assetError} localFileBusy={localFileBusy} localFileError={localFileError} localFiles={localFiles} onAttachLocalFile={handleAttachLocalFile} onClearLocalFiles={handleClearLocalFiles} onPrepareAsset={handlePrepareAsset} onRemoveLocalFile={handleRemoveLocalFile} onPrepared={handleAssetPrepared} pythonFileError={devicePython.fileError} disabled={session.sealed_exam} task={task} />
          </div>
          <div className="workspace-tab-content" role="tabpanel" id="workspace-panel-board" aria-labelledby="workspace-tab-board" hidden={tab !== "board"}>
            <InfiniteBoard
              key={session.id}
              active={tab==="board"}
              disabled={session.sealed_exam}
              initialBoard={session.board}
              importImage={boardImport}
              sessionId={session.id}
            />
          </div>

          </div>
        </aside>
      </ResizableWorkspace>

    </main>
  );
}

function TaskDocument({
  recoveryScope,
  task,
  statementText,
  annotations,
  onMark,
  markBusy,
  markPending,
  markError,
  onRetryMark,
  onAsk,
  onInk,
  onDeleteInk,
  onCopyDiagram,
  disabled,
  assetBusyId,
  assetError,
  assetFetchAvailable,
  onPrepareAsset,
  onAssetPrepared,
  onSelectionMessage,
  selectionMessage,
}: {
  recoveryScope: string;
  task: SolveSession["task"];
  statementText: string;
  annotations: Annotation[];
  onMark: (selection: { start: number; end: number; text: string }) => void;
  markBusy:boolean;
  markPending:boolean;
  markError:string|null;
  onRetryMark:()=>void;
  onAsk:(text:string)=>void;
  onInk:(annotation:Annotation)=>Promise<Annotation>;
  onDeleteInk:(annotationId:string)=>Promise<void>;
  onCopyDiagram:()=>void;
  disabled:boolean;
  assetBusyId: string | null;
  assetError: string | null;
  assetFetchAvailable: boolean;
  onPrepareAsset: (assetId: string) => void;
  onAssetPrepared: (asset: AssetRef) => void;
  onSelectionMessage: (message: string | null) => void;
  selectionMessage: string | null;
}) {
  const statementRef = useRef<HTMLDivElement>(null);

  function markSelection() {
    if(disabled||markPending||markBusy)return;
    const root = statementRef.current;
    const selection = window.getSelection();
    if (!root || !selection || selection.rangeCount === 0 || selection.isCollapsed) {
      onSelectionMessage("Выделите фрагмент условия, затем нажмите «Маркер».");
      return;
    }
    const range = selection.getRangeAt(0);
    if (!root.contains(range.commonAncestorContainer)) {
      onSelectionMessage("Маркер работает только внутри текста условия.");
      return;
    }
    const before = document.createRange();
    before.selectNodeContents(root);
    before.setEnd(range.startContainer, range.startOffset);
    const rawStart = before.toString().length;
    const raw = range.toString();
    const leading = raw.search(/\S|$/);
    const trailing = raw.length - raw.replace(/\s+$/, "").length;
    const text = raw.trim();
    const start = rawStart + leading;
    const end = rawStart + raw.length - trailing;
    if (!text || start < 0 || end <= start || end > statementText.length) {
      onSelectionMessage("Не удалось определить диапазон выделения.");
      return;
    }
    onSelectionMessage(null);
    onMark({ start, end, text });
    selection.removeAllRanges();
  }

  return (
    <article className="task-document">
      <header className="task-header">
        <div>
          <div className="task-kicker">
            {task.difficulty === "hard" ? "Сложная задача" : task.difficulty === "easy" ? "Базовая задача" : "Практика"}
          </div>
          <h2>{task.title}</h2>
        </div>
        <button className="icon-button" aria-label="Выделить текст маркером" title="Выделить текст маркером" disabled={disabled||markBusy||markPending} onMouseDown={event=>event.preventDefault()} onClick={markSelection} type="button"><Icon name="highlighter" size={18}/></button>
        <button className="icon-button kontur-selection-ask" onMouseDown={e=>e.preventDefault()} onClick={()=>{const text=window.getSelection()?.toString();if(text)onAsk(text);else onSelectionMessage("Выделите фрагмент условия, который хотите обсудить.");}} disabled={disabled} title="Спросить по выделению" aria-label="Спросить по выделению" type="button"><Icon name="help" size={18}/></button>
      </header>
      <StatementCanvas recoveryScope={recoveryScope} annotations={annotations} revisionHash={task.revision.content_hash} onInk={onInk} onDeleteInk={onDeleteInk} disabled={disabled}><div className="statement-body" ref={statementRef}>
        <HighlightedStatement text={statementText} annotations={annotations} contentHash={task.revision.content_hash} />
      </div>
      <TaskImages assets={task.assets} canPrepare={assetFetchAvailable && !task.demo} onAssetPrepared={onAssetPrepared} /></StatementCanvas>
      {task.assets.some(asset=>["image/png","image/jpeg","image/gif","image/webp"].includes(asset.media_type))&&<button type="button" className="outline-button" onClick={onCopyDiagram} disabled={disabled}><Icon name="board" size={16}/>Перенести схему на доску</button>}
      <div className="task-actions">
        {task.assets.map((asset) => <AssetRow asset={asset} busy={assetBusyId === asset.id} key={asset.id} onPrepare={onPrepareAsset} type9={task.line_number === 9} revisionHash={task.revision.content_hash} disabled={disabled} onPrepared={onAssetPrepared} />)}
      </div>
      {!task.demo&&<a className="task-source-link" href={task.source_url} rel="noreferrer" target="_blank">Оригинал на КЕГЭ <Icon name="external" size={13}/></a>}
      {assetError ? <p className="inline-note asset-error" role="alert"><Icon name="warning" size={14} />{assetError}</p> : null}

      {markError ? <div className="inline-error" role="alert"><span>{markError}</span><button className="outline-button" disabled={disabled||markBusy} onClick={onRetryMark} type="button">Повторить сохранение выделения</button></div> : null}
      {selectionMessage ? <p className="inline-note">{selectionMessage}</p> : null}
    </article>
  );
}

function AssetRow({ asset, type9, disabled, revisionHash }: { asset: AssetRef; busy: boolean; onPrepare: (assetId: string) => void; type9?: boolean; disabled?: boolean; revisionHash: string; onPrepared: (asset: AssetRef) => void }) {
  const [pathCopied, setPathCopied] = useState(false);
  const inputPath = asset.name;
  async function copyInputPath() {
    try {
      await navigator.clipboard.writeText(inputPath);
      setPathCopied(true);
    } catch { setPathCopied(false); }
  }
  return <div className="asset-file-group">
    <DeviceFileRow name={asset.name} sizeBytes={asset.size_bytes} disabled={disabled} onDownload={async () => downloadDeviceFile(await loadDeviceAsset(asset, revisionHash))} />
    {!(type9 && isSpreadsheetName(asset.name)) ? <button aria-label={`Скопировать имя ${inputPath}`} className="tiny-action asset-path-button" disabled={disabled} onClick={() => void copyInputPath()} type="button">{pathCopied ? "Скопировано" : "Скопировать имя"}</button> : null}
    {type9 && isSpreadsheetName(asset.name) ? <TaskSpreadsheetFiles asset={asset} disabled={disabled} revisionHash={revisionHash} /> : null}
  </div>;
}

function HighlightedStatement({ text, annotations, contentHash }: { text: string; annotations: Annotation[]; contentHash: string }) {
  const ranges = annotations
    .filter((item) => item.kind === "highlight" && item.anchor?.content_hash === contentHash && item.anchor.end > item.anchor.start)
    .map((item) => {
      const start=Math.max(0,item.anchor?.start??0),end=Math.min(text.length,item.anchor?.end??0);
      if(!item.text||text.slice(start,end)===item.text)return{start,end};
      const found=text.indexOf(item.text);
      return found>=0&&text.indexOf(item.text,found+1)<0?{start:found,end:found+item.text.length}:{start:0,end:0};
    })
    .filter((item) => item.end > item.start);
  if (!ranges.length) return <>{text}</>;
  const boundaries = Array.from(new Set([0, text.length, ...ranges.flatMap((item) => [item.start, item.end])])).sort((a, b) => a - b);
  return <>{boundaries.slice(0, -1).map((start, index) => { const end = boundaries[index + 1] ?? start; const marked = ranges.some((item) => item.start < end && item.end > start); return <span className={marked ? "statement-highlight" : undefined} key={`${start}-${end}`}>{text.slice(start, end)}</span>; })}</>;
}

function ConversationFeed({ messages }: { messages: Message[] }) {
  return (
    <section className="conversation-feed" aria-label="Разговор с репетитором">
      <h3>Разбор с наставником</h3>
      {!messages.length ? <p className="conversation-empty">С чего начнём? Расскажите, что уже получилось, или попросите подсказку в поле внизу.</p> : null}
      {messages.map((message) => (
        <div className={"message-row " + message.role} key={message.id}>
          <span className={message.role === "assistant" ? "assistant-avatar" : "learner-avatar"}>{message.role === "assistant" ? <Icon name="spark" size={18} /> : "Д"}</span>
          <div className="message-bubble">
            <div className="message-meta">
               {message.role === "assistant" ? "Наставник" : "Вы"}
               <time>{formatTime(message.created_at)}</time>
              {message.model ? <span className="message-model">{message.model}</span> : null}
            </div>
            <p>{message.content}</p>
          </div>
        </div>
      ))}
    </section>
  );
}

function codeSaveErrorMessage(reason: unknown): string {
  if (reason instanceof ApiError && reason.code === "VERSION_CONFLICT") {
    return "Сохранённый код изменился; сохранение и запуск отменены. Сравните черновик с сохранённой версией.";
  }
  return readableError(reason, "Не удалось сохранить код. Черновик остаётся на этом устройстве; повторите, когда соединение восстановится.");
}

function CodePanel({code,codeDirty,codeVersion,codeError,conflict,readOnly,canRestore,onResolveConflict,onChange,onFlush,run,runnerStatus,onSelection,draftKey,editorTheme,editorFontSize,editorWrap,editorTabSize,editorLineNumbers,outputHeight}:{code:string;codeDirty:boolean;codeVersion:number;codeSaving:boolean;codeError:string|null;conflict:string|null;readOnly:boolean;canRestore:boolean;onResolveConflict:(keepLocal:boolean)=>void;onChange:(value:string)=>void;onFlush:()=>Promise<CodeDocument|null>;onRun:()=>void;run:CodeRun|null;runnerStatus:{available:boolean;reason:string|null};onSelection:(text:string)=>void;draftKey:string;editorTheme:"interface"|"light"|"charcoal";editorFontSize:12|14|16;editorWrap:boolean;editorTabSize:2|4|8;editorLineNumbers:boolean;outputHeight:number}){
 return <div className={"code-panel editor-theme-"+editorTheme}>
  <CodeEditor readOnly={readOnly} onBlur={()=>void onFlush().catch(()=>undefined)} onChange={onChange} value={code} draftKey={draftKey} onSelection={onSelection} fontSize={editorFontSize} wrap={editorWrap} tabSize={editorTabSize} lineNumbers={editorLineNumbers}/>
  {conflict!==null&&<div className="panel-error draft-conflict" role="alert"><p>Есть несохранённый черновик с этого устройства, а сохранённая работа уже изменилась. Выберите, что продолжить.</p><details><summary>Посмотреть мой черновик</summary><pre>{conflict||"Пустой код"}</pre></details><div>{canRestore?<button className="outline-button" type="button" onClick={()=>onResolveConflict(true)}>Продолжить мой черновик</button>:null}<button className="outline-button" type="button" onClick={()=>onResolveConflict(false)}>Оставить сохранённый</button></div></div>}
  {codeError&&conflict===null&&<div className="panel-error" role="alert"><Icon name="warning" size={14}/>{codeError}<button className="outline-button" type="button" onClick={()=>void onFlush().catch(()=>undefined)}>Повторить</button></div>}
  <OutputPanel run={run} stale={Boolean(run)&&(codeDirty||run?.code_version!==codeVersion)} runnerAvailable={runnerStatus.available} height={outputHeight}/>
 </div>;
}

function OutputPanel({ run, stale, runnerAvailable, height }: { run: CodeRun | null; stale:boolean;runnerAvailable: boolean; height: number }) {
  return (
    <div className={"output-panel" + (run?.status === "unavailable" ? " is-unavailable" : "")} style={{ "--output-height": `${height}px` } as CSSProperties}>
      <div className="output-header">
        <span>{stale?"Вывод · предыдущий запуск":"Вывод"}</span>
        {run ? (
          <span className={"output-status " + run.status}>
            <span className="status-dot" />
            {run.status === "completed" || run.status === "succeeded" ? "Выполнено" : run.status === "unavailable" ? "Недоступно" : run.status === "timed_out" ? "Лимит времени" : run.status === "queued" || run.status === "running" ? "Выполняется" : "Ошибка"}
          </span>
        ) : null}
      </div>
      {run?.status === "unavailable" ? (
        <p className="output-empty">Запуск сейчас недоступен. Код сохранён, вы можете продолжить работу.</p>
      ) : run ? (
        <pre className="output-content">{[run.stdout,run.stderr].filter(Boolean).join("\n") || "Программа завершилась без вывода."}</pre>
      ) : (
        <p className="output-empty">Здесь появится вывод вашей программы.</p>
      )}
    </div>
  );
}

function FilesPanel({ task, assetBusyId, assetError, localFileBusy, localFileError, localFiles, onAttachLocalFile, onClearLocalFiles, onPrepareAsset, onRemoveLocalFile, onPrepared, pythonFileError, disabled }: { task: SolveSession["task"]; assetBusyId: string | null; assetError: string | null; localFileBusy: boolean; localFileError: string | null; localFiles: DeviceFile[]; onAttachLocalFile: (file: File) => Promise<void>; onClearLocalFiles: () => void; onPrepareAsset: (assetId: string) => void; onRemoveLocalFile: (file: DeviceFile) => void; onPrepared: (asset: AssetRef) => void; pythonFileError: string | null; disabled?: boolean }) {
  const pickerRef = useRef<HTMLInputElement>(null);
  const taskNames = new Set(task.assets.map((asset) => asset.name.toLowerCase()));
  return (
    <div className="rail-content files-panel">
      <div className="rail-content-heading">
        <div>
          <span className="eyebrow">Вложения задачи</span>
          <h3>Файлы</h3>
        </div>
        <span className="file-count">{task.assets.length + localFiles.length}</span>
      </div>
      <p className="muted-copy">Используйте имя файла в коде. Кнопка скачивания нужна, только если хотите сохранить отдельную копию.</p>
      <div className="file-picker-actions">
        <input
          ref={pickerRef}
          aria-label="Выбрать локальный файл"
          className="files-picker-input"
          disabled={disabled || localFileBusy}
          onChange={(event) => {
            const file = event.currentTarget.files?.[0] ?? null;
            event.currentTarget.value = "";
            if (file) void onAttachLocalFile(file);
          }}
          type="file"
        />
        <button className="outline-button" disabled={disabled || localFileBusy} onClick={() => pickerRef.current?.click()} type="button"><Icon name="paperclip" size={15} />{localFileBusy ? "Читаем файл…" : "Прикрепить файл"}</button>
        {localFiles.length ? <button className="tiny-action" disabled={disabled || localFileBusy} onClick={onClearLocalFiles} type="button">Очистить локальные</button> : null}
      </div>
      <p className="muted-copy files-local-note">Файлы используются только для запуска кода и не отправляются помощнику. Выбранные локальные файлы доступны до закрытия рабочего места; для замены недоступного вложения выберите файл с тем же именем. Исходные вложения сохраняются.</p>
      {localFileError ? <div className="panel-error" role="alert"><Icon name="warning" size={14} />{localFileError}</div> : null}
      {pythonFileError ? <div className="panel-error" role="alert"><Icon name="warning" size={14} />{pythonFileError}</div> : null}
      <div className="file-list">
        {task.assets.map((asset) => <AssetRow asset={asset} busy={assetBusyId === asset.id} key={asset.id} onPrepare={onPrepareAsset} type9={task.line_number === 9} revisionHash={task.revision.content_hash} disabled={disabled} onPrepared={onPrepared} />)}
        {localFiles.map((file) => {
          const fallback = taskNames.has(file.name.toLowerCase());
          return <div className="local-file-group" key={`${file.name}:${file.sha256}`}>
            <DeviceFileRow file={file} disabled={disabled || localFileBusy} meta={`${fallback ? "Локальная копия" : "Локальный файл"} · ${formatBytes(file.bytes.byteLength)}`} onDownload={() => downloadDeviceFile(file)} />
            <button className="tiny-action local-file-remove" disabled={disabled || localFileBusy} onClick={() => onRemoveLocalFile(file)} type="button">Удалить</button>
          </div>;
        })}
      </div>
      {assetError ? <div className="panel-error" role="alert"><Icon name="warning" size={14} />{assetError}</div> : null}
    </div>
  );
}

function AttemptsPanel({
  runs,
  attempts,
  sessionId,
  disabled,
  onAttemptsChange,
}: {
  runs: CodeRun[];
  attempts: Attempt[];
  sessionId: string;
  disabled: boolean;
  onAttemptsChange: (items: Attempt[]) => void;
}) {
  const [status, setStatus] = useState<Attempt["status"]>("in_progress");
  const [answer, setAnswer] = useState("");
  const [verification, setVerification] = useState<"unknown" | "correct" | "incorrect">("unknown");
  const [independent, setIndependent] = useState(true);
  const [hintCount, setHintCount] = useState(0);
  const [errorType, setErrorType] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function recordAttempt() {
    if (disabled || saving) return;
    setSaving(true);
    setActionError(null);
    try {
      const created = await createAttempt(sessionId, {
        status,
        user_answer: answer.trim() || null,
        verified_correct: verification === "unknown" ? null : verification === "correct",
        verification_method: verification === "unknown" ? null : "self_reported",
        independent,
        hint_count: Math.max(0, Math.min(100, hintCount)),
        error_type: errorType.trim() || null,
        notes: notes.trim() || null,
      }, `attempt:${sessionId}:${Date.now()}`);
      onAttemptsChange([...attempts, created]);
      setAnswer("");
      setVerification("unknown");
      setIndependent(true);
      setHintCount(0);
      setErrorType("");
      setNotes("");
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "Попытка не сохранена");
    } finally {
      setSaving(false);
    }
  }

  async function completeAttempt(item: Attempt) {
    if (disabled || item.status !== "in_progress" || saving) return;
    setSaving(true);
    setActionError(null);
    try {
      const updated = await updateAttempt(sessionId, item.id, { status: "solved", verified_correct: true, verification_method: "self_reported" });
      onAttemptsChange(attempts.map((value) => value.id === updated.id ? updated : value));
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "Попытка не обновлена");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rail-content attempts-panel">
      <div className="rail-content-heading">
        <div>
          <span className="eyebrow">История</span>
          <h3>Попытки</h3>
        </div>
        <span className="file-count">{attempts.length}</span>
      </div>
      <section className="attempt-record-form" aria-label="Записать попытку">
        <div className="attempt-form-heading"><strong>Записать результат</strong><span>Отдельно от запуска кода</span></div>
        <textarea aria-label="Ответ ученика" className="attempt-answer-input" disabled={disabled || saving} onChange={(event) => setAnswer(event.target.value)} placeholder="Ваш ответ или краткая запись решения" rows={3} value={answer} />
        <div className="attempt-form-grid">
          <label><span>Статус</span><select disabled={disabled || saving} onChange={(event) => setStatus(event.target.value as Attempt["status"])} value={status}><option value="in_progress">В процессе</option><option value="solved">Решено</option><option value="failed">Не решено</option><option value="self_reported">Самоотчёт</option></select></label>
          <label><span>Проверка</span><select disabled={disabled || saving} onChange={(event) => setVerification(event.target.value as typeof verification)} value={verification}><option value="unknown">Не указана</option><option value="correct">Верно</option><option value="incorrect">Неверно</option></select></label>
          <label><span>Подсказок</span><input aria-label="Количество подсказок" disabled={disabled || saving} min={0} max={100} onChange={(event) => setHintCount(Number(event.target.value) || 0)} type="number" value={hintCount} /></label>
        </div>
        <label className="attempt-check"><input checked={independent} disabled={disabled || saving} onChange={(event) => setIndependent(event.target.checked)} type="checkbox" /><span>Самостоятельная попытка</span></label>
        <input aria-label="Тип ошибки" className="attempt-text-input" disabled={disabled || saving} onChange={(event) => setErrorType(event.target.value)} placeholder="Тип ошибки (если есть)" value={errorType} />
        <input aria-label="Заметка к попытке" className="attempt-text-input" disabled={disabled || saving} onChange={(event) => setNotes(event.target.value)} placeholder="Заметка" value={notes} />
        <button className="tiny-action" disabled={disabled || saving} onClick={() => void recordAttempt()} type="button">{saving ? "Сохраняем…" : "Сохранить попытку"}</button>
        {disabled ? <small className="attempt-disabled-note">Редактирование завершённого пробника недоступно.</small> : null}
        {actionError ? <small className="attempt-action-error">{actionError}</small> : null}
      </section>
      <div className="attempt-subsection"><div className="attempt-subsection-heading"><span>Попытки ученика</span><span>{attempts.length}</span></div>
      {!attempts.length ? <p className="muted-copy">Записей пока нет.</p> : null}
      <div className="attempt-list">
        {attempts.map((item, index) => (
          <div className="attempt-row learner-attempt" key={item.id}>
            <span className={"attempt-icon " + item.status}><Icon name={item.status === "solved" ? "check" : item.status === "in_progress" ? "target" : "warning"} size={14} /></span>
            <div>
              <strong>Попытка {index + 1} · {attemptStatusLabel(item.status)}</strong>
              <small>{formatTime(item.started_at)} · {item.independent ? "самостоятельно" : "с подсказкой"} · подсказок: {item.hint_count}</small>
              {item.error_type ? <small>Ошибка: {item.error_type}</small> : null}
              {item.user_answer ? <small className="attempt-answer">{item.user_answer}</small> : null}
            </div>
            {item.status === "in_progress" ? <button className="tiny-action" disabled={disabled || saving} onClick={() => void completeAttempt(item)} type="button">Решено</button> : null}
          </div>
        ))}
      </div></div>
      <div className="attempt-subsection"><div className="attempt-subsection-heading"><span>Запуски кода</span><span>{runs.length}</span></div>
      {!runs.length ? <p className="muted-copy">Запуски появятся после первой проверки кода.</p> : null}
      <div className="attempt-list">
        {runs.map((item, index) => (
          <div className="attempt-row" key={item.id}>
            <span className={"attempt-icon " + item.status}><Icon name={item.status === "completed" || item.status === "succeeded" ? "check" : "warning"} size={14} /></span>
            <div>
              <strong>Запуск {runs.length - index}</strong>
              <small>{formatTime(item.created_at)} · {runStatusLabel(item.status)}</small>
            </div>
            <span className="attempt-output">{item.stdout || "—"}</span>
          </div>
        ))}
      </div>
      </div>
    </div>
  );
}

function attemptStatusLabel(status: Attempt["status"]): string {
  if (status === "solved") return "решено";
  if (status === "failed") return "не решено";
  if (status === "self_reported") return "самоотчёт";
  return "в процессе";
}

function runStatusLabel(status: CodeRun["status"]): string {
  if (status === "completed" || status === "succeeded") return "выполнено";
  if (status === "unavailable") return "недоступно";
  if (status === "timed_out") return "лимит времени";
  if (status === "queued" || status === "running") return "выполняется";
  return "ошибка";
}

function formatBytes(bytes: number | null): string {
  if (bytes == null) return "Размер неизвестен";
  if (bytes < 1024) return bytes + " Б";
  return Math.round(bytes / 1024) + " КБ";
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
