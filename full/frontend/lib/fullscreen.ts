export type FullscreenKeyboardEventLike = {
  key?: string;
  repeat?: boolean;
  isComposing?: boolean;
  keyCode?: number;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  metaKey?: boolean;
  defaultPrevented?: boolean;
};

export type FullscreenDocumentLike = {
  fullscreenElement?: unknown;
  exitFullscreen?: () => Promise<void> | void;
};

export type FullscreenRootLike = {
  requestFullscreen?: () => Promise<void> | void;
};

export function isPlainF11Event(event: FullscreenKeyboardEventLike): boolean {
  return event.key === "F11"
    && !event.repeat
    && event.isComposing !== true
    && event.keyCode !== 229
    && !event.ctrlKey
    && !event.altKey
    && !event.shiftKey
    && !event.metaKey
    && event.defaultPrevented !== true;
}

export function canToggleFullscreen(documentLike: FullscreenDocumentLike, root: FullscreenRootLike): boolean {
  return Boolean(documentLike.fullscreenElement
    ? typeof documentLike.exitFullscreen === "function"
    : typeof root.requestFullscreen === "function");
}

export function toggleFullscreen(documentLike: FullscreenDocumentLike, root: FullscreenRootLike): Promise<void> | undefined {
  try {
    if (documentLike.fullscreenElement) {
      return typeof documentLike.exitFullscreen === "function"
        ? Promise.resolve(documentLike.exitFullscreen())
        : undefined;
    }
    return typeof root.requestFullscreen === "function"
      ? Promise.resolve(root.requestFullscreen())
      : undefined;
  } catch (error) {
    return Promise.reject(error);
  }
}
