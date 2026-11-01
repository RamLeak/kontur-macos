"use client";

import { useEffect } from "react";
import { resolveBoardShortcut, type BoardShortcutAction } from "./board-tools";

export type BoardShortcutId = BoardShortcutAction | "zoom.in" | "zoom.out" | "zoom.reset" | "clear";
export type BoardShortcutActions = Partial<Record<BoardShortcutId, () => void>>;

function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return Boolean(element && typeof element.closest === "function" && element.closest("input, textarea, select, [contenteditable='true']"));
}

export function useBoardShortcuts(enabled: boolean, actions: BoardShortcutActions): void {
  useEffect(() => {
    if (!enabled) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || isTypingTarget(event.target)) return;
      const resolved = resolveBoardShortcut(event);
      const action = resolved ? actions[resolved] : event.key === "+" || event.key === "=" ? actions["zoom.in"] : event.key === "-" ? actions["zoom.out"] : event.key === "0" ? actions["zoom.reset"] : undefined;
      if (!action) return;
      event.preventDefault(); action();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [actions, enabled]);
}
