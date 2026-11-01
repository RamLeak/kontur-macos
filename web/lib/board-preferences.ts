"use client";

export type BoardBackground = "blank" | "grid" | "dots";
export interface BoardPreferences {
  boardBackground: BoardBackground;
  boardColor: string;
  boardWidth: number;
  boardOpacity: number;
  boardMarkerColor: string;
  boardMarkerWidth: number;
  boardMarkerOpacity: number;
  drawingSmoothing: number;
}

export const BOARD_PREFERENCES_CHANGED = "kontur:offline-board-preferences-changed";
export const DEFAULT_BOARD_PREFERENCES: BoardPreferences = {
  boardBackground: "blank", boardColor: "#1D1D1F", boardWidth: 3, boardOpacity: 1,
  boardMarkerColor: "#F2C94C", boardMarkerWidth: 18, boardMarkerOpacity: 1, drawingSmoothing: 0.65,
};
const STORAGE_KEY = "kontur:offline-board-preferences:v1";

function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function color(value: unknown, fallback: string): string { return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value) ? value : fallback; }

export function getBoardPreferences(): BoardPreferences {
  if (typeof window === "undefined") return { ...DEFAULT_BOARD_PREFERENCES };
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}") as Partial<BoardPreferences>;
    return normalize(value);
  } catch { return { ...DEFAULT_BOARD_PREFERENCES }; }
}

function normalize(value: Partial<BoardPreferences>): BoardPreferences {
  const next = { ...DEFAULT_BOARD_PREFERENCES, ...value };
  return {
    boardBackground: next.boardBackground === "grid" || next.boardBackground === "dots" ? next.boardBackground : "blank",
    boardColor: color(next.boardColor, DEFAULT_BOARD_PREFERENCES.boardColor),
    boardWidth: finite(next.boardWidth) ? Math.min(24, Math.max(1, next.boardWidth)) : DEFAULT_BOARD_PREFERENCES.boardWidth,
    boardOpacity: finite(next.boardOpacity) ? Math.min(1, Math.max(0.1, next.boardOpacity)) : DEFAULT_BOARD_PREFERENCES.boardOpacity,
    boardMarkerColor: color(next.boardMarkerColor, DEFAULT_BOARD_PREFERENCES.boardMarkerColor),
    boardMarkerWidth: finite(next.boardMarkerWidth) ? Math.min(24, Math.max(1, next.boardMarkerWidth)) : DEFAULT_BOARD_PREFERENCES.boardMarkerWidth,
    boardMarkerOpacity: finite(next.boardMarkerOpacity) ? Math.min(1, Math.max(0.1, next.boardMarkerOpacity)) : DEFAULT_BOARD_PREFERENCES.boardMarkerOpacity,
    drawingSmoothing: finite(next.drawingSmoothing) ? Math.min(1, Math.max(0, next.drawingSmoothing)) : DEFAULT_BOARD_PREFERENCES.drawingSmoothing,
  };
}

export function setBoardPreferences(patch: Partial<BoardPreferences>): BoardPreferences {
  const next = normalize({ ...getBoardPreferences(), ...patch });
  if (typeof window === "undefined") return next;
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); } catch { /* Preferences are best effort. */ }
  window.dispatchEvent(new Event(BOARD_PREFERENCES_CHANGED));
  return next;
}
