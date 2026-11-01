"use client";

import type { FocusEvent as ReactFocusEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "@/components/icon";
import { compactStrokePoints, drawSmoothRoundStroke } from "@/lib/stroke-smoothing";
import { createInkLayoutAnchor, hitTestInkStroke, inkSweepPoints, INK_STROKE_WIDTH, projectInkPoints, type InkFrame, type InkLayoutAnchor } from "@/lib/statement-ink";
import { getPreferences, PREFERENCES_CHANGED } from "@/lib/kontur-preferences";
import type { Annotation } from "@/lib/types";
import {readLocalDraft,saveLocalDraft,clearLocalDraft,isLocalDraftDurable} from "@/lib/local-drafts";
import {registerDraftFlush,registerDraftSaveState} from "@/lib/draft-lifecycle";
import styles from "./statement-canvas.module.css";

type InkTool = "pen" | "marker" | "eraser";
type NormalizedPoint = { x: number; y: number };
type PendingSave = { annotation: Annotation; status: "saving" | "error" };
type DeleteState = { annotation: Annotation; status: "deleting" | "error" };
type InkHistoryEntry = { kind: "add" | "remove"; annotations: Annotation[] };
type InkRecord = {
  key: string;
  annotation: Annotation;
  revisionHash: string;
  desired: "present" | "absent";
  serverId: string | null;
  attemptedCreate: boolean;
  error: string | null;
  aliases: Set<string>;
};
type InkStyle = { width: number; opacity: number };
type InkAnchorDraft = InkLayoutAnchor;

const PALETTE = [
  { value: "#1d1d1f", label: "Графит" },
  { value: "#0066cc", label: "Синий" },
  { value: "#d92d20", label: "Красный" },
  { value: "#16803c", label: "Зелёный" },
  { value: "#a15c00", label: "Янтарный" },
] as const;

const ERASER_TOLERANCE = 22;

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function normalizedPoint(event: ReactPointerEvent<HTMLCanvasElement>, host: HTMLDivElement): NormalizedPoint {
  const rect = host.getBoundingClientRect();
  return {
    x: rect.width ? (event.clientX - rect.left) / rect.width : 0,
    y: rect.height ? (event.clientY - rect.top) / rect.height : 0,
  };
}

function safeCanvasColor(value: string): string {
  return /^#[0-9a-f]{3,8}$/i.test(value) ? value : "#1d1d1f";
}

function visibleInk(annotations: Annotation[], revisionHash: string): Annotation[] {
  return annotations.filter((annotation) => annotation.kind === "ink" && annotation.anchor?.content_hash === revisionHash);
}

function usablePoints(annotation: Annotation): NormalizedPoint[] {
  return (annotation.points ?? [])
    .filter((point): point is NormalizedPoint => Boolean(point && Number.isFinite(point.x) && Number.isFinite(point.y)))
    .map((point) => ({ x: clamp(point.x), y: clamp(point.y) }));
}

function readableError(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function newInkId(counter: { value: number }): string {
  counter.value += 1;
  const randomId = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${counter.value}`;
  return `ink-${randomId}`;
}

function isTextEditingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return Boolean(element && typeof element.closest === "function" && element.closest("input, textarea, select, [contenteditable='true'], [data-code-editor='true']"));
}

function uniqueAnnotations(records: Iterable<InkRecord>): Annotation[] {
  const seen = new Set<string>();
  const result: Annotation[] = [];
  for (const record of records) {
    if (record.desired !== "present") continue;
    const annotation = record.annotation;
    if (seen.has(annotation.id)) continue;
    seen.add(annotation.id);
    result.push(annotation);
  }
  return result;
}

function statementTextRoot(host: HTMLDivElement): HTMLElement {
  return host.querySelector<HTMLElement>(".statement-body") ?? host;
}

function textNodes(root: Node): Text[] {
  const document = root.ownerDocument;
  if (!document) return [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const result: Text[] = [];
  let node = walker.nextNode();
  while (node) {
    if (node.nodeValue) result.push(node as Text);
    node = walker.nextNode();
  }
  return result;
}

function absoluteTextOffset(root: Node, target: Node, targetOffset: number): number | null {
  let offset = 0;
  for (const node of textNodes(root)) {
    if (node === target) return offset + Math.max(0, Math.min(targetOffset, node.nodeValue?.length ?? 0));
    offset += node.nodeValue?.length ?? 0;
  }
  return null;
}

function textOffsetAtPoint(root: HTMLElement, clientX: number, clientY: number): number | null {
  const document = root.ownerDocument;
  const caret = document.caretRangeFromPoint?.(clientX, clientY);
  if (caret && root.contains(caret.startContainer)) return absoluteTextOffset(root, caret.startContainer, caret.startOffset);
  const position = document.caretPositionFromPoint?.(clientX, clientY);
  if (position && root.contains(position.offsetNode)) return absoluteTextOffset(root, position.offsetNode, position.offset);
  return null;
}

function textPointAtOffset(root: HTMLElement, offset: number, hostRect: DOMRect): NormalizedPoint | null {
  const nodes = textNodes(root);
  let remaining = Math.max(0, offset);
  for (const node of nodes) {
    const length = node.nodeValue?.length ?? 0;
    if (remaining > length) {
      remaining -= length;
      continue;
    }
    const localOffset = Math.min(remaining, length);
    const range = root.ownerDocument.createRange();
    const start = localOffset === length && localOffset > 0 ? localOffset - 1 : localOffset;
    const end = localOffset === length && localOffset > 0 ? localOffset : Math.min(length, localOffset + 1);
    range.setStart(node, start);
    range.setEnd(node, end);
    const rect = range.getClientRects()[0] ?? range.getBoundingClientRect();
    if (rect.width || rect.height || rect.top || rect.left) {
      return { x: rect.left - hostRect.left, y: rect.top - hostRect.top + rect.height / 2 };
    }
    return null;
  }
  return null;
}

export function StatementCanvas({
  recoveryScope,
  children,
  annotations,
  revisionHash,
  disabled = false,
  onInk,
  onDeleteInk,
}: {
  recoveryScope?: string;
  children: ReactNode;
  annotations: Annotation[];
  revisionHash: string;
  disabled?: boolean;
  onInk: (annotation: Annotation) => Promise<Annotation>;
  onDeleteInk: (annotationId: string) => Promise<void>;
}) {
  const [enabled, setEnabled] = useState(false);
  const [tool, setTool] = useState<InkTool>("pen");
  const [toolColors, setToolColors] = useState({ pen: PALETTE[0].value, marker: "#f2c94c" });
  const [colorOpen, setColorOpen] = useState(false);
  const [palettePosition, setPalettePosition] = useState({ left: 8, top: 8 });
  const colorButtonRef = useRef<HTMLButtonElement | null>(null);
  const paletteRef = useRef<HTMLDivElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [hasDraft, setHasDraft] = useState(false);
  const [localInk, setLocalInk] = useState<Annotation[]>(() => visibleInk(annotations, revisionHash));
  const [pendingSave, setPendingSave] = useState<PendingSave | null>(null);
  const [deleteState, setDeleteState] = useState<DeleteState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drawingSmoothing, setDrawingSmoothing] = useState(() => getPreferences().drawingSmoothing);
  const [brushWidths, setBrushWidths] = useState({ pen: INK_STROKE_WIDTH.pen, marker: INK_STROKE_WIDTH.marker });
  const [brushOpacities, setBrushOpacities] = useState({ pen: 0.95, marker: 0.38 });
  const [history, setHistory] = useState<InkHistoryEntry[]>([]);
  const [redoHistory, setRedoHistory] = useState<InkHistoryEntry[]>([]);
  const contentRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const eraserCursorRef = useRef<HTMLDivElement>(null);
  const erasingRef = useRef<Annotation[] | null>(null);
  const eraserLastPointRef = useRef<NormalizedPoint | null>(null);
  const drawingRef = useRef<NormalizedPoint[]>([]);
  const inkAnchorRef = useRef<InkAnchorDraft | null>(null);
  const activeToolRef = useRef<InkTool>("pen");
  const straightRef = useRef(false);
  const pointerIdRef = useRef<number | null>(null);
  const inkCounterRef = useRef({ value: 0 });
  const revisionHashRef = useRef(revisionHash);
  const syncedRevisionRef = useRef<string | null>(null);
  const recordsRef = useRef<Map<string, InkRecord>>(new Map());
  const localInkRef = useRef(localInk);
  const historyRef = useRef<InkHistoryEntry[]>([]);
  const redoHistoryRef = useRef<InkHistoryEntry[]>([]);
  const frameRef = useRef<Map<string, InkFrame>>(new Map());
  const styleRef = useRef<Map<string, InkStyle>>(new Map());
  const redrawFrameRef = useRef<number | null>(null);
  const drawCanvasRef = useRef<() => void>(() => {});
  const workerRef = useRef<Promise<void> | null>(null);
  const onInkRef = useRef(onInk);
  const onDeleteInkRef = useRef(onDeleteInk);

  useEffect(() => {
    const syncPreferences = () => setDrawingSmoothing(getPreferences().drawingSmoothing);
    window.addEventListener(PREFERENCES_CHANGED, syncPreferences);
    window.addEventListener("storage", syncPreferences);
    return () => {
      window.removeEventListener(PREFERENCES_CHANGED, syncPreferences);
      window.removeEventListener("storage", syncPreferences);
    };
  }, []);

  const savedInk = useMemo(() => visibleInk(annotations, revisionHash), [annotations, revisionHash]);
  const currentColor = tool === "marker" ? toolColors.marker : toolColors.pen;
  const currentBrushStyle = tool === "marker"
    ? { width: brushWidths.marker, opacity: brushOpacities.marker }
    : { width: brushWidths.pen, opacity: brushOpacities.pen };
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  const currentColorRef = useRef(currentColor);
  currentColorRef.current = currentColor;
  const currentBrushStyleRef = useRef(currentBrushStyle);
  currentBrushStyleRef.current = currentBrushStyle;
  const transientOperation = pendingSave?.status === "saving" || deleteState?.status === "deleting";
  const busy = transientOperation;

  useEffect(() => {
    onInkRef.current = onInk;
    onDeleteInkRef.current = onDeleteInk;
  }, [onDeleteInk, onInk]);

  useEffect(() => {
    const revisionChanged = syncedRevisionRef.current !== revisionHash;
    syncedRevisionRef.current = revisionHash;
    revisionHashRef.current = revisionHash;
    if (revisionChanged) {
      historyRef.current = [];
      redoHistoryRef.current = [];
      setHistory([]);
      setRedoHistory([]);
      drawingRef.current = [];
      inkAnchorRef.current = null;
      activeToolRef.current = "pen";
      straightRef.current = false;
      pointerIdRef.current = null;
      erasingRef.current = null;
      eraserCursorRef.current?.classList.remove(styles.eraserCursorVisible);
      setHasDraft(false);
      setPendingSave(null);
      setDeleteState(null);
      setError(null);
      setColorOpen(false);
      frameRef.current.clear();
      styleRef.current.clear();
    }
    const incoming = visibleInk(annotations, revisionHash);
    const records = recordsRef.current;
    const incomingIds=new Set(incoming.map(item=>item.id));
    if(!workerRef.current)for(const [key,record] of records){
      if(record.revisionHash===revisionHash&&record.serverId&&record.desired==="present"&&!incomingIds.has(record.serverId))records.delete(key);
    }
    for (const annotation of incoming) {
      let record = [...records.values()].find((item) => item.serverId === annotation.id || item.annotation.id === annotation.id);
      if (!record) {
        record = {
          key: annotation.id,
          annotation,
          revisionHash,
          desired: "present",
          serverId: annotation.id,
          attemptedCreate: true,
          error: null,
          aliases: new Set<string>(),
        };
        records.set(record.key, record);
      } else if (record.desired === "present" && record.serverId === annotation.id) {
        record.annotation = annotation;
        record.error = null;
      }
      const ink = annotation.anchor?.ink;
      const fallbackFrame = { width: contentRef.current?.getBoundingClientRect().width ?? 1, height: contentRef.current?.getBoundingClientRect().height ?? 1 };
      frameRef.current.set(annotation.id, ink?.frame ?? fallbackFrame);
      if (ink?.style) styleRef.current.set(annotation.id, ink.style);
    }
    localInkRef.current = uniqueAnnotations([...records.values()].filter((item) => item.revisionHash === revisionHash));
    setLocalInk(localInkRef.current);
  }, [annotations, revisionHash]);

  const publishLocal = useCallback(() => {
    const next = uniqueAnnotations([...recordsRef.current.values()].filter((item) => item.revisionHash === revisionHashRef.current));
    localInkRef.current = next;
    setLocalInk(next);
    if(recoveryScope){
      const pending=[...recordsRef.current.values()].filter(item=>item.revisionHash===revisionHashRef.current&&
        (item.desired==="present"?!item.serverId:Boolean(item.serverId)||item.attemptedCreate));
      if(pending.length)saveLocalDraft(recoveryScope,"ink",0,pending.map(item=>({...item,aliases:[...item.aliases]})));
      else clearLocalDraft(recoveryScope,"ink");
    }
  }, [recoveryScope]);

  const findRecord = useCallback((annotation: Annotation): InkRecord | undefined => {
    return [...recordsRef.current.values()].find((item) => item.annotation.id === annotation.id || item.serverId === annotation.id || item.key === annotation.id || item.aliases.has(annotation.id));
  }, []);

  const ensureRecord = useCallback((annotation: Annotation): InkRecord => {
    const existing = findRecord(annotation);
    if (existing) return existing;
    const record: InkRecord = {
      key: annotation.id,
      annotation,
      revisionHash: annotation.anchor?.content_hash ?? revisionHashRef.current,
      desired: "present",
      serverId: null,
      attemptedCreate: false,
      error: null,
      aliases: new Set<string>(),
    };
    recordsRef.current.set(record.key, record);
    return record;
  }, [findRecord]);

  const drawCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    const host = contentRef.current;
    if (!canvas || !host) return;
    const rect = host.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    const dpr = window.devicePixelRatio || 1;
    const pixelWidth = Math.max(1, Math.round(width * dpr));
    const pixelHeight = Math.max(1, Math.round(height * dpr));
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(pixelWidth / width, 0, 0, pixelHeight / height, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    context.lineCap = "round";
    context.lineJoin = "round";
    const replayPoints = (annotation: Annotation, points: NormalizedPoint[]): NormalizedPoint[] => {
      const ink = annotation.anchor?.ink;
      if (!ink?.char || !ink.frame || !contentRef.current) return points;
      const anchorPoint = textPointAtOffset(statementTextRoot(contentRef.current), ink.char.offset, rect);
      if (!anchorPoint) return points;
      return projectInkPoints(points, ink, anchorPoint, rect);
    };
    const paint = (annotation: Annotation, points: NormalizedPoint[], draftStyle?: InkStyle) => {
      if (!points.length) return;
      const frame = frameRef.current.get(annotation.id) ?? { width: rect.width, height: rect.height };
      frameRef.current.set(annotation.id, frame);
      const pixelPoints = replayPoints(annotation, points).map((point) => ({ x: point.x * rect.width, y: point.y * rect.height }));
      const style = draftStyle ?? styleRef.current.get(annotation.id) ?? annotation.anchor?.ink?.style ?? (annotation.text === "marker"
        ? { width: INK_STROKE_WIDTH.marker, opacity: 0.38 }
        : { width: INK_STROKE_WIDTH.pen, opacity: 0.95 });
      context.save();
      context.strokeStyle = safeCanvasColor(annotation.color);
      context.globalAlpha = style.opacity;
      context.lineWidth = style.width;
      drawSmoothRoundStroke(context, pixelPoints, drawingSmoothing);
      context.restore();
    };
    localInkRef.current.forEach((annotation) => paint(annotation, usablePoints(annotation)));
    if (drawingRef.current.length) {
      const draft: Annotation = { id: "draft", kind: "ink", color: currentColor, text: activeToolRef.current === "marker" ? "marker" : undefined, points: drawingRef.current };
      paint(draft, drawingRef.current, currentBrushStyle);
    }
  }, [currentBrushStyle, currentColor, drawingSmoothing, localInk]);

  drawCanvasRef.current = drawCanvas;

  const scheduleRedraw = useCallback(() => {
    if (redrawFrameRef.current !== null) return;
    if (typeof requestAnimationFrame === "undefined") {
      drawCanvasRef.current();
      return;
    }
    redrawFrameRef.current = requestAnimationFrame(() => {
      redrawFrameRef.current = null;
      drawCanvasRef.current();
    });
  }, []);

  const publishRecord = useCallback((record: InkRecord, desired: "present" | "absent") => {
    record.desired = desired;
    record.error = null;
    if (desired === "absent" && !record.serverId && !record.attemptedCreate) recordsRef.current.delete(record.key);
    publishLocal();
  }, [publishLocal]);

  const pumpOutbox = useCallback((): Promise<void> => {
    if (workerRef.current) return workerRef.current;
    const operation = (async () => {
      while (true) {
        const record = [...recordsRef.current.values()].find((item) => {
          if (item.error) return false;
          if (item.desired === "present") return !item.serverId;
          return Boolean(item.serverId) || item.attemptedCreate;
        });
        if (!record) break;
        const isCurrentRevision = record.revisionHash === revisionHashRef.current;
        if (record.desired === "present" || !record.serverId) {
          const annotation = record.annotation;
          record.attemptedCreate = true;
          if (isCurrentRevision) setPendingSave({ annotation, status: "saving" });
          try {
            const onInk = onInkRef.current;
            const saved = await onInk(annotation);
            if (!saved || typeof saved.id !== "string" || !saved.id) throw new Error("Пометку не удалось подтвердить на сервере.");
            if (saved.id !== annotation.id) {
              record.aliases.add(annotation.id);
              frameRef.current.set(saved.id, saved.anchor?.ink?.frame ?? frameRef.current.get(annotation.id) ?? { width: contentRef.current?.getBoundingClientRect().width ?? 1, height: contentRef.current?.getBoundingClientRect().height ?? 1 });
              const style = saved.anchor?.ink?.style ?? styleRef.current.get(annotation.id);
              if (style) styleRef.current.set(saved.id, style);
            }
            if (saved.anchor?.ink?.frame) frameRef.current.set(saved.id, saved.anchor.ink.frame);
            if (saved.anchor?.ink?.style) styleRef.current.set(saved.id, saved.anchor.ink.style);
            record.annotation = saved;
            record.serverId = saved.id;
            record.error = null;
            publishLocal();
            if (isCurrentRevision) setPendingSave(null);
          } catch (reason: unknown) {
            record.error = readableError(reason, "Пометку не удалось сохранить.");
            if (isCurrentRevision) {
              setPendingSave({ annotation, status: "error" });
              setError(record.error);
            }
            continue;
          }
        } else if (record.serverId) {
          const annotation = record.annotation;
          const serverId = record.serverId;
          if (isCurrentRevision) setDeleteState({ annotation, status: "deleting" });
          try {
            const onDeleteInk = onDeleteInkRef.current;
            await onDeleteInk(serverId); // onDeleteInk(annotation.id) uses the canonical server id.
            record.aliases.add(serverId);
            record.serverId = null;
            record.attemptedCreate = false;
            record.error = null;
            // Keep the tombstone in memory so undo/redo can recreate the mark
            // with a fresh idempotency key after a confirmed delete.
            const previousClientId = record.annotation.id;
            const nextClientId = newInkId(inkCounterRef.current);
            record.aliases.add(previousClientId);
            record.annotation = { ...record.annotation, id: nextClientId };
            const frame = frameRef.current.get(previousClientId);
            if (frame) frameRef.current.set(nextClientId, frame);
            const style = styleRef.current.get(previousClientId);
            if (style) styleRef.current.set(nextClientId, style);
            publishLocal();
            if (isCurrentRevision) setDeleteState(null);
          } catch (reason: unknown) {
            record.error = readableError(reason, "Пометку не удалось удалить. Повторите попытку.");
            if (isCurrentRevision) {
              setDeleteState({ annotation, status: "error" });
              setError(record.error);
            }
            continue;
          }
        }
      }
      // A later success must not hide an earlier failed record from the same gesture.
      const failed = [...recordsRef.current.values()].filter((item) => item.revisionHash === revisionHashRef.current && item.error);
      const failedSave = failed.find((item) => item.desired === "present");
      const failedDelete = failed.find((item) => item.desired === "absent");
      setPendingSave(failedSave ? { annotation: failedSave.annotation, status: "error" } : null);
      setDeleteState(failedDelete ? { annotation: failedDelete.annotation, status: "error" } : null);
      setError(failedSave?.error ?? failedDelete?.error ?? null);
    })().finally(() => {
      workerRef.current = null;
    });
    workerRef.current = operation;
    void operation.catch(() => undefined);
    return operation;
  }, [publishLocal]);

  async function captureTransientInk(): Promise<void> {
    finishErasing();
    if (!drawingRef.current.length) return;
    if (disabledRef.current) throw new Error("Пометки заблокированы серверной политикой экзамена.");
    const points = compactStrokePoints(drawingRef.current, straightRef.current ? 0 : 0.0005);
    const strokeTool = activeToolRef.current;
    const inkAnchor = inkAnchorRef.current;
    const color = currentColorRef.current;
    const style = currentBrushStyleRef.current;
    drawingRef.current = [];
    inkAnchorRef.current = null;
    activeToolRef.current = "pen";
    straightRef.current = false;
    pointerIdRef.current = null;
    setHasDraft(false);
    scheduleRedraw();
    if (!points.length || strokeTool === "eraser") return;
    const annotation: Annotation = {
      id: newInkId(inkCounterRef.current),
      kind: "ink",
      text: strokeTool === "marker" ? "marker" : undefined,
      color,
      points: points.map((point) => ({ x: clamp(point.x), y: clamp(point.y) })),
      anchor: {
        start: 0,
        end: 0,
        content_hash: revisionHashRef.current,
        ...(inkAnchor ? {
          ink: {
            frame: inkAnchor.frame,
            origin: inkAnchor.origin,
            ...(inkAnchor.charOffset === null ? {} : { char: { offset: inkAnchor.charOffset } }),
            style,
          },
        } : {}),
      },
    };
    styleRef.current.set(annotation.id, style);
    const rect = contentRef.current?.getBoundingClientRect();
    if (rect) frameRef.current.set(annotation.id, inkAnchor?.frame ?? { width: rect.width, height: rect.height });
    ensureRecord(annotation);
    publishLocal();
    addInkHistory([annotation]);
    await persistInk(annotation);
  }

  useEffect(()=>{
    if(!recoveryScope)return;
    const restored=readLocalDraft<Array<Omit<InkRecord,"aliases">&{aliases:string[]}>>(recoveryScope,"ink")?.content;
    if(!Array.isArray(restored))return;
    for(const item of restored.slice(0,2000)){
      if(item?.revisionHash!==revisionHash||typeof item.key!=="string"||item.annotation?.kind!=="ink"||
        item.annotation.anchor?.content_hash!==revisionHash||!["present","absent"].includes(item.desired))continue;
      const existing=recordsRef.current.get(item.key);
      if(!existing||!existing.serverId)recordsRef.current.set(item.key,{...item,error:null,aliases:new Set(Array.isArray(item.aliases)?item.aliases:[])});
    }
    publishLocal();if(!disabled)void pumpOutbox();
  },[recoveryScope,revisionHash]);

  useEffect(()=>{
    const pending=()=>[...recordsRef.current.values()].some(item=>item.revisionHash===revisionHashRef.current&&
      (item.desired==="present"?!item.serverId:Boolean(item.serverId)||item.attemptedCreate));
    const unstate=registerDraftSaveState(()=>({dirty:pending()||drawingRef.current.length>0,saving:Boolean(workerRef.current),
      conflict:false,offline:typeof navigator!=="undefined"&&!navigator.onLine,
      localRecoverable:!drawingRef.current.length&&(!pending()||Boolean(recoveryScope&&isLocalDraftDurable(recoveryScope,"ink")&&readLocalDraft(recoveryScope,"ink")))}),recoveryScope+":ink");
    const unflush=registerDraftFlush(async()=>{
      if(disabledRef.current&&drawingRef.current.length)throw new Error("Пометки заблокированы серверной политикой экзамена.");
      for(const item of recordsRef.current.values())item.error=null;
      await captureTransientInk();
      await pumpOutbox();publishLocal();
      if(pending()||drawingRef.current.length)throw new Error("Пометки ещё не подтверждены сервером.");
    },recoveryScope+":ink");
    return()=>{unstate();unflush();};
  },[recoveryScope,disabled,pumpOutbox,publishLocal]);

  const addInkHistory = useCallback((annotationsToAdd: Annotation[]) => {
    if (!annotationsToAdd.length) return;
    historyRef.current = [...historyRef.current.slice(-49), { kind: "add", annotations: annotationsToAdd }];
    redoHistoryRef.current = [];
    setHistory(historyRef.current);
    setRedoHistory([]);
  }, []);

  const removeInkHistory = useCallback((annotationsToRemove: Annotation[]) => {
    if (!annotationsToRemove.length) return;
    historyRef.current = [...historyRef.current.slice(-49), { kind: "remove", annotations: annotationsToRemove }];
    redoHistoryRef.current = [];
    setHistory(historyRef.current);
    setRedoHistory([]);
  }, []);

  const setDesiredForAnnotation = useCallback((annotation: Annotation, desired: "present" | "absent") => {
    const record = ensureRecord(annotation);
    if (desired === "present") {
      // A deleted mark may be restored from history. Its old client id is an
      // alias, so retain the regenerated id assigned after the delete.
      if (!record.serverId && !record.aliases.has(annotation.id)) record.annotation = annotation;
      record.revisionHash = annotation.anchor?.content_hash ?? record.revisionHash;
    }
    publishRecord(record, desired);
    void pumpOutbox();
  }, [ensureRecord, pumpOutbox, publishRecord]);

  const undo = useCallback(() => {
    if (disabled) return;
    finishErasing();
    const entry = historyRef.current.at(-1);
    if (!entry) return;
    historyRef.current = historyRef.current.slice(0, -1);
    redoHistoryRef.current = [...redoHistoryRef.current.slice(-49), entry];
    setHistory(historyRef.current);
    setRedoHistory(redoHistoryRef.current);
    const desired = entry.kind === "add" ? "absent" : "present";
    entry.annotations.forEach((annotation) => setDesiredForAnnotation(annotation, desired));
  }, [disabled, setDesiredForAnnotation]);

  const redo = useCallback(() => {
    if (disabled) return;
    finishErasing();
    const entry = redoHistoryRef.current.at(-1);
    if (!entry) return;
    redoHistoryRef.current = redoHistoryRef.current.slice(0, -1);
    historyRef.current = [...historyRef.current.slice(-49), entry];
    setRedoHistory(redoHistoryRef.current);
    setHistory(historyRef.current);
    const desired = entry.kind === "add" ? "present" : "absent";
    entry.annotations.forEach((annotation) => setDesiredForAnnotation(annotation, desired));
  }, [disabled, setDesiredForAnnotation]);

  const persistInk = useCallback(async (annotation: Annotation): Promise<Annotation | null> => {
    const record = ensureRecord(annotation);
    record.desired = "present";
    record.error = null;
    publishLocal();
    if (revisionHashRef.current === record.revisionHash) setPendingSave({ annotation, status: "saving" });
    await pumpOutbox();
    if (record.serverId) return record.annotation;
    return null;
  }, [ensureRecord, pumpOutbox, publishLocal]);

  const deleteInk = useCallback(async (annotation: Annotation, allowPending = false) => {
    if (disabled && !allowPending) return;
    const record = ensureRecord(annotation);
    record.desired = "absent";
    record.error = null;
    publishLocal();
    if (revisionHashRef.current === record.revisionHash) setDeleteState({ annotation, status: "deleting" });
    await pumpOutbox();
  }, [disabled, ensureRecord, pumpOutbox, publishLocal]);

  useEffect(() => {
    void pumpOutbox();
    return () => {
      if (redrawFrameRef.current !== null && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(redrawFrameRef.current);
    };
  }, [pumpOutbox]);

  useLayoutEffect(() => {
    drawCanvas();
  }, [drawCanvas]);

  useEffect(() => {
    scheduleRedraw();
    const host = contentRef.current;
    if (!host || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(scheduleRedraw);
    observer.observe(host);
    return () => observer.disconnect();
  }, [scheduleRedraw]);

  useEffect(() => {
    const redrawOnResize = () => scheduleRedraw();
    window.addEventListener("resize", redrawOnResize);
    return () => window.removeEventListener("resize", redrawOnResize);
  }, [scheduleRedraw]);

  useEffect(() => {
    if (!enabled || disabled) {
      suspendPointer();
    } else if (tool !== "eraser") {
      hideEraserCursor();
      finishErasing();
    }
  }, [disabled, enabled, tool]);

  useEffect(() => {
    const hide = () => suspendPointer();
    const blur = () => suspendPointer();
    document.addEventListener("scroll", hide, true);
    window.addEventListener("blur", blur);
    return () => {
      document.removeEventListener("scroll", hide, true);
      window.removeEventListener("blur", blur);
    };
  }, []);

  useEffect(() => {
    if (!colorOpen) return undefined;
    const frame = requestAnimationFrame(() => paletteRef.current?.querySelector<HTMLButtonElement>("button[aria-pressed='true']")?.focus());
    const closeOutside = (event: Event) => {
      if (!paletteRef.current?.contains(event.target as Node) && !colorButtonRef.current?.contains(event.target as Node)) setColorOpen(false);
    };
    const closeKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setColorOpen(false);
        colorButtonRef.current?.focus();
      }
    };
    const closeViewport = () => setColorOpen(false);
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeKey, true);
    window.addEventListener("resize", closeViewport);
    document.addEventListener("scroll", closeViewport, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeKey, true);
      window.removeEventListener("resize", closeViewport);
      document.removeEventListener("scroll", closeViewport, true);
    };
  }, [colorOpen]);

  function releasePointer(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    pointerIdRef.current = null;
  }

  function cancelDraft() {
    drawingRef.current = [];
    inkAnchorRef.current = null;
    activeToolRef.current = "pen";
    straightRef.current = false;
    releaseActivePointer();
    setHasDraft(false);
    scheduleRedraw();
  }

  function findInkAt(point: NormalizedPoint): Annotation | null {
    const host = contentRef.current;
    if (!host) return null;
    const rect = host.getBoundingClientRect();
    for (let index = localInkRef.current.length - 1; index >= 0; index -= 1) {
      const annotation = localInkRef.current[index];
      const width = styleRef.current.get(annotation.id)?.width ?? (annotation.text === "marker" ? INK_STROKE_WIDTH.marker : INK_STROKE_WIDTH.pen);
      const ink = annotation.anchor?.ink;
      const sourcePoints = usablePoints(annotation);
      const anchorPoint = ink?.char ? textPointAtOffset(statementTextRoot(host), ink.char.offset, rect) : null;
      const renderedPoints = ink && anchorPoint ? projectInkPoints(sourcePoints, ink, anchorPoint, rect) : sourcePoints;
      if (hitTestInkStroke(renderedPoints, point, { width: rect.width, height: rect.height }, width, ERASER_TOLERANCE)) return annotation;
    }
    return null;
  }

  function hideEraserCursor() {
    eraserCursorRef.current?.classList.remove(styles.eraserCursorVisible);
  }

  function updateEraserCursor(event: ReactPointerEvent<HTMLCanvasElement>) {
    const cursor = eraserCursorRef.current;
    const host = contentRef.current;
    if (!cursor || !host || tool !== "eraser" || !enabled || disabled || event.pointerType === "touch") {
      hideEraserCursor();
      return;
    }
    const rect = host.getBoundingClientRect();
    cursor.style.left = `${event.clientX - rect.left}px`;
    cursor.style.top = `${event.clientY - rect.top}px`;
    cursor.classList.add(styles.eraserCursorVisible);
  }

  function finishErasing() {
    const removed = erasingRef.current;
    erasingRef.current = null;
    eraserLastPointRef.current = null;
    if (removed === null) return;
    releaseActivePointer();
    if (removed?.length) removeInkHistory(removed);
  }

  function releaseActivePointer() {
    const pointer = pointerIdRef.current;
    pointerIdRef.current = null;
    try {
      if (pointer !== null && canvasRef.current?.hasPointerCapture(pointer)) canvasRef.current.releasePointerCapture(pointer);
    } catch { /* A detached canvas or an already released pointer is terminal. */ }
  }

  function suspendPointer() {
    hideEraserCursor();
    finishErasing();
    if (drawingRef.current.length) cancelDraft();
    else releaseActivePointer();
  }

  function eraseAt(point: NormalizedPoint) {
    const removed = erasingRef.current;
    if (!removed) return;
    let annotation: Annotation | null;
    while ((annotation = findInkAt(point))) {
      removed.push(annotation);
      void deleteInk(annotation);
    }
  }

  function eraseThrough(point: NormalizedPoint) {
    const rect = contentRef.current?.getBoundingClientRect();
    if (!rect) return;
    for (const sample of inkSweepPoints(eraserLastPointRef.current ?? point, point, rect, ERASER_TOLERANCE / 2)) eraseAt(sample);
    eraserLastPointRef.current = point;
  }

  function selectTool(next: InkTool) {
    finishErasing();
    hideEraserCursor();
    setColorOpen(false);
    setTool(next);
  }

  function onPointerDown(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (disabled || !enabled || event.button !== 0 || !contentRef.current || pointerIdRef.current !== null) return;
    updateEraserCursor(event);
    wrapperRef.current?.focus({ preventScroll: true });
    const point = normalizedPoint(event, contentRef.current);
    if (point.x < 0 || point.x > 1 || point.y < 0 || point.y > 1) return;
    if (tool === "eraser") {
      event.currentTarget.setPointerCapture(event.pointerId);
      pointerIdRef.current = event.pointerId;
      erasingRef.current = [];
      eraseThrough(point);
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerIdRef.current = event.pointerId;
    activeToolRef.current = tool;
    drawingRef.current = [point];
    const rect = contentRef.current.getBoundingClientRect();
    const root = statementTextRoot(contentRef.current);
    const pointerCanvas = event.currentTarget;
    const previousPointerEvents = pointerCanvas.style.pointerEvents;
    pointerCanvas.style.pointerEvents = "none";
    const charOffset = textOffsetAtPoint(root, event.clientX, event.clientY);
    pointerCanvas.style.pointerEvents = previousPointerEvents;
    inkAnchorRef.current = createInkLayoutAnchor(
      { width: Math.max(1, rect.width), height: Math.max(1, rect.height) }, point,
      charOffset === null ? null : textPointAtOffset(root, charOffset, rect), charOffset,
    );
    straightRef.current = event.shiftKey;
    setHasDraft(true);
    setError(null);
    scheduleRedraw();
  }

  function onPointerMove(event: ReactPointerEvent<HTMLCanvasElement>) {
    updateEraserCursor(event);
    if (pointerIdRef.current === event.pointerId && contentRef.current) {
      const point = normalizedPoint(event, contentRef.current);
      if (point.x < 0 || point.x > 1 || point.y < 0 || point.y > 1) {
        hideEraserCursor();
        onPointerUp(event);
        return;
      }
    }
    if (!disabled && enabled && erasingRef.current && pointerIdRef.current === event.pointerId && contentRef.current) {
      eraseThrough(normalizedPoint(event, contentRef.current));
      return;
    }
    if (disabled || !enabled || pointerIdRef.current !== event.pointerId || !contentRef.current || !drawingRef.current.length) return;
    const point = normalizedPoint(event, contentRef.current);
    if (event.shiftKey) straightRef.current = true;
    if (straightRef.current) {
      const start = drawingRef.current[0];
      drawingRef.current = start ? [start, point] : [point];
    } else {
      drawingRef.current.push(point);
    }
    scheduleRedraw();
  }

  function onPointerUp(event: ReactPointerEvent<HTMLCanvasElement>) {
    if (pointerIdRef.current !== event.pointerId) return;
    if (erasingRef.current) {
      if (!disabledRef.current && contentRef.current) {
        const point = normalizedPoint(event, contentRef.current);
        if (point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1) eraseThrough(point);
      }
      finishErasing();
      releasePointer(event);
      return;
    }
    if (disabledRef.current) {
      releasePointer(event);
      return;
    }
    const points = compactStrokePoints(drawingRef.current, straightRef.current ? 0 : 0.0005);
    const strokeTool = activeToolRef.current;
    const inkAnchor = inkAnchorRef.current;
    drawingRef.current = [];
    inkAnchorRef.current = null;
    activeToolRef.current = "pen";
    straightRef.current = false;
    setHasDraft(false);
    releasePointer(event);
    scheduleRedraw();
    if (!points.length || strokeTool === "eraser") return;
    const annotation: Annotation = {
      id: newInkId(inkCounterRef.current),
      kind: "ink",
      text: strokeTool === "marker" ? "marker" : undefined,
      color: currentColor,
      points: points.map((point) => ({ x: clamp(point.x), y: clamp(point.y) })),
      anchor: {
        start: 0,
        end: 0,
        content_hash: revisionHash,
        ...(inkAnchor ? {
          ink: {
            frame: inkAnchor.frame,
            origin: inkAnchor.origin,
            ...(inkAnchor.charOffset === null ? {} : { char: { offset: inkAnchor.charOffset } }),
            style: currentBrushStyle,
          },
        } : {}),
      },
    };
    styleRef.current.set(annotation.id, currentBrushStyle);
    const host = contentRef.current;
    const rect = host?.getBoundingClientRect();
    if (rect) frameRef.current.set(annotation.id, inkAnchor?.frame ?? { width: rect.width, height: rect.height });
    ensureRecord(annotation);
    publishLocal();
    addInkHistory([annotation]);
    void persistInk(annotation);
  }

  function onPointerCancel(event: ReactPointerEvent<HTMLCanvasElement>) {
    hideEraserCursor();
    if (pointerIdRef.current !== event.pointerId) return;
    if (erasingRef.current) {
      finishErasing();
      releasePointer(event);
      return;
    }
    if (disabledRef.current) {
      releasePointer(event);
      return;
    }
    cancelDraft();
    releasePointer(event);
  }

  function toggleEnabled() {
    if (disabled) return;
    finishErasing();
    hideEraserCursor();
    if (enabled) cancelDraft();
    setEnabled(!enabled);
    if (!enabled) wrapperRef.current?.focus({ preventScroll: true });
  }

  function retryPendingSave() {
    if (disabled || pendingSave?.status !== "error") return;
    retryFailedInk("present");
  }

  async function discardPendingSave() {
    if (disabled || pendingSave?.status !== "error") return;
    // Coordinates are not exact text anchors; normalized points survive reflow.
    // Reconcile with the same client annotation id before issuing a delete;
    // a rejected response may have committed the POST on the server.
    const epoch = revisionHashRef.current;
    const saved = await persistInk(pendingSave.annotation);
    if (saved && epoch === revisionHashRef.current) await deleteInk(saved, true);
  }

  function retryDelete() {
    if (disabled || deleteState?.status !== "error") return;
    retryFailedInk("absent");
  }

  function retryFailedInk(desired: "present" | "absent") {
    for (const record of recordsRef.current.values()) {
      if (record.revisionHash === revisionHashRef.current && record.desired === desired) record.error = null;
    }
    setError(null);
    queueMicrotask(() => { void pumpOutbox(); });
  }

  function selectBrushWidth(value: string) {
    const width = Number(value);
    if (!Number.isFinite(width) || width < 1 || width > 24) return;
    setBrushWidths((current) => ({ ...current, [tool === "marker" ? "marker" : "pen"]: width }));
  }

  function selectBrushOpacity(value: string) {
    const opacity = Number(value);
    if (!Number.isFinite(opacity) || opacity < 0.1 || opacity > 1) return;
    setBrushOpacities((current) => ({ ...current, [tool === "marker" ? "marker" : "pen"]: opacity }));
  }

  function onSurfaceKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (disabled || !enabled || isTextEditingTarget(event.target) || event.defaultPrevented) return;
    const key = event.key.toLowerCase();
    const modifier = event.ctrlKey || event.metaKey;
    if (!modifier) return;
    if (key === "z" || key === "y") {
      event.preventDefault();
      if (key === "y" || event.shiftKey) redo();
      else undo();
    }
  }

  function onSurfaceBlur(event: ReactFocusEvent<HTMLDivElement>) {
    const nextFocus = event.relatedTarget as Node | null;
    if (!nextFocus || !event.currentTarget.contains(nextFocus)) {
      suspendPointer();
      // The wrapper remains intentionally focusable when marks are enabled;
      // this state is useful to assistive technology without changing canvas
      // pointer behavior.
    }
  }

  return (
    <div className={styles.wrapper} ref={wrapperRef} tabIndex={enabled ? 0 : -1} aria-busy={busy || undefined} onBlur={onSurfaceBlur} onKeyDown={onSurfaceKeyDown} data-statement-surface="true">
      <div className={styles.controls} role="toolbar" aria-label="Пометки условия">
        <button aria-pressed={enabled} className={[styles.toggle, enabled ? styles.selected : ""].filter(Boolean).join(" ")} disabled={disabled} onClick={toggleEnabled} type="button">
          <Icon name="pen" size={17} />
          <span>Пометки</span>
        </button>
        {enabled ? (
          <>
            <span className={styles.divider} aria-hidden="true" />
            <button aria-label="Перо" aria-pressed={tool === "pen"} className={[styles.toolButton, tool === "pen" ? styles.selected : ""].filter(Boolean).join(" ")} disabled={disabled} onClick={() => selectTool("pen")} title="Перо" type="button">
              <Icon name="pen" size={18} />
              <span>Перо</span>
            </button>
            <button aria-label="Маркер" aria-pressed={tool === "marker"} className={[styles.toolButton, tool === "marker" ? styles.selected : ""].filter(Boolean).join(" ")} disabled={disabled} onClick={() => selectTool("marker")} title="Маркер" type="button">
              <Icon name="highlighter" size={18} />
              <span>Маркер</span>
            </button>
            <button aria-label="Ластик" aria-pressed={tool === "eraser"} className={[styles.toolButton, tool === "eraser" ? styles.selected : ""].filter(Boolean).join(" ")} disabled={disabled} onClick={() => selectTool("eraser")} title="Ластик" type="button">
              <Icon name="eraser" size={18} />
              <span>Ластик</span>
            </button>
            <div className={styles.colorControl}>
              <button aria-expanded={colorOpen} aria-haspopup="true" aria-label={`Цвет ${tool === "marker" ? "маркера" : "пера"}`} className={styles.colorButton} disabled={disabled || tool === "eraser"} ref={colorButtonRef} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setPalettePosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - 282)), top: rect.bottom + 72 < window.innerHeight ? rect.bottom + 6 : Math.max(8, rect.top - 72) }); setColorOpen((current) => !current); }} type="button">
                <span className={styles.swatchCurrent} style={{ backgroundColor: currentColor }} />
                <span>Цвет</span>
              </button>
              {colorOpen ? createPortal(
                <div ref={paletteRef} className={styles.palette} style={palettePosition} role="group" aria-label="Цвет пометки">
                  {PALETTE.map((item) => (
                    <button aria-label={item.label} aria-pressed={currentColor === item.value} className={[styles.swatch, currentColor === item.value ? styles.swatchSelected : ""].filter(Boolean).join(" ")} disabled={disabled} key={item.value} onClick={() => { setToolColors((current) => ({ ...current, [tool === "marker" ? "marker" : "pen"]: item.value })); setColorOpen(false); colorButtonRef.current?.focus(); }} style={{ backgroundColor: item.value }} title={item.label} type="button" />
                  ))}
                </div>, document.body,
              ) : null}
            </div>
            <label className={styles.rangeControl}>
              <span>Толщина</span>
              <input aria-label="Толщина кисти" className={styles.range} disabled={disabled || tool === "eraser"} max={24} min={1} onChange={(event) => selectBrushWidth(event.target.value)} step={1} type="range" value={currentBrushStyle.width} />
              <output>{currentBrushStyle.width}</output>
            </label>
            <label className={styles.rangeControl}>
              <span>Непрозрачность</span>
              <input aria-label="Непрозрачность пометки" className={styles.range} disabled={disabled || tool === "eraser"} max={1} min={0.1} onChange={(event) => selectBrushOpacity(event.target.value)} step={0.05} type="range" value={currentBrushStyle.opacity} />
              <output>{Math.round(currentBrushStyle.opacity * 100)}%</output>
            </label>
              <button aria-label="Стереть последний черновик" aria-hidden={!hasDraft} className={[styles.iconButton, !hasDraft ? styles.inactiveDraft : ""].filter(Boolean).join(" ")} disabled={disabled || !hasDraft} onClick={cancelDraft} title="Стереть последний черновик" type="button">
                <Icon name="close" size={18} />
              </button>
            <button aria-label="Отменить последнюю сохранённую пометку" className={styles.iconButton} disabled={disabled || history.length === 0} onClick={undo} title="Отменить последнюю сохранённую пометку" type="button">
              <Icon name="undo" size={18} />
            </button>
            <button aria-label="Повторить пометку" className={styles.iconButton} disabled={disabled || redoHistory.length === 0} onClick={redo} title="Повторить пометку" type="button">
              <Icon name="redo" size={18} />
            </button>
          </>
        ) : null}
      </div>
      <div className={styles.content + (enabled || localInk.length ? ` ${styles.paper}` : "")} ref={contentRef}>
        {children}
        <canvas aria-hidden="true" className={[styles.canvas, enabled && !disabled ? styles.canvasEnabled : "", enabled && tool === "eraser" ? styles.canvasEraser : ""].filter(Boolean).join(" ")} onPointerCancel={onPointerCancel} onLostPointerCapture={onPointerCancel} onPointerDown={onPointerDown} onPointerLeave={hideEraserCursor} onPointerMove={onPointerMove} onPointerUp={onPointerUp} ref={canvasRef} />
        <div aria-hidden="true" className={styles.eraserCursor} ref={eraserCursorRef} />
      </div>
      {error ? (
        <div className={styles.feedback} role="alert">
          <p className={styles.error}><Icon name="warning" size={14} />{error}</p>
          {pendingSave?.status === "error" ? (
            <div className={styles.recoveryActions}>
              <button className={styles.recoveryButton} disabled={disabled} onClick={retryPendingSave} type="button">Повторить сохранение</button>
              <button className={styles.recoveryButton} disabled={disabled} onClick={discardPendingSave} type="button">Удалить после проверки</button>
            </div>
          ) : null}
          {deleteState?.status === "error" ? (
            <div className={styles.recoveryActions}>
              <button className={styles.recoveryButton} disabled={disabled} onClick={retryDelete} type="button">Повторить удаление</button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
