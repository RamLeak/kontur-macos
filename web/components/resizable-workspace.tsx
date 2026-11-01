"use client";

import { Children, useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { assistantSplit, clampSplit, DEFAULT_SPLIT, readStoredSplit, splitAtPointer, splitBounds, SPLIT_STORAGE_KEY } from "@/lib/workspace-layout";
import styles from "./resizable-workspace.module.css";

export function ResizableWorkspace({ children, focusedPane, mobilePane, assistantMode = false, assistantWidth = "standard" }: {
  children: ReactNode;
  focusedPane: "split" | "task" | "tools";
  mobilePane: "task" | "tools";
  assistantMode?: boolean;
  assistantWidth?: "narrow" | "standard" | "wide";
}) {
  const container = useRef<HTMLDivElement>(null);
  const lastSplit = useRef(DEFAULT_SPLIT);
  const [split, setSplit] = useState(DEFAULT_SPLIT);
  const [width, setWidth] = useState(1000);
  const [dragging, setDragging] = useState(false);
  const [conditionPane, toolsPane] = Children.toArray(children);
  const storageKey = assistantMode ? `kontur:assistant-split:v1:${assistantWidth}` : SPLIT_STORAGE_KEY;
  const defaultSplit = assistantMode ? assistantSplit(width, assistantWidth) : DEFAULT_SPLIT;

  function changeSplit(value: number, persist = false) {
    const measuredWidth = container.current?.clientWidth ?? width;
    const next = clampSplit(value, measuredWidth);
    lastSplit.current = next;
    setSplit(next);
    if (persist) {
      try { localStorage.setItem(storageKey, String(next)); } catch { /* Layout still works without storage. */ }
    }
  }

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    try {
      const storedAssistant = assistantMode ? localStorage.getItem(storageKey) : null;
      lastSplit.current = assistantMode
        ? storedAssistant === null ? assistantSplit(element.clientWidth, assistantWidth) : clampSplit(Number(storedAssistant), element.clientWidth)
        : readStoredSplit(element.clientWidth);
    } catch { lastSplit.current = assistantMode ? assistantSplit(element.clientWidth, assistantWidth) : DEFAULT_SPLIT; }
    const measure = () => {
      setWidth(element.clientWidth);
      const next = clampSplit(lastSplit.current, element.clientWidth);
      lastSplit.current = next;
      setSplit(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [assistantMode, assistantWidth, storageKey]);

  const [minimum, maximum] = splitBounds(width);
  return (
    <div ref={container} className={`workspace-grid focus-${focusedPane} mobile-${mobilePane} ${styles.grid} ${dragging ? styles.dragging : ""}`} style={{ "--tools-share": `${split}fr`, "--task-share": `${100 - split}fr` } as CSSProperties}>
      {conditionPane}
      <div
        aria-label={assistantMode ? "Ширина панели кода или доски и помощника" : "Ширина панели кода или доски и условия"}
        aria-describedby="workspace-resize-hint"
        aria-orientation="vertical"
        aria-valuemin={Math.round(minimum)}
        aria-valuemax={Math.round(maximum)}
        aria-valuenow={Math.round(split)}
        aria-valuetext={`Инструменты ${Math.round(split)}%, ${assistantMode ? "помощник" : "условие"} ${Math.round(100 - split)}%`}
        className={styles.divider}
        hidden={focusedPane !== "split"}
        onDoubleClick={() => changeSplit(defaultSplit, true)}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return;
          event.preventDefault();
          const step = event.shiftKey ? 10 : 2;
          changeSplit(event.key === "Home" ? minimum : event.key === "End" ? maximum : event.key === "Enter" ? defaultSplit : split + (event.key === "ArrowLeft" ? step : -step), true);
        }}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          event.currentTarget.focus();
          event.currentTarget.setPointerCapture(event.pointerId);
          setDragging(true);
        }}
        onPointerMove={(event) => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId) || !container.current) return;
          const bounds = container.current.getBoundingClientRect();
          changeSplit(100 - splitAtPointer(event.clientX, bounds.left, bounds.width));
        }}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
          setDragging(false);
          changeSplit(lastSplit.current, true);
        }}
        onPointerCancel={() => { setDragging(false); changeSplit(lastSplit.current, true); }}
        onLostPointerCapture={() => setDragging(false)}
        role="separator"
        tabIndex={focusedPane === "split" ? 0 : -1}
        title="Потяните для изменения ширины · двойной клик — исходная ширина"
      >
        <span id="workspace-resize-hint" className="sr-only">Потяните границу. Клавиши влево и вправо меняют ширину, Enter возвращает исходную ширину.</span>
      </div>
      {toolsPane}
    </div>
  );
}
