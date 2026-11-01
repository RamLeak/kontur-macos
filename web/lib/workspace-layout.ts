export const DEFAULT_SPLIT = 64;
export const SPLIT_STORAGE_KEY = "kontur-local:workspace-split:v1";
export const LEGACY_SPLIT_STORAGE_KEY = "kontur-local:workspace-split:legacy";

export function splitBounds(width: number): [number, number] {
  const minimum = Math.min(45, Math.max(20, 300 / Math.max(1, width - 8) * 100));
  return [minimum, 100 - minimum];
}

export function clampSplit(value: number, width: number): number {
  const [min, max] = splitBounds(width);
  return Math.max(min, Math.min(max, Number.isFinite(value) ? value : DEFAULT_SPLIT));
}

export function splitAtPointer(clientX: number, left: number, width: number): number {
  return clampSplit((clientX - left) / Math.max(1, width - 8) * 100, width);
}

/**
 * Read the code/board share used by the current left pane.
 *
 * v2 stored the condition/task share because the task was the first child.
 * v3 stores the first (code/board) pane share after the workspace order was
 * inverted. Migrate once so a user's existing divider position remains
 * visually stable rather than silently jumping to the opposite side.
 */
export function readStoredSplit(width: number, storage?: Storage): number {
  try {
    const target = storage ?? (typeof localStorage === "undefined" ? null : localStorage);
    if (!target) return clampSplit(DEFAULT_SPLIT, width);
    const current = target.getItem(SPLIT_STORAGE_KEY);
    if (current !== null) {
      const value = Number(current);
      return clampSplit(value, width);
    }
    const legacy = target.getItem(LEGACY_SPLIT_STORAGE_KEY);
    if (legacy !== null) {
      const value = Number(legacy);
      const migrated = clampSplit(100 - value, width);
      target.setItem(SPLIT_STORAGE_KEY, String(migrated));
      return migrated;
    }
  } catch {
    // Storage is optional; layout remains usable with the default share.
  }
  return clampSplit(DEFAULT_SPLIT, width);
}

export function assistantSplit(width: number, size: "narrow" | "standard" | "wide"): number {
  const rightWidth = { narrow: 300, standard: 360, wide: 440 }[size];
  return clampSplit((1 - rightWidth / Math.max(1, width - 8)) * 100, width);
}
