"use client";
import {archiveDraftConflict} from "@/lib/local-drafts";

import type { FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent } from "react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import {Modal} from "./modal";
import { Icon, type IconName } from "@/components/icon";
import styles from "./infinite-board.module.css";
import { saveBoard } from "@/lib/api";
import { loadDeviceAssetById } from "@/lib/device-files";
import {
  getStrokeStyle,
  isBoardSpaceKey,
  strokeOpacity,
  strokeOpacityForPreference,
  type BoardTool,
} from "@/lib/board-tools";
import { drawSmoothRoundStroke } from "@/lib/stroke-smoothing";
import { hitTestInkStroke } from "@/lib/statement-ink";
import { registerDraftFlush, registerDraftSaveState, acceptDraftHandoff, type FlushOptions } from "@/lib/draft-lifecycle";
import { assessLocalDraft, clearLocalDraft, readLocalDraft, saveLocalDraft, isLocalDraftDurable } from "@/lib/local-drafts";
import { getPreferences, PREFERENCES_CHANGED, setPreferences } from "@/lib/kontur-preferences";
import { useShortcuts } from "@/lib/kontur-shortcuts";
import { ApiError } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";
import type { BoardDocument, BoardImage, BoardStroke, BoardText } from "@/lib/types";

type SaveState = "saved" | "saving" | "error";
type BoardDraftContent = Omit<BoardDocument, "version" | "updated_at">;
type DraftRecovery = { status: "restore" | "conflict"; content: BoardDraftContent };

const TOOLS: Array<{ key: BoardTool; label: string; icon: IconName }> = [
  { key: "pen", label: "Перо", icon: "pen" },
  { key: "marker", label: "Маркер", icon: "highlighter" },
  { key: "eraser", label: "Ластик", icon: "eraser" },
  { key: "line", label: "Линия", icon: "line" },
  { key: "rect", label: "Прямоугольник", icon: "stop" },
  { key: "ellipse", label: "Эллипс", icon: "circle" },
  { key: "text", label: "Текст", icon: "text" },
  { key: "pan", label: "Панорама", icon: "mouse-pointer" },
];

const ERASER_RADIUS = 22;

const PALETTE = [
  { value: "#242529", label: "Графит" },
  { value: "#2563eb", label: "Синий" },
  { value: "#dc2626", label: "Красный" },
  { value: "#16a34a", label: "Зелёный" },
  { value: "#f2c94c", label: "Жёлтый" },
  { value: "#a855f7", label: "Фиолетовый" },
] as const;

const BACKGROUNDS = [
  { value: "blank", label: "Чистый" },
  { value: "grid", label: "Клетка" },
  { value: "dots", label: "Точки" },
] as const;
type BoardBackground = (typeof BACKGROUNDS)[number]["value"];

function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return Boolean(element && typeof element.closest === "function" && element.closest("input, textarea, select, [contenteditable='true']"));
}

function cloneBoard(board: BoardDocument): BoardDocument {
  return JSON.parse(JSON.stringify(board)) as BoardDocument;
}

function boardContentKey(board: Pick<BoardDocument, "strokes" | "texts" | "images" | "viewport">): string {
  return JSON.stringify({ strokes: board.strokes, texts: board.texts, images: board.images ?? [], viewport: board.viewport });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isBoardPoint(value: unknown): value is { x: number; y: number } {
  return isRecord(value) && isFiniteNumber(value.x) && isFiniteNumber(value.y);
}

function isBoardDraftStroke(value: unknown): value is BoardStroke {
  if (!isRecord(value) || typeof value.id !== "string" || !Array.isArray(value.points) || typeof value.color !== "string" || !isFiniteNumber(value.width)) return false;
  if (value.kind !== undefined && !["freehand", "marker", "line", "rect", "ellipse"].includes(value.kind as string)) return false;
  if (value.opacity !== undefined && !isFiniteNumber(value.opacity)) return false;
  return value.points.every(isBoardPoint);
}

function isBoardDraftText(value: unknown): value is BoardText {
  return isRecord(value)
    && typeof value.id === "string"
    && isFiniteNumber(value.x)
    && isFiniteNumber(value.y)
    && typeof value.text === "string";
}

function isBoardDraftImage(value: unknown): value is BoardImage {
  return isRecord(value)
    && typeof value.id === "string"
    && typeof value.asset_id === "string"
    && typeof value.revision_hash === "string"
    && isFiniteNumber(value.x)
    && isFiniteNumber(value.y)
    && isFiniteNumber(value.width)
    && isFiniteNumber(value.height);
}

function isBoardDraftContent(value: unknown): value is BoardDraftContent {
  if (!isRecord(value) || typeof value.id !== "string" || !Array.isArray(value.strokes) || !Array.isArray(value.texts) || !Array.isArray(value.images) || !isRecord(value.viewport)) return false;
  return value.strokes.every(isBoardDraftStroke)
    && value.texts.every(isBoardDraftText)
    && value.images.every(isBoardDraftImage)
    && isFiniteNumber(value.viewport.x)
    && isFiniteNumber(value.viewport.y)
    && isFiniteNumber(value.viewport.zoom)
    && value.viewport.zoom > 0;
}

function boardDraftContent(board: BoardDocument): BoardDraftContent {
  const cloned = cloneBoard(board);
  return {
    id: cloned.id,
    strokes: cloned.strokes,
    texts: cloned.texts,
    images: cloned.images ?? [],
    viewport: cloned.viewport,
  };
}

function boardFromDraft(base: BoardDocument, content: BoardDraftContent): BoardDocument {
  const cloned = JSON.parse(JSON.stringify(content)) as BoardDraftContent;
  return { ...cloneBoard(base), ...cloned, id: base.id };
}

function boardImageKey(image: Pick<BoardImage, "asset_id" | "revision_hash">): string {
  return `${image.asset_id}:${image.revision_hash}`;
}

const BOARD_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

async function loadBoardImage(assetId: string, revisionHash: string, signal?: AbortSignal): Promise<{ image: HTMLImageElement; objectUrl: string }> {
  const file = await loadDeviceAssetById(assetId, revisionHash, signal);
  const mediaType = file.mediaType.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  if (!BOARD_IMAGE_TYPES.has(mediaType)) throw new Error("Сервер вернул файл неподдерживаемого типа.");
  signal?.throwIfAborted();
  const objectUrl = URL.createObjectURL(new Blob([new Uint8Array(file.bytes)], { type: mediaType }));
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const element = new Image();
      let settled = false;
      function finish(error?: unknown) {
        if (settled) return;
        settled = true;
        signal?.removeEventListener("abort", onAbort);
        if (error) reject(error); else resolve(element);
      }
      function onAbort() {
        finish(new DOMException("Загрузка отменена.", "AbortError"));
      }
      element.onload = () => finish();
      element.onerror = () => finish(new Error("Файл иллюстрации повреждён или недоступен."));
      signal?.addEventListener("abort", onAbort, { once: true });
      element.src = objectUrl;
      if (signal?.aborted) onAbort();
    });
    return { image, objectUrl };
  } catch (reason) {
    URL.revokeObjectURL(objectUrl);
    throw reason;
  }
}

function fitBoardImage(board: BoardDocument, naturalWidth: number, naturalHeight: number, stage: HTMLDivElement | null): Pick<BoardImage, "x" | "y" | "width" | "height"> {
  const rect = stage?.getBoundingClientRect();
  const zoom = Number.isFinite(board.viewport.zoom) && board.viewport.zoom > 0 ? board.viewport.zoom : 1;
  const screenWidth = Math.max(rect?.width ?? 0, 800);
  const screenHeight = Math.max(rect?.height ?? 0, 600);
  const worldWidth = screenWidth / zoom;
  const worldHeight = screenHeight / zoom;
  const padding = 36 / zoom;
  const availableWidth = Math.max(120 / zoom, worldWidth - padding * 2);
  const availableHeight = Math.max(120 / zoom, worldHeight - padding * 2);
  const aspect = naturalWidth > 0 && naturalHeight > 0 ? naturalWidth / naturalHeight : 1;
  const scale = Math.min(1, availableWidth / Math.max(1, naturalWidth), availableHeight / Math.max(1, naturalHeight));
  const width = Math.max(1, naturalWidth * scale);
  const height = Math.max(1, width / aspect);
  const centerX = (screenWidth / 2 - board.viewport.x) / zoom;
  const centerY = (screenHeight / 2 - board.viewport.y) / zoom;
  return { x: centerX - width / 2, y: centerY - height / 2, width, height };
}

export function InfiniteBoard({
  sessionId,
  initialBoard,
  disabled = false,
  active = true,
  persist: persistDocumentOverride,
  importImage,
  draftScope: draftScopeProp,
}: {
  sessionId: string;
  initialBoard: BoardDocument;
  disabled?: boolean;
  active?: boolean;
  persist?: (document: BoardDocument) => Promise<BoardDocument>;
  importImage?: { assetId: string; revisionHash: string; requestId: string };
  draftScope?: string;
}) {
  const draftScope = draftScopeProp?.trim() || sessionId;
  const [board, setBoard] = useState(() => cloneBoard(initialBoard));
  const [history, setHistory] = useState<BoardDocument[]>([]);
  const [redoHistory, setRedoHistory] = useState<BoardDocument[]>([]);
  const [tool, setTool] = useState<BoardTool>("pen");
  const [toolColors, setToolColors] = useState(() => ({ pen: getPreferences().boardColor, marker: getPreferences().boardMarkerColor }));
  const [toolWidths, setToolWidths] = useState(()=>({ pen: getPreferences().boardWidth, marker: getPreferences().boardMarkerWidth }));
  const [toolOpacities, setToolOpacities] = useState(()=>({ pen: getPreferences().boardOpacity, marker: getPreferences().boardMarkerOpacity }));
  const [drawingSmoothing, setDrawingSmoothing] = useState(() => getPreferences().drawingSmoothing);
  const previousDefaultsRef=useRef(getPreferences());
  const [boardBackground, setBoardBackground] = useState<BoardBackground>(() => getPreferences().boardBackground);
  const [colorOpen, setColorOpen] = useState(false);
  const [boardMenuOpen, setBoardMenuOpen] = useState(false);
  const chromeId = useId();
  const toolbarRef = useRef<HTMLDivElement>(null);
  const boardMenuRef = useRef<HTMLDivElement>(null);
  const brushButtonRef = useRef<HTMLButtonElement>(null);
  const boardMenuButtonRef = useRef<HTMLButtonElement>(null);
  const [boardFocused, setBoardFocused] = useState(false);
  const [imageBusy, setImageBusy] = useState(false);
  const [imageError, setImageError] = useState<string | null>(null);
  const [imageRetry, setImageRetry] = useState(0);
  const [imageRenderVersion, setImageRenderVersion] = useState(0);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [draftRecovery, setDraftRecovery] = useState<DraftRecovery | null>(null);
  const [textDraft, setTextDraft] = useState<{ x: number; y: number } | null>(null);
  const [textValue, setTextValue] = useState("");
  const disabledRef=useRef(disabled);disabledRef.current=disabled;
  const textDraftRef=useRef(textDraft);textDraftRef.current=textDraft;
  const textValueRef=useRef(textValue);textValueRef.current=textValue;
  const draftRecoveryRef=useRef(draftRecovery);draftRecoveryRef.current=draftRecovery;
  const textInProgressRef=useRef(false);textInProgressRef.current=Boolean(textDraft&&textValue);
  const boardDraftKey=()=>drawingRef.current||textInProgressRef.current?undefined:boardContentKey(draftRecoveryRef.current?.content??boardDraftContent(boardRef.current));
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const eraserCursorRef = useRef<HTMLDivElement>(null);
  const drawingRef = useRef<BoardStroke | null>(null);
  const drawingStartRef = useRef<{ x: number; y: number; straight: boolean } | null>(null);
  const erasingRef = useRef<{ before: BoardDocument; changed: boolean } | null>(null);
  const panRef = useRef<{ x: number; y: number; viewport: { x: number; y: number } } | null>(null);
  const temporaryPanRef = useRef<BoardTool | null>(null);
  const imageCacheRef = useRef<Map<string, { image: HTMLImageElement; objectUrl: string }>>(new Map());
  const imageRequestRef = useRef(0);
  const boardRef = useRef(board);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmedVersionRef = useRef(initialBoard.version);
  const confirmedContentKeyRef = useRef(boardContentKey(board));
  const queuedBoardRef = useRef<BoardDocument | null>(null);
  const savePromiseRef = useRef<Promise<BoardDocument> | null>(null);
  const flushPromiseRef = useRef<Promise<void> | null>(null);
  const mountedRef = useRef(false);
  const draftScopeRef = useRef<string | null>(null);
  const draftAssessmentReadyRef = useRef(false);
  const renderFrameRef = useRef<number | null>(null);
  const strokeCounterRef = useRef(0);

  useEffect(() => {
    boardRef.current = board;
  }, [board]);

  useEffect(() => {
    if (tool === "eraser" && !disabled && active && !draftRecovery) return;
    eraserCursorRef.current?.classList.remove(styles.eraserCursorVisible);
  }, [active, disabled, draftRecovery, tool]);

  useEffect(() => {
    if (draftScopeRef.current === draftScope) return;
    draftScopeRef.current = draftScope;
    draftAssessmentReadyRef.current = false;
    const draft = readLocalDraft<BoardDraftContent>(draftScope, "board");
    const validDraft = draft && isBoardDraftContent(draft.content) ? draft : null;
    const assessment = assessLocalDraft(validDraft, initialBoard.version, boardDraftContent(initialBoard));
    if (assessment.status === "confirmed") {
      clearLocalDraft(draftScope, "board");
      setDraftRecovery(null);
    } else if ((assessment.status === "restore" || assessment.status === "conflict") && assessment.content) {
      setDraftRecovery({ status: assessment.status, content: assessment.content });
    } else {
      setDraftRecovery(null);
    }
    draftAssessmentReadyRef.current = true;
  }, [draftScope, initialBoard]);

  useEffect(()=>{
    if(initialBoard.version<=confirmedVersionRef.current||savePromiseRef.current)return;
    const local=boardRef.current;
    const dirty=boardContentKey(local)!==confirmedContentKeyRef.current||Boolean(queuedBoardRef.current);
    if(dirty){saveLocalDraft(draftScope,"board",confirmedVersionRef.current,boardDraftContent(local));
      setDraftRecovery({status:"conflict",content:boardDraftContent(local)});}
    queuedBoardRef.current=null;
    if(saveTimerRef.current){clearTimeout(saveTimerRef.current);saveTimerRef.current=null;}
    confirmedVersionRef.current=initialBoard.version;confirmedContentKeyRef.current=boardContentKey(initialBoard);
    const next={...cloneBoard(initialBoard),viewport:local.viewport};confirmedContentKeyRef.current=boardContentKey(next);boardRef.current=next;setBoard(next);
    setSaveState("saved");setSaveError(null);
  },[initialBoard,draftScope]);

  useEffect(()=>registerDraftSaveState(()=>{
    const inProgress=Boolean(drawingRef.current)||textInProgressRef.current;
    const dirty=inProgress||boardContentKey(boardRef.current)!==confirmedContentKeyRef.current||Boolean(queuedBoardRef.current)||Boolean(draftRecoveryRef.current);
    const local=readLocalDraft<BoardDraftContent>(draftScope,"board");
    return {dirty,saving:Boolean(savePromiseRef.current||flushPromiseRef.current),conflict:draftRecoveryRef.current?.status==="conflict",
      offline:typeof navigator!=="undefined"&&!navigator.onLine,localRecoverable:!dirty||(!inProgress&&isLocalDraftDurable(draftScope,"board")&&Boolean(local)&&boardContentKey(local!.content)===boardContentKey(draftRecoveryRef.current?.content??boardDraftContent(boardRef.current)))};
  },draftScope+":board",boardDraftKey),[draftScope]);

  const saveDocument = useCallback(
    (document: BoardDocument) => persistDocumentOverride
      ? persistDocumentOverride(document)
      : saveBoard(sessionId, document),
    [persistDocumentOverride, sessionId],
  );

  useEffect(() => {
    const syncPreferences = () => {
      const preferences = getPreferences();
      const previous=previousDefaultsRef.current;
      if(preferences.boardBackground!==previous.boardBackground)setBoardBackground(preferences.boardBackground);
      if(preferences.boardColor!==previous.boardColor)setToolColors(current=>({...current,pen:preferences.boardColor}));
      if(preferences.boardMarkerColor!==previous.boardMarkerColor)setToolColors(current=>({...current,marker:preferences.boardMarkerColor}));
      if(preferences.boardWidth!==previous.boardWidth)setToolWidths(current=>({...current,pen:preferences.boardWidth}));
      if(preferences.boardMarkerWidth!==previous.boardMarkerWidth)setToolWidths(current=>({...current,marker:preferences.boardMarkerWidth}));
      if(preferences.boardOpacity!==previous.boardOpacity)setToolOpacities(current=>({...current,pen:preferences.boardOpacity}));
      if(preferences.boardMarkerOpacity!==previous.boardMarkerOpacity)setToolOpacities(current=>({...current,marker:preferences.boardMarkerOpacity}));
      if(preferences.drawingSmoothing!==previous.drawingSmoothing)setDrawingSmoothing(preferences.drawingSmoothing);
      previousDefaultsRef.current=preferences;
    };
    syncPreferences();
    window.addEventListener(PREFERENCES_CHANGED, syncPreferences);
    window.addEventListener("storage", syncPreferences);
    return () => {
      window.removeEventListener(PREFERENCES_CHANGED, syncPreferences);
      window.removeEventListener("storage", syncPreferences);
    };
  }, []);

  const render = useCallback(() => {
    const canvas = canvasRef.current;
    const stage = stageRef.current;
    if (!canvas || !stage) return;
    const rect = stage.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";
    const context = canvas.getContext("2d");
    if (!context) return;
    const computedStyle = window.getComputedStyle(stage);
    const surfaceColor = computedStyle.getPropertyValue("--board-paper").trim() || "#ffffff";
    const gridColor = computedStyle.getPropertyValue("--board-rule").trim() || "#d1d1d6";
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    context.fillStyle = surfaceColor;
    context.fillRect(0, 0, rect.width, rect.height);
    context.save();
    context.translate(board.viewport.x, board.viewport.y);
    context.scale(board.viewport.zoom, board.viewport.zoom);
    const gridStep = 28;
    const startX = Math.floor(-board.viewport.x / board.viewport.zoom / gridStep) * gridStep - gridStep;
    const startY = Math.floor(-board.viewport.y / board.viewport.zoom / gridStep) * gridStep - gridStep;
    const endX = (rect.width - board.viewport.x) / board.viewport.zoom + gridStep;
    const endY = (rect.height - board.viewport.y) / board.viewport.zoom + gridStep;
    if (boardBackground === "grid") {
      context.strokeStyle = gridColor;
      context.lineWidth = 1 / board.viewport.zoom;
      context.beginPath();
      for (let x = startX; x < endX; x += gridStep) {
        context.moveTo(x, startY);
        context.lineTo(x, endY);
      }
      for (let y = startY; y < endY; y += gridStep) {
        context.moveTo(startX, y);
        context.lineTo(endX, y);
      }
      context.stroke();
    } else if (boardBackground === "dots") {
      context.fillStyle = gridColor;
      for (let x = startX; x < endX; x += gridStep) {
        for (let y = startY; y < endY; y += gridStep) {
          context.beginPath();
          context.arc(x, y, 0.85 / board.viewport.zoom, 0, Math.PI * 2);
          context.fill();
        }
      }
    }
    context.globalAlpha = 1;
    (board.images ?? []).forEach((image) => {
      const cached = imageCacheRef.current.get(boardImageKey(image));
      if (!cached || image.width <= 0 || image.height <= 0) return;
      context.drawImage(cached.image, image.x, image.y, image.width, image.height);
    });
    context.lineCap = "round";
    context.lineJoin = "round";
    [...board.strokes, ...(drawingRef.current ? [drawingRef.current] : [])].forEach((stroke) => {
      if (stroke.points.length < 2) return;
      context.strokeStyle = stroke.color;
      context.globalAlpha = strokeOpacity(stroke.kind, stroke.opacity);
      context.lineWidth = stroke.width;
      context.beginPath();
      const start = stroke.points[0];
      const end = stroke.points[stroke.points.length - 1];
      if (!start || !end) return;
      if (stroke.kind === "line") {
        context.moveTo(start.x, start.y);
        context.lineTo(end.x, end.y);
      } else if (stroke.kind === "rect") {
        context.rect(start.x, start.y, end.x - start.x, end.y - start.y);
      } else if (stroke.kind === "ellipse") {
        context.ellipse(
          (start.x + end.x) / 2,
          (start.y + end.y) / 2,
          Math.abs(end.x - start.x) / 2,
          Math.abs(end.y - start.y) / 2,
          0,
          0,
          Math.PI * 2,
        );
      } else {
        drawSmoothRoundStroke(context, stroke.points, drawingSmoothing);
        return;
      }
      context.stroke();
    });
    context.restore();
  }, [board, boardBackground, drawingSmoothing, imageRenderVersion]);

  useLayoutEffect(() => {
    if (!active) return;
    render();
  }, [active, render]);

  const scheduleRender = useCallback(() => {
    if (renderFrameRef.current !== null) return;
    if (typeof requestAnimationFrame === "undefined") {
      render();
      return;
    }
    renderFrameRef.current = requestAnimationFrame(() => {
      renderFrameRef.current = null;
      render();
    });
  }, [render]);

  useEffect(() => {
    scheduleRender();
    const stage = stageRef.current;
    if (!stage) return;
    const observer = new ResizeObserver(scheduleRender);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [scheduleRender]);

  const publishBoard = useCallback((next: BoardDocument) => {
    boardRef.current = next;
    if (!disabled && draftAssessmentReadyRef.current) {
      saveLocalDraft(draftScope, "board", confirmedVersionRef.current, boardDraftContent(next));
    }
    setBoard(next);
  }, [disabled, draftScope]);

  const drainSaves = useCallback(() => {
    if (disabledRef.current || savePromiseRef.current || !queuedBoardRef.current) return;
    const queued = queuedBoardRef.current;
    queuedBoardRef.current = null;
    const payload = { ...cloneBoard(queued), version: confirmedVersionRef.current };
    const payloadKey = boardContentKey(payload);
    if (payloadKey === confirmedContentKeyRef.current) {
      const latestDraft = readLocalDraft<BoardDraftContent>(draftScope, "board");
      const latestContent = latestDraft && isBoardDraftContent(latestDraft.content) ? latestDraft.content : null;
      if (!latestDraft || (latestContent && boardContentKey(latestContent) === payloadKey)) {
        clearLocalDraft(draftScope, "board");
      } else if (latestContent && !queuedBoardRef.current) {
        queuedBoardRef.current = boardFromDraft(payload, latestContent);
      }
      if (mountedRef.current) setSaveState("saved");
      return;
    }
    if (mountedRef.current) {
      setSaveState("saving");
      setSaveError(null);
    }
    let succeeded = false;
    const operation = Promise.resolve()
      .then(() => saveDocument(payload))
      .then((saved) => {
        succeeded = true;
        confirmedVersionRef.current = saved.version;
        confirmedContentKeyRef.current = payloadKey;
        const current = boardRef.current;
        const currentKey = boardContentKey(current);
        const withConfirmedMeta = { ...current, version: saved.version, updated_at: saved.updated_at };
        boardRef.current = withConfirmedMeta;
        if (mountedRef.current) setBoard(withConfirmedMeta);
        if (mountedRef.current) {
          setSaveState("saved");
          setSaveError(null);
        }
        const latestDraft = readLocalDraft<BoardDraftContent>(draftScope, "board");
        const latestContent = latestDraft && isBoardDraftContent(latestDraft.content) ? latestDraft.content : null;
        const latestDraftKey = latestContent ? boardContentKey(latestContent) : null;
        if (latestContent && latestDraftKey !== payloadKey) {
          saveLocalDraft(draftScope, "board", saved.version, latestContent);
          if (!queuedBoardRef.current) queuedBoardRef.current = boardFromDraft(withConfirmedMeta, latestContent);
        } else if (currentKey !== payloadKey) {
          saveLocalDraft(draftScope, "board", saved.version, boardDraftContent(current));
          if (!queuedBoardRef.current) queuedBoardRef.current = cloneBoard(current);
        } else if (!latestDraft) {
          clearLocalDraft(draftScope, "board");
        }
        return saved;
      })
      .catch((reason: unknown) => {
        if (!queuedBoardRef.current && boardContentKey(boardRef.current) !== confirmedContentKeyRef.current) {
          queuedBoardRef.current = cloneBoard(boardRef.current);
        }
        if (mountedRef.current) {
          setSaveState("error");
          if (reason instanceof ApiError && (reason.status === 409 || reason.code === "STALE_VERSION")) {
            setSaveError("Конфликт версии: доска изменена в другом окне. Обновите сессию перед продолжением.");
          } else if (reason instanceof ApiError && (reason.status === 413 || reason.code === "BOARD_QUOTA_EXCEEDED")) {
            setSaveError("Лимит доски достигнут: удалите лишние штрихи или текст.");
          } else {
            setSaveError(readableError(reason, "Не удалось сохранить доску. Проверьте соединение и повторите попытку."));
          }
        }
        throw reason;
      })
      .finally(() => {
        if (savePromiseRef.current === operation) savePromiseRef.current = null;
        if (succeeded && queuedBoardRef.current) drainSaves();
      });
    savePromiseRef.current = operation;
    void operation.catch(() => undefined);
  }, [draftScope, saveDocument]);

  const persist = useCallback(
    (next: BoardDocument) => {
      if (disabledRef.current) return;
      boardRef.current = next;
      if (draftAssessmentReadyRef.current) {
        saveLocalDraft(draftScope, "board", confirmedVersionRef.current, boardDraftContent(next));
      }
      queuedBoardRef.current = cloneBoard(next);
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      if (mountedRef.current) {
        setSaveState("saving");
        setSaveError(null);
      }
      saveTimerRef.current = setTimeout(() => {
        saveTimerRef.current = null;
        drainSaves();
      }, 650);
    },
    [draftScope, drainSaves],
  );

  const captureTransientEdits = useCallback(() => {
    const drawing = drawingRef.current;
    const draft = textDraftRef.current;
    const value = textValueRef.current;
    const pan = panRef.current;
    const erasing = erasingRef.current;
    const text = value.trim();
    const panChanged = Boolean(pan && (pan.viewport.x !== boardRef.current.viewport.x || pan.viewport.y !== boardRef.current.viewport.y));
    const hasMutation = Boolean(drawing || text || erasing?.changed || panChanged);
    if (draftRecoveryRef.current && hasMutation) {
      throw new Error("Сначала завершите восстановление локального черновика доски.");
    }
    if (disabledRef.current && hasMutation) {
      throw new Error("Изменения доски заблокированы серверной политикой экзамена.");
    }

    let next = boardRef.current;
    let changed = false;
    let historyMutation = false;
    const previous = cloneBoard(next);
    if (drawing) {
      drawingRef.current = null;
      drawingStartRef.current = null;
      if (drawing.points.length >= 2) {
        const stroke = { ...drawing, points: drawing.points.map((point) => ({ ...point })) };
        next = { ...next, strokes: [...next.strokes, stroke] };
        changed = true;
        historyMutation = true;
      }
    }
    if (draft) {
      textDraftRef.current = null;
      textValueRef.current = "";
      setTextDraft(null);
      setTextValue("");
      if (text) {
        const nextText: BoardText = { id: "text-" + Date.now(), x: draft.x, y: draft.y, text };
        next = { ...next, texts: [...next.texts, nextText] };
        changed = true;
        historyMutation = true;
      }
    }
    if (pan) {
      panRef.current = null;
      if (panChanged) changed = true;
    }
    if (erasing) {
      erasingRef.current = null;
      if (erasing.changed) {
        changed = true;
        historyMutation = true;
      }
    }
    if (!changed) {
      if (drawing || draft) scheduleRender();
      return;
    }
    if (historyMutation) {
      setHistory((items) => [...items.slice(-29), previous]);
      setRedoHistory([]);
    }
    publishBoard(next);
    persist(next);
  }, [persist, publishBoard, scheduleRender]);

  const flushSaves = useCallback((options?:FlushOptions): Promise<void> => {
    if(options?.pendingOnly)return savePromiseRef.current?.then(()=>undefined)??Promise.resolve();
    captureTransientEdits();
    if (draftRecoveryRef.current) {
      throw new Error("Сначала завершите восстановление локального черновика доски.");
    }
    const ongoing = flushPromiseRef.current;
    if (ongoing) return ongoing;
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    if (!disabledRef.current && boardContentKey(boardRef.current) !== confirmedContentKeyRef.current) {
      queuedBoardRef.current = cloneBoard(boardRef.current);
    }
    const operation = (async () => {
      while (true) {
        if (!savePromiseRef.current) drainSaves();
        const pending = savePromiseRef.current;
        if (!pending) return;
        await pending;
      }
    })();
    flushPromiseRef.current = operation;
    void operation.finally(() => {
      if (flushPromiseRef.current === operation) flushPromiseRef.current = null;
    }).catch(() => undefined);
    return operation;
  }, [captureTransientEdits, drainSaves]);

  useEffect(() => {
    return registerDraftFlush(flushSaves,draftScope+":board",boardDraftKey);
  }, [flushSaves]);

  useEffect(() => {
    mountedRef.current = true;
    function flushWhenHidden() {
      if (document.visibilityState === "hidden") void flushSaves().catch(() => undefined);
    }
    document.addEventListener("visibilitychange", flushWhenHidden);
    return () => {
      document.removeEventListener("visibilitychange", flushWhenHidden);
      void flushSaves().catch(() => undefined);
      mountedRef.current = false;
    };
  }, [flushSaves]);

  const commit = useCallback(
    (next: BoardDocument) => {
      const previousBoard = cloneBoard(boardRef.current);
      setHistory((previous) => [...previous.slice(-29), previousBoard]);
      setRedoHistory([]);
      publishBoard(next);
      persist(next);
    },
    [persist, publishBoard],
  );

  useEffect(() => {
    return () => {
      if (renderFrameRef.current !== null && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(renderFrameRef.current);
      imageCacheRef.current.forEach(({ objectUrl }) => URL.revokeObjectURL(objectUrl));
      imageCacheRef.current.clear();
    };
  }, []);

  useEffect(() => {
    const assetId = importImage?.assetId?.trim();
    const revisionHash = importImage?.revisionHash?.trim();
    const requestId = importImage?.requestId?.trim();
    if (disabled || draftRecovery || !assetId || !revisionHash || !requestId) return undefined;
    const token = ++imageRequestRef.current;
    let disposed = false;
    const controller = new AbortController();
    setImageBusy(true);
    setImageError(null);
    void (async () => {
      try {
        const key = boardImageKey({ asset_id: assetId, revision_hash: revisionHash });
        let cached = imageCacheRef.current.get(key);
        if (!cached) {
          cached = await loadBoardImage(assetId, revisionHash, controller.signal);
          if (disposed || token !== imageRequestRef.current) {
            URL.revokeObjectURL(cached.objectUrl);
            return;
          }
          imageCacheRef.current.set(key, cached);
          setImageRenderVersion((value) => value + 1);
        }
        if (disposed || token !== imageRequestRef.current) return;
        const current = boardRef.current;
        const existing = (current.images ?? []).some((image) => image.asset_id === assetId && image.revision_hash === revisionHash);
        if (!existing) {
          const size = fitBoardImage(current, cached.image.naturalWidth, cached.image.naturalHeight, stageRef.current);
          commit({
            ...current,
            images: [
              ...(current.images ?? []),
              { id: `image-${Date.now()}-${requestId}`, asset_id: assetId, revision_hash: revisionHash, ...size },
            ],
          });
        }
      } catch {
        if (!disposed && token === imageRequestRef.current) setImageError("Иллюстрацию не удалось загрузить. Проверьте подключение и повторите попытку.");
      } finally {
        if (!disposed && token === imageRequestRef.current) setImageBusy(false);
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [commit, disabled, draftRecovery, imageRetry, importImage?.assetId, importImage?.requestId, importImage?.revisionHash]);

  function toWorld(event: PointerEvent<HTMLCanvasElement>) {
    const stage = stageRef.current;
    if (!stage) return { x: 0, y: 0 };
    const rect = stage.getBoundingClientRect();
    const current = boardRef.current;
    return {
      x: (event.clientX - rect.left - current.viewport.x) / current.viewport.zoom,
      y: (event.clientY - rect.top - current.viewport.y) / current.viewport.zoom,
    };
  }

  function hideEraserCursor() {
    eraserCursorRef.current?.classList.remove(styles.eraserCursorVisible);
  }

  function updateEraserCursor(event: PointerEvent<HTMLCanvasElement>) {
    const cursor = eraserCursorRef.current;
    const stage = stageRef.current;
    if (!cursor || !stage || tool !== "eraser" || disabled || draftRecovery || !active || event.pointerType === "touch") {
      hideEraserCursor();
      return;
    }
    const rect = stage.getBoundingClientRect();
    const left = event.clientX - rect.left;
    const top = event.clientY - rect.top;
    if (left < 0 || top < 0 || left > rect.width || top > rect.height) {
      hideEraserCursor();
      return;
    }
    cursor.style.left = `${left}px`;
    cursor.style.top = `${top}px`;
    cursor.classList.add(styles.eraserCursorVisible);
  }

  function onPointerLeave() {
    hideEraserCursor();
  }

  function releasePointer(event: PointerEvent<HTMLCanvasElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function eraseAt(point: { x: number; y: number }) {
    const gesture = erasingRef.current;
    if (!gesture) return;
    const current = boardRef.current;
    const radius = ERASER_RADIUS / Math.max(0.1, current.viewport.zoom);
    const nextStrokes = current.strokes.filter(
      (stroke) => {
        if (stroke.kind !== "rect" && stroke.kind !== "ellipse") {
          return !hitTestInkStroke(stroke.points, point, { width: 1, height: 1 }, stroke.width, radius);
        }
        return !stroke.points.some((item) => Math.hypot(item.x - point.x, item.y - point.y) < radius);
      },
    );
    if (nextStrokes.length === current.strokes.length) return;
    gesture.changed = true;
    publishBoard({ ...current, strokes: nextStrokes });
    scheduleRender();
  }

  function onPointerDown(event: PointerEvent<HTMLCanvasElement>) {
    updateEraserCursor(event);
    if (disabled || draftRecovery || !active || event.button !== 0) return;
    stageRef.current?.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    const current = boardRef.current;
    if (tool === "pan") {
      panRef.current = {
        x: event.clientX,
        y: event.clientY,
        viewport: { x: current.viewport.x, y: current.viewport.y },
      };
      return;
    }
    const point = toWorld(event);
    if (tool === "text") {
      textDraftRef.current = point;
      textValueRef.current = "";
      setTextDraft(point);
      setTextValue("");
      return;
    }
    if (tool === "eraser") {
      erasingRef.current = { before: cloneBoard(current), changed: false };
      eraseAt(point);
      return;
    }
    const selectedWidth = tool === "marker" ? toolWidths.marker : toolWidths.pen;
    const style = getStrokeStyle(tool, tool === "marker" ? toolColors.marker : toolColors.pen, current.viewport.zoom, selectedWidth);
    if (!style) return;
    drawingRef.current = {
      id: "stroke-" + Date.now() + "-" + (++strokeCounterRef.current),
      kind: style.kind,
      points: [point],
      color: style.color,
      width: style.width,
      opacity: strokeOpacityForPreference(style.kind, tool==="marker"?toolOpacities.marker:toolOpacities.pen),
      // Legacy preference contract: strokeOpacityForPreference(style.kind, tool==="marker"?getPreferences().boardMarkerOpacity:getPreferences().boardOpacity)
    };
    drawingStartRef.current = {
      x: point.x,
      y: point.y,
      straight: (tool === "pen" || tool === "marker") && event.shiftKey,
    };
  }

  function onPointerMove(event: PointerEvent<HTMLCanvasElement>) {
    updateEraserCursor(event);
    if (disabled || draftRecovery || !active) return;
    if (panRef.current) {
      const pan = panRef.current;
      const next = {
        ...boardRef.current,
        viewport: {
          ...boardRef.current.viewport,
          x: pan.viewport.x + event.clientX - pan.x,
          y: pan.viewport.y + event.clientY - pan.y,
        },
      };
      publishBoard(next);
      return;
    }
    if (erasingRef.current) {
      eraseAt(toWorld(event));
      return;
    }
    if (!drawingRef.current) return;
    const point = toWorld(event);
    const drawing = drawingRef.current;
    const drawingStart = drawingStartRef.current;
    if (drawingStart && (drawing.kind === "freehand" || drawing.kind === "marker")) {
      if (event.shiftKey) drawingStart.straight = true;
      if (drawingStart.straight) {
        drawing.points = [{ x: drawingStart.x, y: drawingStart.y }, point];
      } else {
        drawing.points.push(point);
      }
    } else if (drawing.kind === "line" || drawing.kind === "rect" || drawing.kind === "ellipse") {
      drawing.points = [drawing.points[0] ?? point, point];
    } else {
      drawing.points.push(point);
    }
    publishBoard({ ...boardRef.current });
  }

  function onPointerUp(event: PointerEvent<HTMLCanvasElement>) {
    updateEraserCursor(event);
    if (disabled) {
      releasePointer(event);
      return;
    }
    if (!active || draftRecovery) {
      panRef.current=null;erasingRef.current=null;drawingRef.current=null;drawingStartRef.current=null;
      releasePointer(event);publishBoard({ ...boardRef.current });return;
    }
    if (panRef.current) {
      panRef.current = null;
      persist(boardRef.current);
      releasePointer(event);
      return;
    }
    if (erasingRef.current) {
      const gesture = erasingRef.current;
      erasingRef.current = null;
      if (gesture.changed) {
        const next = cloneBoard(boardRef.current);
        setHistory((items) => [...items.slice(-29), gesture.before]);
        setRedoHistory([]);
        persist(next);
      }
      releasePointer(event);
      return;
    }
    const stroke = drawingRef.current;
    drawingRef.current = null;
    drawingStartRef.current = null;
    if (stroke && stroke.points.length >= 2) {
      const current = boardRef.current;
      commit({ ...current, strokes: [...current.strokes, stroke] });
    }
    releasePointer(event);
  }

  function onPointerCancel(event: PointerEvent<HTMLCanvasElement>) {
    hideEraserCursor();
    if (disabled) {
      releasePointer(event);
      return;
    }
    const wasPanning = Boolean(panRef.current);
    const wasErasing = erasingRef.current;
    panRef.current = null;
    erasingRef.current = null;
    drawingRef.current = null;
    drawingStartRef.current = null;
    if (wasPanning) persist(boardRef.current);
    if (wasErasing?.changed) persist(boardRef.current);
    releasePointer(event);
  }

  function addText() {
    if (disabled || !active || draftRecovery) return;
    const draft = textDraftRef.current;
    const value = textValueRef.current;
    if (!draft || !value.trim()) {
      textDraftRef.current = null;
      textValueRef.current = "";
      setTextDraft(null);
      setTextValue("");
      return;
    }
    const text: BoardText = {
      id: "text-" + Date.now(),
      x: draft.x,
      y: draft.y,
      text: value.trim(),
    };
    const current = boardRef.current;
    commit({ ...current, texts: [...current.texts, text] });
    textDraftRef.current = null;
    textValueRef.current = "";
    setTextDraft(null);
    setTextValue("");
  }

  function zoom(delta: number) {
    const current = boardRef.current;
    const nextZoom = Math.max(0.35, Math.min(2.5, current.viewport.zoom + delta));
    commit({
      ...current,
      viewport: { ...current.viewport, zoom: Number(nextZoom.toFixed(2)) },
    });
  }

  function resetViewport() {
    const current = boardRef.current;
    commit({ ...current, viewport: { x: 0, y: 0, zoom: 1 } });
  }

  function undo() {
    const previous = history.at(-1);
    if (!previous) return;
    const currentBoard = cloneBoard(boardRef.current);
    setHistory((items) => items.slice(0, -1));
    setRedoHistory((items) => [...items.slice(-29), currentBoard]);
    publishBoard(previous);
    persist(previous);
  }

  function redo() {
    const next = redoHistory.at(-1);
    if (!next) return;
    const currentBoard = cloneBoard(boardRef.current);
    setRedoHistory((items) => items.slice(0, -1));
    setHistory((items) => [...items.slice(-29), currentBoard]);
    publishBoard(next);
    persist(next);
  }

  function clearBoard() {
    const current = boardRef.current;
    if (!current.strokes.length && !current.texts.length && !(current.images ?? []).length) return;
    setClearConfirmOpen(true);
  }

  function confirmClearBoard() {
    const current = boardRef.current;
    commit({ ...current, strokes: [], texts: [], images: [] });
    setClearConfirmOpen(false);
  }

  function restoreDraft() {
    if (disabled || !draftRecovery) return;
    const previous = cloneBoard(boardRef.current);
    archiveDraftConflict(draftScope,"board",draftRecovery.content,previous);
    const restored = boardFromDraft(previous, draftRecovery.content);
    setHistory((items) => [...items.slice(-29), previous]);
    setRedoHistory([]);
    publishBoard(restored);
    if (!disabled) persist(restored);
    setDraftRecovery(null);
  }

  function keepServerBoard() {
    acceptDraftHandoff(draftScope+":board",boardDraftKey());
    if(draftRecovery)archiveDraftConflict(draftScope,"board",draftRecovery.content,boardRef.current);
    clearLocalDraft(draftScope, "board");
    setDraftRecovery(null);
  }

  function restoreTemporaryPan() {
    const previous = temporaryPanRef.current;
    if (!previous) return;
    temporaryPanRef.current = null;
    setTool((current) => current === "pan" ? previous : current);
  }

  function onBoardKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (disabled || draftRecovery || !active || event.defaultPrevented || isTypingTarget(event.target)) return;
    if (isBoardSpaceKey(event)) {
      event.preventDefault();
      if (!event.repeat) beginTemporaryPan();
    }
  }

  function onBoardKeyUp(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (disabled || draftRecovery || !active || isTypingTarget(event.target) || !isBoardSpaceKey(event)) return;
    event.preventDefault();
    restoreTemporaryPan();
  }

  function onBoardBlur(event: ReactFocusEvent<HTMLDivElement>) {
    hideEraserCursor();
    const nextFocus = event.relatedTarget as Node | null;
    if (!nextFocus || !event.currentTarget.contains(nextFocus)) {
      setBoardFocused(false);
      restoreTemporaryPan();
    }
  }

  useEffect(() => {
    const restoreOnWindowBlur = () => {
      restoreTemporaryPan();
      hideEraserCursor();
    };
    window.addEventListener("blur", restoreOnWindowBlur);
    return () => window.removeEventListener("blur", restoreOnWindowBlur);
  }, [tool]);

  function selectColor(color: string) {
    if (tool === "marker") {
      setToolColors((current) => ({ ...current, marker: color }));
      setPreferences({ boardMarkerColor: color });
    } else {
      setToolColors((current) => ({ ...current, pen: color }));
      setPreferences({ boardColor: color });
    }
    setColorOpen(false);
  }

  function selectWidth(value: string) {
    const width = Number(value);
    if (!Number.isFinite(width) || width < 1 || width > 24) return;
    if (tool === "marker") {
      setToolWidths((current) => ({ ...current, marker: width }));
      setPreferences({ boardMarkerWidth: width });
    } else {
      setToolWidths((current) => ({ ...current, pen: width }));
      setPreferences({ boardWidth: width });
    }
  }

  function selectOpacity(value: string) {
    const opacity = Number(value);
    if (!Number.isFinite(opacity) || opacity < 0.1 || opacity > 1) return;
    if (tool === "marker") {
      setToolOpacities((current) => ({ ...current, marker: opacity }));
      setPreferences({ boardMarkerOpacity: opacity });
    } else {
      setToolOpacities((current) => ({ ...current, pen: opacity }));
      setPreferences({ boardOpacity: opacity });
    }
  }

  function selectBackground(value: BoardBackground) {
    setPreferences({ boardBackground: value });
    setBoardBackground(value);
  }

  function beginTemporaryPan() {
    if (temporaryPanRef.current !== null || tool === "pan") return;
    temporaryPanRef.current = tool;
    setTool("pan");
  }

  const shortcutActions = active && boardFocused && !disabled && !draftRecovery && !clearConfirmOpen ? {
    "board.pen": () => setTool("pen"),
    "board.marker": () => setTool("marker"),
    "board.pan": beginTemporaryPan,
    "board.line": () => setTool("line"),
    "board.rect": () => setTool("rect"),
    "board.ellipse": () => setTool("ellipse"),
    "board.text": () => setTool("text"),
    "board.eraser": () => setTool("eraser"),
    "board.undo": undo,
    "board.redo": redo,
    "board.clear": clearBoard,
    "board.zoom.in": () => zoom(0.1),
    "board.zoom.out": () => zoom(-0.1),
    "board.zoom.reset": resetViewport,
  } : {};
  useShortcuts("canvas", shortcutActions);

  const paletteTool = tool === "marker" ? "marker" : "pen";
  const activeColor = toolColors[paletteTool];
  const activeWidth = toolWidths[paletteTool];
  const activeOpacity = toolOpacities[paletteTool];
  const controlsDisabled = disabled || !active || Boolean(draftRecovery);
  useEffect(() => {
    setColorOpen(false);
    setBoardMenuOpen(false);
  }, [tool, controlsDisabled]);

  useEffect(() => {
    if (!colorOpen && !boardMenuOpen) return;
    const closeOutside = (event: globalThis.PointerEvent) => {
      const target = event.target as Node | null;
      if (target && (toolbarRef.current?.contains(target) || boardMenuRef.current?.contains(target))) return;
      setColorOpen(false);
      setBoardMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      (colorOpen ? brushButtonRef.current : boardMenuButtonRef.current)?.focus();
      setColorOpen(false);
      setBoardMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [boardMenuOpen, colorOpen]);
  const stageClassName = [
    "board-stage",
    "tool-" + tool,
    styles.stage,
    tool === "pan" ? styles.stagePan : "",
    tool === "text" ? styles.stageText : "",
    tool === "eraser" ? styles.stageEraser : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={["board-shell", styles.shell].join(" ")}>
      {draftRecovery ? (
        <div
          aria-live="polite"
          className={["board-status", styles.status, draftRecovery.status === "conflict" ? styles.statusError : ""].filter(Boolean).join(" ")}
          role={draftRecovery.status === "conflict" ? "alert" : "status"}
        >
          <span>
            {draftRecovery.status === "conflict"
              ? "На сервере уже другая версия доски. Выберите, что оставить."
              : "Есть локальный черновик после обновления или ошибки сохранения."}
          </span>
          <div className="kontur-modal-actions">
            <button className="outline-button" onClick={keepServerBoard} type="button">Оставить серверную версию</button>
            <button className="primary-button" disabled={disabled} onClick={restoreDraft} type="button">
              {disabled ? "Локальный черновик сохранён отдельно" : "Восстановить черновик"}
            </button>
          </div>
        </div>
      ) : null}
      <Modal open={clearConfirmOpen} onClose={()=>setClearConfirmOpen(false)} title="Очистить доску?">
        <p>Штрихи, текст и вставленные иллюстрации будут убраны из текущего черновика.</p>
        <div className="kontur-modal-actions">
          <button className="outline-button" onClick={()=>setClearConfirmOpen(false)} type="button">Отмена</button>
          <button className="primary-button" onClick={confirmClearBoard} type="button">Очистить</button>
        </div>
      </Modal>
      <div className={styles.workspace} data-brush-open={colorOpen || undefined}>
      <div className={["board-toolbar", styles.toolbar].join(" ")} ref={toolbarRef} role="toolbar" aria-label="Инструменты доски" aria-orientation="vertical" data-board-chrome="rail">
        <div className={styles.toolRow}>
          <div className={styles.toolGroup} role="group" aria-label="Инструменты рисования">
            {TOOLS.map((item) => (
              <button aria-label={item.label} aria-pressed={tool === item.key}
                className={["tool-button", styles.toolButton, tool === item.key ? "is-selected " + styles.selected : ""].filter(Boolean).join(" ")}
                disabled={controlsDisabled} key={item.key} onClick={() => { setTool(item.key); setColorOpen(false); setBoardMenuOpen(false); }} title={item.label} type="button">
                <Icon name={item.icon} size={20} />
                <span className={styles.toolLabel}>{item.label}</span>
              </button>
            ))}
          </div>
          <span className={styles.divider} aria-hidden="true" />
          <button aria-expanded={colorOpen} aria-controls={`${chromeId}-brush`} aria-label="Настройки кисти" title="Цвет, толщина и непрозрачность"
            className={[styles.toolButton, colorOpen ? styles.selected : ""].filter(Boolean).join(" ")}
            disabled={controlsDisabled || tool === "eraser" || tool === "pan"} ref={brushButtonRef}
            onClick={() => { setBoardMenuOpen(false); setColorOpen((open) => !open); }} type="button">
            <span className={styles.currentSwatch} style={{ backgroundColor: activeColor }} />
          </button>
        </div>
        {colorOpen ? (
          <div className={styles.propertyRow} id={`${chromeId}-brush`} role="group" aria-label="Настройки инструмента">
            <div className={styles.panelHeading}>
              <strong>{tool === "marker" ? "Маркер" : tool === "text" ? "Текст" : "Перо и фигуры"}</strong>
              <button aria-label="Закрыть настройки кисти" title="Закрыть настройки кисти" className={styles.toolButton}
                onClick={() => { setColorOpen(false); brushButtonRef.current?.focus(); }} type="button"><Icon name="close" size={16} /></button>
            </div>
            <div className={styles.colorControl}>
              <span className={styles.controlLabel}>Цвет</span>
              <div className={styles.colorPopover} role="group" aria-label="Палитра цветов">
                {PALETTE.map((item) => (
                  <button aria-label={item.label} aria-pressed={activeColor === item.value}
                    className={[styles.swatch, activeColor === item.value ? styles.swatchSelected : ""].filter(Boolean).join(" ")}
                    disabled={controlsDisabled} key={item.value} onClick={() => selectColor(item.value)}
                    style={{ backgroundColor: item.value }} title={item.label} type="button" />
                ))}
              </div>
            </div>
            <label className={styles.thicknessControl}>
              <span>Толщина штриха</span>
              <output className={styles.thicknessOutput}>{activeWidth}</output>
              <input aria-label="Толщина штриха" className={styles.thicknessRange} disabled={controlsDisabled} max={24} min={1} onChange={(event) => selectWidth(event.target.value)} step={1} type="range" value={activeWidth} />
            </label>
            <label className={styles.opacityControl}>
              <span>Непрозрачность</span>
              <output className={styles.opacityOutput}>{Math.round(activeOpacity * 100)}%</output>
              <input aria-label="Непрозрачность штриха" className={styles.opacityRange} disabled={controlsDisabled} max={1} min={0.1} onChange={(event) => selectOpacity(event.target.value)} step={0.05} type="range" value={activeOpacity} />
            </label>
          </div>
        ) : null}
      </div>
      <div className={styles.boardActions} ref={boardMenuRef}>
        <button aria-label="Настройки доски" title="Фон и очистка доски" aria-expanded={boardMenuOpen} aria-controls={`${chromeId}-board`}
          className={styles.toolButton} disabled={controlsDisabled} ref={boardMenuButtonRef}
          onClick={() => { setColorOpen(false); setBoardMenuOpen((open) => !open); }} type="button"><Icon name="settings" size={19} /></button>
        {boardMenuOpen ? (
          <div className={styles.boardPopover} id={`${chromeId}-board`} role="group" aria-label="Настройки доски">
            <strong className={styles.controlLabel}>Фон доски</strong>
            <div className={styles.backgroundControl} role="group" aria-label="Фон доски">
              {BACKGROUNDS.map((item) => (
                <button aria-pressed={boardBackground === item.value} title={item.label}
                  className={[styles.backgroundButton, boardBackground === item.value ? styles.backgroundSelected : ""].filter(Boolean).join(" ")}
                  disabled={controlsDisabled} key={item.value} onClick={() => selectBackground(item.value)} type="button">{item.label}</button>
              ))}
            </div>
            <button className={["board-clear", styles.clearButton].join(" ")} disabled={controlsDisabled}
              onClick={() => { setBoardMenuOpen(false); clearBoard(); }} type="button">Очистить доску</button>
          </div>
        ) : null}
      </div>
      <div className={styles.navigationDock} role="toolbar" aria-label="Навигация доски" data-board-chrome="navigation">
        <div className={styles.historyTools} role="group" aria-label="История действий">
          <button aria-label="Отменить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled || !history.length} onClick={undo} title="Отменить" type="button"><Icon name="undo" size={18} /></button>
          <button aria-label="Повторить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled || !redoHistory.length} onClick={redo} title="Повторить" type="button"><Icon name="redo" size={18} /></button>
        </div>
        <div className={styles.zoomTools} role="group" aria-label="Масштаб доски">
          <button aria-label="Уменьшить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={() => zoom(-0.1)} title="Уменьшить" type="button"><Icon name="zoom-out" size={18} /></button>
          <span className={["zoom-label", styles.zoomLabel].join(" ")}>{Math.round(board.viewport.zoom * 100)}%</span>
          <button aria-label="Увеличить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={() => zoom(0.1)} title="Увеличить" type="button"><Icon name="zoom-in" size={18} /></button>
          <button aria-label="Вернуть масштаб и положение доски" title="Вернуть масштаб и положение" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={resetViewport} type="button"><Icon name="maximize" size={18} /></button>
        </div>
      </div>
      <div
        aria-label="Бесконечная доска"
        className={stageClassName}
        ref={stageRef}
        tabIndex={controlsDisabled ? -1 : 0}
        onBlur={onBoardBlur}
        onDoubleClick={() => { if (!controlsDisabled) setTool("pan"); }}
        onFocus={() => setBoardFocused(true)}
        onKeyDown={onBoardKeyDown}
        onKeyUp={onBoardKeyUp}
      >
        <canvas
          aria-label="Бесконечная доска"
          className={["board-canvas", styles.canvas].join(" ")}
          onPointerDown={onPointerDown}
          onPointerLeave={onPointerLeave}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerCancel}
          ref={canvasRef}
        />
        <div aria-hidden="true" className={styles.eraserCursor} ref={eraserCursorRef} />
        <div className={["board-text-layer", styles.textLayer].join(" ")}>
          {board.texts.map((text) => (
            <span
              className={["board-text-object", styles.textObject].join(" ")}
              key={text.id}
              style={{
                left: board.viewport.x + text.x * board.viewport.zoom,
                top: board.viewport.y + text.y * board.viewport.zoom,
                transform: "translateY(-100%)",
              }}
            >
              {text.text}
            </span>
          ))}
        </div>
        {textDraft ? (
          <div className={["board-text-editor", styles.textEditor].join(" ")} style={{ left: board.viewport.x + textDraft.x * board.viewport.zoom, top: board.viewport.y + textDraft.y * board.viewport.zoom }}>
            <input
              autoFocus
              aria-label="Текст на доске"
              disabled={controlsDisabled}
              onChange={(event) => { textValueRef.current = event.target.value; setTextValue(event.target.value); }}
              onKeyDown={(event) => {
                if (event.key === "Enter") addText();
                if (event.key === "Escape") { textDraftRef.current = null; textValueRef.current = ""; setTextDraft(null); setTextValue(""); }
              }}
              onBlur={addText}
              placeholder="Введите текст"
              value={textValue}
            />
          </div>
        ) : null}
      </div>
      </div>
      {imageBusy ? <p className={styles.imageStatus} role="status">Загружаем иллюстрацию…</p> : null}
      {imageError ? (
        <div className={styles.imageStatusError} role="alert">
          <span>{imageError}</span>
          <button onClick={() => { setImageError(null); setImageRetry((value) => value + 1); }} type="button">Повторить</button>
        </div>
      ) : null}
      {saveState === "error" && saveError ? (
        <div className={["board-status", styles.status, styles.statusError].join(" ")} role="alert">
          <span className={["status-dot", styles.statusDot].join(" ")} />
          <span>Не удалось сохранить изменения</span>
          <span className={["board-error", styles.error].join(" ")}>{saveError}</span>
        </div>
      ) : null}
    </div>
  );
}
