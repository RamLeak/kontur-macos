/**
 * Small, surface-local history helpers shared by drawing surfaces.
 *
 * A history entry represents one completed user gesture.  Callers own the
 * snapshots and should clone mutable documents before passing them here.
 */
export interface DrawingHistoryEntry<T> {
  before: T;
  after: T;
}

export interface DrawingHistoryState<T> {
  history: DrawingHistoryEntry<T>[];
  redo: DrawingHistoryEntry<T>[];
}

export function pushDrawingHistory<T>(
  state: DrawingHistoryState<T>,
  before: T,
  after: T,
  limit = 50,
): DrawingHistoryState<T> {
  if (limit <= 0) return { history: [], redo: [] };
  return {
    history: [...state.history, { before, after }].slice(-limit),
    redo: [],
  };
}

export function undoDrawingHistory<T>(
  state: DrawingHistoryState<T>,
  current: T,
): { state: DrawingHistoryState<T>; value: T } | null {
  const entry = state.history.at(-1);
  if (!entry) return null;
  return {
    state: {
      history: state.history.slice(0, -1),
      redo: [...state.redo, { before: entry.before, after: current }].slice(-50),
    },
    value: entry.before,
  };
}

export function redoDrawingHistory<T>(
  state: DrawingHistoryState<T>,
  current: T,
): { state: DrawingHistoryState<T>; value: T } | null {
  const entry = state.redo.at(-1);
  if (!entry) return null;
  return {
    state: {
      history: [...state.history, { before: current, after: entry.after }].slice(-50),
      redo: state.redo.slice(0, -1),
    },
    value: entry.after,
  };
}
