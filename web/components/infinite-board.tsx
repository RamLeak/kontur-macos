"use client";

import type { FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Modal } from "./modal";
import { Icon, type IconName } from "@/components/icon";
import styles from "./infinite-board.module.css";
import { native } from "@/lib/native";
import {
  getStrokeStyle,
  isBoardSpaceKey,
  strokeOpacity,
  strokeOpacityForPreference,
  type BoardTool,
} from "@/lib/board-tools";
import { drawSmoothRoundStroke } from "@/lib/stroke-smoothing";
import { registerSurfaceFlush } from "@/lib/surface-lifecycle";
import {
  BOARD_PREFERENCES_CHANGED,
  getBoardPreferences,
  setBoardPreferences,
  type BoardBackground,
} from "@/lib/board-preferences";
import { useBoardShortcuts, type BoardShortcutActions } from "@/lib/board-shortcuts";
import type { BoardDocument, BoardImage, BoardStroke, BoardText } from "@/lib/board-types";

type SaveState = "saved" | "saving" | "error";
type ImageCacheEntry = { image: HTMLImageElement; objectUrl: string };

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

const PALETTE = [
  { value: "#242529", label: "Графит" },
  { value: "#2563eb", label: "Синий" },
  { value: "#dc2626", label: "Красный" },
  { value: "#16a34a", label: "Зелёный" },
  { value: "#f2c94c", label: "Жёлтый" },
  { value: "#a855f7", label: "Фиолетовый" },
] as const;

const BACKGROUNDS: Array<{ value: BoardBackground; label: string }> = [
  { value: "blank", label: "Чистый" },
  { value: "grid", label: "Клетка" },
  { value: "dots", label: "Точки" },
];

const BOARD_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const MAX_BOARD_IMAGE_BYTES = 8 * 1024 * 1024;

function cloneBoard(board: BoardDocument): BoardDocument {
  return JSON.parse(JSON.stringify(board)) as BoardDocument;
}

function boardContentKey(board: Pick<BoardDocument, "strokes" | "texts" | "images" | "viewport">): string {
  return JSON.stringify({ strokes: board.strokes, texts: board.texts, images: board.images ?? [], viewport: board.viewport });
}

function readableError(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return Boolean(element && typeof element.closest === "function" && element.closest("input, textarea, select, [contenteditable='true']"));
}

function boardImageKey(image: Pick<BoardImage, "asset_id" | "revision_hash">): string {
  return image.asset_id + ":" + image.revision_hash;
}

async function loadBoardImage(
  assetId: string,
  signal?: AbortSignal,
): Promise<ImageCacheEntry> {
  const data = await native.asset(assetId);
  signal?.throwIfAborted();
  if (data.metadata.id !== assetId) throw new Error("Идентификатор иллюстрации не совпадает.");
  if (!Number.isSafeInteger(data.metadata.sizeBytes) || data.metadata.sizeBytes < 0 || data.metadata.sizeBytes > MAX_BOARD_IMAGE_BYTES || data.bytes.length > MAX_BOARD_IMAGE_BYTES) {
    throw new Error("Иллюстрация больше разрешённых 8 МиБ.");
  }
  const mediaType = data.metadata.mime.split(";", 1)[0]?.trim().toLowerCase() ?? "";
  if (!BOARD_IMAGE_TYPES.has(mediaType)) throw new Error("Тип иллюстрации не поддерживается.");
  const objectUrl = URL.createObjectURL(new Blob([new Uint8Array(data.bytes)], { type: mediaType }));
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const element = new Image();
      let settled = false;
      const finish = (error?: unknown) => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener("abort", onAbort);
        if (error) reject(error); else resolve(element);
      };
      const onAbort = () => finish(new DOMException("Загрузка отменена.", "AbortError"));
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

function fitBoardImage(
  board: BoardDocument,
  naturalWidth: number,
  naturalHeight: number,
  stage: HTMLDivElement | null,
): Pick<BoardImage, "x" | "y" | "width" | "height"> {
  const rect = stage?.getBoundingClientRect();
  const zoom = Number.isFinite(board.viewport.zoom) && board.viewport.zoom > 0 ? board.viewport.zoom : 1;
  const screenWidth = Math.max(rect?.width ?? 0, 800);
  const screenHeight = Math.max(rect?.height ?? 0, 600);
  const padding = 36 / zoom;
  const availableWidth = Math.max(120 / zoom, screenWidth / zoom - padding * 2);
  const availableHeight = Math.max(120 / zoom, screenHeight / zoom - padding * 2);
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
  persist,
  onChange,
  importImage,
}: {
  sessionId: string;
  initialBoard: BoardDocument;
  disabled?: boolean;
  active?: boolean;
  persist: (document: BoardDocument) => Promise<BoardDocument>;
  onChange?: (document: BoardDocument) => void;
  importImage?: { assetId: string; revisionHash: string; requestId: string };
}) {
  const [board, setBoard] = useState(() => cloneBoard(initialBoard));
  const [history, setHistory] = useState<BoardDocument[]>([]);
  const [redoHistory, setRedoHistory] = useState<BoardDocument[]>([]);
  const [tool, setTool] = useState<BoardTool>("pen");
  const defaults = getBoardPreferences();
  const [toolColors, setToolColors] = useState({ pen: defaults.boardColor, marker: defaults.boardMarkerColor });
  const [toolWidths, setToolWidths] = useState({ pen: defaults.boardWidth, marker: defaults.boardMarkerWidth });
  const [toolOpacities, setToolOpacities] = useState({ pen: defaults.boardOpacity, marker: defaults.boardMarkerOpacity });
  const [drawingSmoothing, setDrawingSmoothing] = useState(defaults.drawingSmoothing);
  const [boardBackground, setBoardBackground] = useState<BoardBackground>(defaults.boardBackground);
  const [colorOpen, setColorOpen] = useState(false);
  const [boardFocused, setBoardFocused] = useState(false);
  const [imageBusy, setImageBusy] = useState(false);
  const [imageError, setImageError] = useState<string | null>(null);
  const [imageRetry, setImageRetry] = useState(0);
  const [imageRenderVersion, setImageRenderVersion] = useState(0);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [textDraft, setTextDraft] = useState<{ x: number; y: number } | null>(null);
  const [textValue, setTextValue] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const boardRef = useRef(board);
  const drawingRef = useRef<BoardStroke | null>(null);
  const drawingStartRef = useRef<{ x: number; y: number; straight: boolean } | null>(null);
  const erasingRef = useRef<{ before: BoardDocument; changed: boolean } | null>(null);
  const panRef = useRef<{ x: number; y: number; viewport: { x: number; y: number } } | null>(null);
  const temporaryPanRef = useRef<BoardTool | null>(null);
  const textDraftRef = useRef<{ x: number; y: number } | null>(textDraft);
  const textValueRef = useRef(textValue);
  const imageCacheRef = useRef<Map<string, ImageCacheEntry>>(new Map());
  const imageRequestRef = useRef(0);
  const renderFrameRef = useRef<number | null>(null);
  const strokeCounterRef = useRef(0);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingBoardRef = useRef<BoardDocument | null>(null);
  const savePromiseRef = useRef<Promise<void> | null>(null);
  const lastSavedKeyRef = useRef(boardContentKey(board));
  const mountedRef = useRef(false);
  const onChangeRef = useRef(onChange);
  const persistRef = useRef(persist);
  onChangeRef.current = onChange;
  persistRef.current = persist;
  textDraftRef.current = textDraft;
  textValueRef.current = textValue;

  useEffect(() => { boardRef.current = board; }, [board]);

  useEffect(() => {
    if (initialBoard.id !== boardRef.current.id || initialBoard.version > boardRef.current.version) {
      const next = cloneBoard(initialBoard);
      boardRef.current = next;
      setBoard(next);
      setHistory([]);
      setRedoHistory([]);
      pendingBoardRef.current = null;
      lastSavedKeyRef.current = boardContentKey(next);
      setSaveState("saved");
      setSaveError(null);
    }
  }, [initialBoard]);

  function queuePersist(next: BoardDocument): void {
    pendingBoardRef.current = cloneBoard(next);
    setSaveState("saving");
    setSaveError(null);
    if (saveTimerRef.current !== null) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      saveTimerRef.current = null;
      void drainSaves().catch(() => undefined);
    }, 350);
  }

  async function drainSaves(): Promise<void> {
    if (savePromiseRef.current) return savePromiseRef.current;
    const operation = (async () => {
      while (pendingBoardRef.current) {
        const sent = pendingBoardRef.current;
        pendingBoardRef.current = null;
        let saved: BoardDocument;
        try {
          saved = await persistRef.current(cloneBoard(sent));
        } catch (reason) {
          if (!pendingBoardRef.current) pendingBoardRef.current = sent;
          setSaveState("error");
          setSaveError(readableError(reason, "Не удалось сохранить доску. Повторите попытку."));
          throw reason;
        }
        if (saved.id !== sent.id || !Number.isSafeInteger(saved.version) || saved.version < sent.version) {
          const error = new Error("INVALID_STORAGE_ACK");
          if (!pendingBoardRef.current) pendingBoardRef.current = sent;
          setSaveState("error");
          setSaveError("Приложение вернуло неподтверждённую версию доски.");
          throw error;
        }
        lastSavedKeyRef.current = boardContentKey(saved);
        const current = boardRef.current;
        if (boardContentKey(current) === boardContentKey(sent)) {
          const committed = cloneBoard(saved);
          boardRef.current = committed;
          setBoard(committed);
          onChangeRef.current?.(committed);
        } else {
          const merged = { ...current, version: saved.version, updated_at: saved.updated_at };
          boardRef.current = merged;
          setBoard(merged);
          onChangeRef.current?.(merged);
          pendingBoardRef.current = cloneBoard(merged);
        }
      }
      setSaveState("saved");
      setSaveError(null);
    })();
    savePromiseRef.current = operation;
    try { await operation; } finally {
      if (savePromiseRef.current === operation) savePromiseRef.current = null;
    }
  }

  function publishBoard(next: BoardDocument, persistNow = true): void {
    const cloned = cloneBoard(next);
    boardRef.current = cloned;
    setBoard(cloned);
    onChangeRef.current?.(cloned);
    if (persistNow) queuePersist(cloned);
    scheduleRender();
  }

  function flushTransientEdits(): void {
    const current = boardRef.current;
    let next = current;
    if (drawingRef.current && drawingRef.current.points.length > 0) {
      next = { ...next, strokes: [...next.strokes, JSON.parse(JSON.stringify(drawingRef.current)) as BoardStroke] };
      drawingRef.current = null;
      drawingStartRef.current = null;
    }
    const draft = textDraftRef.current;
    const value = textValueRef.current.trim();
    if (draft && value) {
      next = { ...next, texts: [...next.texts, { id: "text-" + sessionId + "-" + Date.now(), x: draft.x, y: draft.y, text: value }] };
    }
    if (next !== current) {
      textDraftRef.current = null;
      textValueRef.current = "";
      setTextDraft(null);
      setTextValue("");
      publishBoard(next, false);
    }
    if (boardContentKey(boardRef.current) !== lastSavedKeyRef.current) pendingBoardRef.current = cloneBoard(boardRef.current);
  }

  const flushSaves = useCallback(async () => {
    if (saveTimerRef.current !== null) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    flushTransientEdits();
    if (boardContentKey(boardRef.current) !== lastSavedKeyRef.current && !pendingBoardRef.current) pendingBoardRef.current = cloneBoard(boardRef.current);
    await drainSaves();
  }, [sessionId]);

  useEffect(() => {
    mountedRef.current = true;
    const unregister = registerSurfaceFlush(flushSaves, flushTransientEdits);
    const onVisibilityChange = () => { if (document.visibilityState === "hidden") void flushSaves().catch(() => undefined); };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      unregister();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      mountedRef.current = false;
      if (renderFrameRef.current !== null) cancelAnimationFrame(renderFrameRef.current);
      void flushSaves().catch(() => undefined);
      imageCacheRef.current.forEach(({ objectUrl }) => URL.revokeObjectURL(objectUrl));
      imageCacheRef.current.clear();
    };
  }, [flushSaves]);

  useEffect(() => {
    const syncPreferences = () => {
      const next = getBoardPreferences();
      setBoardBackground(next.boardBackground);
      setToolColors({ pen: next.boardColor, marker: next.boardMarkerColor });
      setToolWidths({ pen: next.boardWidth, marker: next.boardMarkerWidth });
      setToolOpacities({ pen: next.boardOpacity, marker: next.boardMarkerOpacity });
      setDrawingSmoothing(next.drawingSmoothing);
    };
    window.addEventListener(BOARD_PREFERENCES_CHANGED, syncPreferences);
    window.addEventListener("storage", syncPreferences);
    return () => {
      window.removeEventListener(BOARD_PREFERENCES_CHANGED, syncPreferences);
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
    const current = boardRef.current;
    context.translate(current.viewport.x, current.viewport.y);
    context.scale(current.viewport.zoom, current.viewport.zoom);
    const gridStep = 28;
    const startX = Math.floor(-current.viewport.x / current.viewport.zoom / gridStep) * gridStep - gridStep;
    const startY = Math.floor(-current.viewport.y / current.viewport.zoom / gridStep) * gridStep - gridStep;
    const endX = (rect.width - current.viewport.x) / current.viewport.zoom + gridStep;
    const endY = (rect.height - current.viewport.y) / current.viewport.zoom + gridStep;
    if (boardBackground === "grid") {
      context.strokeStyle = gridColor;
      context.lineWidth = 1 / current.viewport.zoom;
      context.beginPath();
      for (let x = startX; x < endX; x += gridStep) { context.moveTo(x, startY); context.lineTo(x, endY); }
      for (let y = startY; y < endY; y += gridStep) { context.moveTo(startX, y); context.lineTo(endX, y); }
      context.stroke();
    } else if (boardBackground === "dots") {
      context.fillStyle = gridColor;
      for (let x = startX; x < endX; x += gridStep) for (let y = startY; y < endY; y += gridStep) {
        context.beginPath(); context.arc(x, y, 0.85 / current.viewport.zoom, 0, Math.PI * 2); context.fill();
      }
    }
    (current.images ?? []).forEach((image) => {
      const cached = imageCacheRef.current.get(boardImageKey(image));
      if (cached && image.width > 0 && image.height > 0) context.drawImage(cached.image, image.x, image.y, image.width, image.height);
    });
    context.globalAlpha = 1;
    context.lineCap = "round";
    context.lineJoin = "round";
    [...current.strokes, ...(drawingRef.current ? [drawingRef.current] : [])].forEach((stroke) => {
      if (!stroke.points.length) return;
      context.strokeStyle = stroke.color;
      context.globalAlpha = strokeOpacity(stroke.kind, stroke.opacity);
      context.lineWidth = stroke.width;
      if (stroke.points.length === 1) {
        context.beginPath(); context.arc(stroke.points[0].x, stroke.points[0].y, Math.max(0.5, stroke.width / 2), 0, Math.PI * 2); context.stroke();
        return;
      }
      const start = stroke.points[0];
      const end = stroke.points[stroke.points.length - 1];
      if (stroke.kind === "line") {
        context.beginPath(); context.moveTo(start.x, start.y); context.lineTo(end.x, end.y); context.stroke();
      } else if (stroke.kind === "rect") {
        context.beginPath(); context.rect(start.x, start.y, end.x - start.x, end.y - start.y); context.stroke();
      } else if (stroke.kind === "ellipse") {
        context.beginPath(); context.ellipse((start.x + end.x) / 2, (start.y + end.y) / 2, Math.abs(end.x - start.x) / 2, Math.abs(end.y - start.y) / 2, 0, 0, Math.PI * 2); context.stroke();
      } else {
        drawSmoothRoundStroke(context, stroke.points, drawingSmoothing);
      }
    });
    context.restore();
  }, [boardBackground, drawingSmoothing, imageRenderVersion]);

  const scheduleRender = useCallback(() => {
    if (renderFrameRef.current !== null) return;
    if (typeof requestAnimationFrame === "undefined") return;
    renderFrameRef.current = requestAnimationFrame(() => {
      renderFrameRef.current = null;
      render();
    });
  }, [render]);

  useLayoutEffect(() => { if (active) render(); }, [active, render]);
  useEffect(() => {
    scheduleRender();
    const stage = stageRef.current;
    if (!stage) return undefined;
    const observer = new ResizeObserver(scheduleRender);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [scheduleRender]);

  function toWorld(event: ReactPointerEvent<HTMLCanvasElement>) {
    const stage = stageRef.current;
    if (!stage) return { x: 0, y: 0 };
    const rect = stage.getBoundingClientRect();
    const current = boardRef.current;
    return { x: (event.clientX - rect.left - current.viewport.x) / current.viewport.zoom, y: (event.clientY - rect.top - current.viewport.y) / current.viewport.zoom };
  }

  function releasePointer(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function eraseAt(point: { x: number; y: number }) {
    const gesture = erasingRef.current;
    if (!gesture) return;
    const current = boardRef.current;
    const radius = 22 / Math.max(0.1, current.viewport.zoom);
    const nextStrokes = current.strokes.filter((stroke) => !stroke.points.some((item) => Math.hypot(item.x - point.x, item.y - point.y) < radius));
    if (nextStrokes.length === current.strokes.length) return;
    gesture.changed = true;
    publishBoard({ ...current, strokes: nextStrokes });
  }

  function onPointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (disabled || !active || event.button !== 0) return;
    stageRef.current?.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    const current = boardRef.current;
    if (tool === "pan") {
      panRef.current = { x: event.clientX, y: event.clientY, viewport: { x: current.viewport.x, y: current.viewport.y } };
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
      id: "stroke-" + sessionId + "-" + Date.now() + "-" + (++strokeCounterRef.current),
      kind: style.kind,
      points: [point],
      color: style.color,
      width: style.width,
      opacity: strokeOpacityForPreference(style.kind, tool === "marker" ? toolOpacities.marker : toolOpacities.pen),
    };
    drawingStartRef.current = { x: point.x, y: point.y, straight: (tool === "pen" || tool === "marker") && event.shiftKey };
    scheduleRender();
  }

  function onPointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (!active) return;
    if (panRef.current) {
      const pan = panRef.current;
      publishBoard({ ...boardRef.current, viewport: { ...boardRef.current.viewport, x: pan.viewport.x + event.clientX - pan.x, y: pan.viewport.y + event.clientY - pan.y } });
      return;
    }
    if (disabled) return;
    if (erasingRef.current) { eraseAt(toWorld(event)); return; }
    if (!drawingRef.current) return;
    const point = toWorld(event);
    const drawing = drawingRef.current;
    const drawingStart = drawingStartRef.current;
    if (drawingStart && (drawing.kind === "freehand" || drawing.kind === "marker")) {
      if (event.shiftKey) drawingStart.straight = true;
      drawing.points = drawingStart.straight ? [{ x: drawingStart.x, y: drawingStart.y }, point] : [...drawing.points, point];
    } else if (drawing.kind === "line" || drawing.kind === "rect" || drawing.kind === "ellipse") {
      drawing.points = [drawing.points[0] ?? point, point];
    } else {
      drawing.points = [...drawing.points, point];
    }
    scheduleRender();
  }

  function commitCurrentStroke(): void {
    const stroke = drawingRef.current;
    drawingRef.current = null;
    drawingStartRef.current = null;
    if (!stroke || !stroke.points.length) return;
    const current = boardRef.current;
    commit({ ...current, strokes: [...current.strokes, stroke] });
  }

  function onPointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (panRef.current) {
      panRef.current = null;
      releasePointer(event);
      void drainSaves().catch(() => undefined);
      return;
    }
    if (erasingRef.current) {
      const gesture = erasingRef.current;
      erasingRef.current = null;
      if (gesture.changed) {
        setHistory((items) => [...items.slice(-29), gesture.before]);
        setRedoHistory([]);
        queuePersist(boardRef.current);
      }
      releasePointer(event);
      return;
    }
    if (!disabled && active) commitCurrentStroke();
    else if (drawingRef.current) flushTransientEdits();
    releasePointer(event);
    scheduleRender();
  }

  function onPointerCancel(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (panRef.current) panRef.current = null;
    if (erasingRef.current?.changed) queuePersist(boardRef.current);
    erasingRef.current = null;
    if (drawingRef.current) flushTransientEdits();
    drawingStartRef.current = null;
    releasePointer(event);
    scheduleRender();
  }

  function addText(): void {
    const draft = textDraftRef.current;
    const value = textValueRef.current.trim();
    textDraftRef.current = null;
    textValueRef.current = "";
    setTextDraft(null);
    setTextValue("");
    if (!draft || !value) return;
    const current = boardRef.current;
    commit({ ...current, texts: [...current.texts, { id: "text-" + sessionId + "-" + Date.now(), x: draft.x, y: draft.y, text: value }] });
  }

  function zoom(delta: number): void {
    const current = boardRef.current;
    const nextZoom = Math.max(0.35, Math.min(2.5, current.viewport.zoom + delta));
    commit({ ...current, viewport: { ...current.viewport, zoom: Number(nextZoom.toFixed(2)) } });
  }

  function resetViewport(): void { commit({ ...boardRef.current, viewport: { x: 0, y: 0, zoom: 1 } }); }

  function commit(next: BoardDocument): void {
    setHistory((items) => [...items.slice(-29), cloneBoard(boardRef.current)]);
    setRedoHistory([]);
    publishBoard(next);
  }

  function undo(): void {
    if (disabled) return;
    const previous = history.at(-1);
    if (!previous) return;
    setHistory((items) => items.slice(0, -1));
    setRedoHistory((items) => [...items.slice(-29), cloneBoard(boardRef.current)]);
    publishBoard(previous);
  }

  function redo(): void {
    if (disabled) return;
    const next = redoHistory.at(-1);
    if (!next) return;
    setRedoHistory((items) => items.slice(0, -1));
    setHistory((items) => [...items.slice(-29), cloneBoard(boardRef.current)]);
    publishBoard(next);
  }

  function clearBoard(): void {
    const current = boardRef.current;
    if (!current.strokes.length && !current.texts.length && !(current.images ?? []).length) return;
    setClearConfirmOpen(true);
  }

  function confirmClearBoard(): void {
    commit({ ...boardRef.current, strokes: [], texts: [], images: [] });
    setClearConfirmOpen(false);
  }

  function restoreTemporaryPan(): void {
    const previous = temporaryPanRef.current;
    if (!previous) return;
    temporaryPanRef.current = null;
    setTool((current) => current === "pan" ? previous : current);
  }

  function onBoardKeyDown(event: ReactKeyboardEvent<HTMLDivElement>): void {
    if (disabled || !active || event.defaultPrevented || isTypingTarget(event.target)) return;
    if (isBoardSpaceKey(event)) {
      event.preventDefault();
      if (!event.repeat) {
        temporaryPanRef.current = tool;
        setTool("pan");
      }
    }
  }

  function onBoardKeyUp(event: ReactKeyboardEvent<HTMLDivElement>): void {
    if (!active || isTypingTarget(event.target) || !isBoardSpaceKey(event)) return;
    event.preventDefault();
    restoreTemporaryPan();
  }

  function onBoardBlur(event: ReactFocusEvent<HTMLDivElement>): void {
    const nextFocus = event.relatedTarget as Node | null;
    if (!nextFocus || !event.currentTarget.contains(nextFocus)) {
      setBoardFocused(false);
      restoreTemporaryPan();
    }
  }

  const shortcutActions: BoardShortcutActions = active && boardFocused && !disabled && !clearConfirmOpen ? {
    pen: () => setTool("pen"),
    marker: () => setTool("marker"),
    pan: () => setTool("pan"),
    line: () => setTool("line"),
    rect: () => setTool("rect"),
    ellipse: () => setTool("ellipse"),
    text: () => setTool("text"),
    eraser: () => setTool("eraser"),
    undo,
    redo,
    "zoom.in": () => zoom(0.1),
    "zoom.out": () => zoom(-0.1),
    "zoom.reset": resetViewport,
    clear: clearBoard,
  } : {};
  useBoardShortcuts(active && boardFocused && !disabled && !clearConfirmOpen, shortcutActions);

  function selectColor(color: string): void {
    if (tool === "marker") {
      setToolColors((current) => ({ ...current, marker: color }));
      setBoardPreferences({ boardMarkerColor: color });
    } else {
      setToolColors((current) => ({ ...current, pen: color }));
      setBoardPreferences({ boardColor: color });
    }
    setColorOpen(false);
  }

  function selectWidth(value: string): void {
    const width = Number(value);
    if (!Number.isFinite(width) || width < 1 || width > 24) return;
    if (tool === "marker") {
      setToolWidths((current) => ({ ...current, marker: width }));
      setBoardPreferences({ boardMarkerWidth: width });
    } else {
      setToolWidths((current) => ({ ...current, pen: width }));
      setBoardPreferences({ boardWidth: width });
    }
  }

  function selectOpacity(value: string): void {
    const opacity = Number(value);
    if (!Number.isFinite(opacity) || opacity < 0.1 || opacity > 1) return;
    if (tool === "marker") {
      setToolOpacities((current) => ({ ...current, marker: opacity }));
      setBoardPreferences({ boardMarkerOpacity: opacity });
    } else {
      setToolOpacities((current) => ({ ...current, pen: opacity }));
      setBoardPreferences({ boardOpacity: opacity });
    }
  }

  function selectBackground(value: BoardBackground): void {
    setBoardPreferences({ boardBackground: value });
    setBoardBackground(value);
  }

  useEffect(() => {
    const assetId = importImage?.assetId?.trim();
    const revisionHash = importImage?.revisionHash?.trim();
    const requestId = importImage?.requestId?.trim();
    if (!active || disabled || !assetId || !requestId) return undefined;
    const token = ++imageRequestRef.current;
    const controller = new AbortController();
    let disposed = false;
    setImageBusy(true);
    setImageError(null);
    void (async () => {
      try {
        const key = boardImageKey({ asset_id: assetId, revision_hash: revisionHash ?? "" });
        let cached = imageCacheRef.current.get(key);
        if (!cached) {
          cached = await loadBoardImage(assetId, controller.signal);
          if (disposed || token !== imageRequestRef.current) {
            URL.revokeObjectURL(cached.objectUrl);
            return;
          }
          imageCacheRef.current.set(key, cached);
          setImageRenderVersion((value) => value + 1);
        }
        const current = boardRef.current;
        if (!(current.images ?? []).some((image) => image.asset_id === assetId && image.revision_hash === (revisionHash ?? ""))) {
          const size = fitBoardImage(current, cached.image.naturalWidth, cached.image.naturalHeight, stageRef.current);
          commit({ ...current, images: [...(current.images ?? []), { id: "image-" + sessionId + "-" + requestId, asset_id: assetId, revision_hash: revisionHash ?? "", ...size }] });
        }
      } catch (reason) {
        if (!disposed && token === imageRequestRef.current) setImageError(readableError(reason, "Иллюстрацию не удалось загрузить. Повторите попытку."));
      } finally {
        if (!disposed && token === imageRequestRef.current) setImageBusy(false);
      }
    })();
    return () => { disposed = true; controller.abort(); };
  }, [active, disabled, imageRetry, importImage?.assetId, importImage?.requestId, importImage?.revisionHash]);

  const paletteTool = tool === "marker" ? "marker" : "pen";
  const activeColor = toolColors[paletteTool];
  const activeWidth = toolWidths[paletteTool];
  const activeOpacity = toolOpacities[paletteTool];
  const controlsDisabled = disabled || !active;
  const stageClassName = [
    "board-stage", "tool-" + tool, styles.stage,
    tool === "pan" ? styles.stagePan : "", tool === "text" ? styles.stageText : "", tool === "eraser" ? styles.stageEraser : "",
  ].filter(Boolean).join(" ");

  return (
    <div className={["board-shell", styles.shell].join(" ")}>
      <div className={["board-toolbar", styles.toolbar].join(" ")} role="toolbar" aria-label="Инструменты доски">
        <div className={styles.toolRow}>
          <div className={styles.toolGroup} role="group" aria-label="Инструменты рисования">
            {TOOLS.map((item) => (
              <button aria-label={item.label} aria-pressed={tool === item.key} className={["tool-button", styles.toolButton, tool === item.key ? "is-selected " + styles.selected : ""].filter(Boolean).join(" ")} disabled={controlsDisabled} key={item.key} onClick={() => setTool(item.key)} title={item.label} type="button">
                <Icon name={item.icon} size={19} /><span className={styles.toolLabel}>{item.label}</span>
              </button>
            ))}
          </div>
          <span className={["toolbar-divider", styles.divider].join(" ")} />
          <div className={styles.historyTools} role="group" aria-label="История действий">
            <button aria-label="Отменить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled || !history.length} onClick={undo} title="Отменить" type="button"><Icon name="undo" size={16} /></button>
            <button aria-label="Повторить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled || !redoHistory.length} onClick={redo} title="Повторить" type="button"><Icon name="redo" size={16} /></button>
          </div>
          <span className={["toolbar-divider", styles.divider].join(" ")} />
          <div className={styles.zoomTools} role="group" aria-label="Масштаб доски">
            <button aria-label="Уменьшить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={() => zoom(-0.1)} title="Уменьшить" type="button"><Icon name="zoom-out" size={16} /></button>
            <span className={["zoom-label", styles.zoomLabel].join(" ")}>{Math.round(board.viewport.zoom * 100)}%</span>
            <button aria-label="Увеличить" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={() => zoom(0.1)} title="Увеличить" type="button"><Icon name="zoom-in" size={16} /></button>
            <button aria-label="Вернуть масштаб и положение доски" title="Вернуть масштаб и положение" className={["tool-button", styles.toolButton].join(" ")} disabled={controlsDisabled} onClick={resetViewport} type="button"><Icon name="maximize" size={16} /></button>
          </div>
        </div>
        <div className={styles.propertyRow}>
          <div className={styles.colorControl}>
            <button aria-expanded={colorOpen} aria-haspopup="true" aria-label={"Цвет " + (paletteTool === "marker" ? "маркера" : "пера")} className={styles.propertyButton} disabled={controlsDisabled} onClick={() => setColorOpen((open) => !open)} type="button">
              <span className={styles.currentSwatch} style={{ backgroundColor: activeColor }} /><span>Цвет</span>
            </button>
            {colorOpen ? <div className={styles.colorPopover} role="group" aria-label="Палитра цветов">
              {PALETTE.map((item) => <button aria-label={item.label} aria-pressed={activeColor === item.value} className={[styles.swatch, activeColor === item.value ? styles.swatchSelected : ""].filter(Boolean).join(" ")} disabled={controlsDisabled} key={item.value} onClick={() => selectColor(item.value)} style={{ backgroundColor: item.value }} title={item.label} type="button" />)}
            </div> : null}
          </div>
          <label className={styles.thicknessControl}><span>Толщина штриха</span><input aria-label="Толщина штриха" className={styles.thicknessRange} disabled={controlsDisabled} max={24} min={1} onChange={(event) => selectWidth(event.target.value)} step={1} type="range" value={activeWidth} /><output className={styles.thicknessOutput}>{activeWidth}</output></label>
          <label className={styles.opacityControl}><span>Непрозрачность</span><input aria-label="Непрозрачность штриха" className={styles.opacityRange} disabled={controlsDisabled} max={1} min={0.1} onChange={(event) => selectOpacity(event.target.value)} step={0.05} type="range" value={activeOpacity} /><output className={styles.opacityOutput}>{Math.round(activeOpacity * 100)}%</output></label>
          <div className={styles.backgroundControl} role="group" aria-label="Фон доски">
            {BACKGROUNDS.map((item) => <button aria-pressed={boardBackground === item.value} className={[styles.backgroundButton, boardBackground === item.value ? styles.backgroundSelected : ""].filter(Boolean).join(" ")} disabled={controlsDisabled} key={item.value} onClick={() => selectBackground(item.value)} type="button">{item.label}</button>)}
          </div>
          <button className={["board-clear", styles.clearButton].join(" ")} disabled={controlsDisabled} onClick={clearBoard} type="button">Очистить</button>
        </div>
      </div>
      <Modal open={clearConfirmOpen} onClose={() => setClearConfirmOpen(false)} title="Очистить доску?">
        <p>Штрихи, текст и вставленные иллюстрации будут убраны из текущего черновика.</p>
        <div className="kontur-modal-actions">
          <button className="outline-button" onClick={() => setClearConfirmOpen(false)} type="button">Отмена</button>
          <button className="primary-button" onClick={confirmClearBoard} type="button">Очистить</button>
        </div>
      </Modal>
      <div aria-label="Бесконечная доска" className={stageClassName} ref={stageRef} tabIndex={controlsDisabled ? -1 : 0} onBlur={onBoardBlur} onDoubleClick={() => { if (!controlsDisabled) setTool("pan"); }} onFocus={() => setBoardFocused(true)} onKeyDown={onBoardKeyDown} onKeyUp={onBoardKeyUp}>
        <canvas aria-label="Бесконечная доска" className={["board-canvas", styles.canvas].join(" ")} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerCancel} ref={canvasRef} />
        <div className={["board-text-layer", styles.textLayer].join(" ")}>
          {board.texts.map((item) => <span className={["board-text-object", styles.textObject].join(" ")} key={item.id} style={{ left: board.viewport.x + item.x * board.viewport.zoom, top: board.viewport.y + item.y * board.viewport.zoom, transform: "translateY(-100%)" }}>{item.text}</span>)}
        </div>
        {textDraft ? <div className={["board-text-editor", styles.textEditor].join(" ")} style={{ left: board.viewport.x + textDraft.x * board.viewport.zoom, top: board.viewport.y + textDraft.y * board.viewport.zoom }}>
          <input autoFocus aria-label="Текст на доске" disabled={disabled} onChange={(event) => { textValueRef.current = event.target.value; setTextValue(event.target.value); }} onKeyDown={(event) => { if (event.key === "Enter") addText(); if (event.key === "Escape") { textDraftRef.current = null; setTextDraft(null); } }} onBlur={addText} placeholder="Введите текст" value={textValue} />
        </div> : null}
      </div>
      {imageBusy ? <p className={styles.imageStatus} role="status">Загружаем иллюстрацию…</p> : null}
      {imageError ? <div className={styles.imageStatusError} role="alert"><span>{imageError}</span><button onClick={() => { setImageError(null); setImageRetry((value) => value + 1); }} type="button">Повторить</button></div> : null}
      {saveState === "saving" ? <div className={["board-status", styles.status].join(" ")} role="status"><span className={["status-dot", styles.statusDot].join(" ")} />Сохраняем…</div> : null}
      {saveState === "error" && saveError ? <div className={["board-status", styles.status, styles.statusError].join(" ")} role="alert"><span className={["status-dot", styles.statusDot].join(" ")} /><span>Не удалось сохранить изменения</span><span className={["board-error", styles.error].join(" ")}>{saveError}</span><button className="outline-button" onClick={() => void flushSaves().catch(() => undefined)} type="button">Повторить</button></div> : null}
    </div>
  );
}
