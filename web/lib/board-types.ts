export type BoardPoint = { x: number; y: number };

export type BoardStrokeKind = "freehand" | "marker" | "line" | "rect" | "ellipse";

export interface BoardStroke {
  id: string;
  kind?: BoardStrokeKind;
  points: BoardPoint[];
  color: string;
  width: number;
  opacity?: number;
}

export interface BoardText {
  id: string;
  x: number;
  y: number;
  text: string;
}

export interface BoardImage {
  id: string;
  asset_id: string;
  revision_hash: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface BoardDocument {
  /** The owning offline Document id; the board is nested inside that document on the wire. */
  id: string;
  version: number;
  strokes: BoardStroke[];
  texts: BoardText[];
  images?: BoardImage[];
  viewport: { x: number; y: number; zoom: number };
  /** Native updatedAt represented as a non-negative Unix timestamp in milliseconds. */
  updated_at: number;
}

export class UnsupportedBoardError extends Error {
  readonly code = "BOARD_UNSUPPORTED";

  constructor(message = "Формат доски в документе не поддерживается этой версией.") {
    super(message);
    this.name = "UnsupportedBoardError";
  }
}

const STROKE_KINDS = new Set<BoardStrokeKind>(["freehand", "marker", "line", "rect", "ellipse"]);
const TOP_LEVEL_KEYS = new Set(["id", "version", "strokes", "texts", "images", "viewport", "updated_at"]);

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function safeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function text(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function timestamp(value: unknown): number | null {
  if (safeInteger(value)) return value;
  if (typeof value !== "string" || value.length === 0) return null;
  const parsed = Date.parse(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function point(value: unknown): BoardPoint {
  const item = record(value);
  if (!item || !finite(item.x) || !finite(item.y)) throw new UnsupportedBoardError("В данных доски есть некорректная точка.");
  return { x: item.x, y: item.y };
}

function stroke(value: unknown): BoardStroke {
  const item = record(value);
  if (!item || !text(item.id) || !Array.isArray(item.points) || !text(item.color) || !finite(item.width) || item.width <= 0) {
    throw new UnsupportedBoardError("В данных доски есть некорректный штрих.");
  }
  if (item.kind !== undefined && (typeof item.kind !== "string" || !STROKE_KINDS.has(item.kind as BoardStrokeKind))) {
    throw new UnsupportedBoardError("В данных доски указан неизвестный тип штриха.");
  }
  if (item.opacity !== undefined && (!finite(item.opacity) || item.opacity < 0 || item.opacity > 1)) {
    throw new UnsupportedBoardError("В данных доски указана некорректная прозрачность.");
  }
  return {
    id: item.id,
    ...(item.kind === undefined ? {} : { kind: item.kind as BoardStrokeKind }),
    points: item.points.map(point),
    color: item.color,
    width: item.width,
    ...(item.opacity === undefined ? {} : { opacity: item.opacity }),
  };
}

function boardText(value: unknown): BoardText {
  const item = record(value);
  if (!item || !text(item.id) || !finite(item.x) || !finite(item.y) || typeof item.text !== "string") {
    throw new UnsupportedBoardError("В данных доски есть некорректная надпись.");
  }
  return { id: item.id, x: item.x, y: item.y, text: item.text };
}

function image(value: unknown): BoardImage {
  const item = record(value);
  if (!item || !text(item.id) || !text(item.asset_id) || !text(item.revision_hash)
    || !finite(item.x) || !finite(item.y) || !finite(item.width) || item.width <= 0
    || !finite(item.height) || item.height <= 0) {
    throw new UnsupportedBoardError("В данных доски есть некорректная иллюстрация.");
  }
  return { id: item.id, asset_id: item.asset_id, revision_hash: item.revision_hash, x: item.x, y: item.y, width: item.width, height: item.height };
}

function viewport(value: unknown): BoardDocument["viewport"] {
  const item = record(value);
  if (!item || !finite(item.x) || !finite(item.y) || !finite(item.zoom) || item.zoom <= 0) {
    throw new UnsupportedBoardError("В данных доски указан некорректный масштаб.");
  }
  return { x: item.x, y: item.y, zoom: item.zoom };
}

export function emptyBoard(documentId: string): BoardDocument {
  if (!text(documentId)) throw new Error("DOCUMENT_ID_REQUIRED");
  return { id: documentId, version: 0, strokes: [], texts: [], images: [], viewport: { x: 0, y: 0, zoom: 1 }, updated_at: 0 };
}

/**
 * Decode the JSON object stored in Document.board without erasing unknown data.
 * Empty `{}` is the v1 wire representation for a new board; populated objects
 * must use the known internal shape or are reported as unsupported.
 */
export function parseBoard(value: unknown, documentId: string): BoardDocument {
  const empty = emptyBoard(documentId);
  if (value === null || value === undefined) return empty;
  const item = record(value);
  if (!item) throw new UnsupportedBoardError();
  const unknown = Object.keys(item).filter((key) => !TOP_LEVEL_KEYS.has(key));
  if (unknown.length) throw new UnsupportedBoardError(`Формат доски не поддерживается: неизвестное поле «${unknown[0]}».`);
  if (!Object.keys(item).length) return empty;

  if (item.id !== undefined && !text(item.id)) throw new UnsupportedBoardError("В данных доски указан некорректный идентификатор.");
  // Legacy exports used a source/session id here. Known board data is rebound
  // to the owning native document; unknown top-level shapes were rejected above.
  if (item.version !== undefined && !safeInteger(item.version)) throw new UnsupportedBoardError("В данных доски указана некорректная версия.");
  const parsedUpdatedAt = item.updated_at === undefined ? 0 : timestamp(item.updated_at);
  if (parsedUpdatedAt === null) throw new UnsupportedBoardError("В данных доски указана некорректная дата.");
  if (item.strokes !== undefined && !Array.isArray(item.strokes)) throw new UnsupportedBoardError("Поле штрихов доски имеет неверный формат.");
  if (item.texts !== undefined && !Array.isArray(item.texts)) throw new UnsupportedBoardError("Поле надписей доски имеет неверный формат.");
  if (item.images !== undefined && !Array.isArray(item.images)) throw new UnsupportedBoardError("Поле иллюстраций доски имеет неверный формат.");

  return {
    id: documentId,
    version: item.version === undefined ? 0 : item.version,
    strokes: item.strokes === undefined ? [] : item.strokes.map(stroke),
    texts: item.texts === undefined ? [] : item.texts.map(boardText),
    images: item.images === undefined ? [] : item.images.map(image),
    viewport: item.viewport === undefined ? { x: 0, y: 0, zoom: 1 } : viewport(item.viewport),
    updated_at: parsedUpdatedAt,
  };
}
