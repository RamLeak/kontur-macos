import type { JsonValue } from "./native-types";

export interface StatementBlocks {
  latex: string | null;
  answerTable: { rows: number; cols: number } | null;
  matrix: { headers: string[]; rows: string[][] } | null;
  subtasks: { sourceId: string | null; typeNumber: number | null; statement: string; assetIds: string[] }[];
  unsupported: boolean;
}

/** Read only the documented legacy12 payload; keep original JSON in native storage. */
export function readStatementBlocks(value: JsonValue): StatementBlocks {
  const result: StatementBlocks = { latex: null, answerTable: null, matrix: null, subtasks: [], unsupported: false };
  if (value === null) return result;
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ...result, unsupported: true };
  if (typeof value.statementLatex === "string" && value.statementLatex.trim()) result.latex = value.statementLatex;
  if (value.table && typeof value.table === "object" && !Array.isArray(value.table)) {
    const { rows, cols } = value.table;
    if (typeof rows === "number" && Number.isInteger(rows) && rows >= 0 && rows <= 100
      && typeof cols === "number" && Number.isInteger(cols) && cols >= 0 && cols <= 50) result.answerTable = { rows, cols };
    else {
      const rawRows = value.table.rows ?? value.table.data;
      const rawHeaders = value.table.headers ?? value.table.columns ?? [];
      const scalar = (cell: JsonValue): boolean => cell === null || typeof cell === "string" || typeof cell === "number" || typeof cell === "boolean";
      if (Array.isArray(rawHeaders) && rawHeaders.length <= 50 && rawHeaders.every(scalar) && Array.isArray(rawRows) && rawRows.length <= 100 && rawRows.every(row => Array.isArray(row) && row.length <= 50 && row.every(scalar))) {
        result.matrix = { headers: rawHeaders.map(cell => cell === null ? "" : String(cell)), rows: (rawRows as JsonValue[][]).map(row => row.map(cell => cell === null ? "" : String(cell))) };
      } else if (Object.keys(value.table).length) result.unsupported = true;
    }
  }
  if (Array.isArray(value.subtasks)) {
    for (const child of value.subtasks) {
      if (!child || typeof child !== "object" || Array.isArray(child) || typeof child.statement !== "string") { result.unsupported = true; continue; }
      result.subtasks.push({ sourceId: typeof child.sourceId === "string" ? child.sourceId : null,
        typeNumber: typeof child.typeNumber === "number" && Number.isInteger(child.typeNumber) && child.typeNumber >= 1 && child.typeNumber <= 27 ? child.typeNumber : null,
        statement: child.statement, assetIds: Array.isArray(child.assetIds) ? child.assetIds.filter((id): id is string => typeof id === "string") : [],
      });
    }
  } else if (value.subtasks !== null && value.subtasks !== undefined) result.unsupported = true;
  const known = new Set(["statementLatex", "table", "subtasks", "legacyRevisionId", "legacyContentHash"]);
  if (Object.keys(value).some(key => !known.has(key))) result.unsupported = true;
  return result;
}
