import { findPythonBracketState } from "./python-highlight";

export type PythonEditRequest = {
  code: string;
  selectionStart: number;
  selectionEnd: number;
  key: string;
  shiftKey?: boolean;
  tabSize?: number;
};

export type PythonEditResult = {
  value: string;
  selectionStart: number;
  selectionEnd: number;
};

type TextEdit = { at: number; removed: number; inserted: string };

function clamp(value: number, maximum: number): number {
  return Math.max(0, Math.min(maximum, Number.isFinite(value) ? value : 0));
}

function normalizedRange(code: string, start: number, end: number): [number, number] {
  const safeStart = clamp(start, code.length);
  const safeEnd = clamp(end, code.length);
  return safeStart <= safeEnd ? [safeStart, safeEnd] : [safeEnd, safeStart];
}

function lineStart(code: string, offset: number): number {
  const newline = code.lastIndexOf("\n", clamp(offset, code.length) - 1);
  return newline < 0 ? 0 : newline + 1;
}

function lineIndent(code: string, offset: number): string {
  const start = lineStart(code, offset);
  return code.slice(start, offset).match(/^[ \t]*/)?.[0] ?? "";
}

function currentLineEndsWithBlockColon(code: string, offset: number): boolean {
  let quote = "";
  let inComment = false;
  let lineLastCode = "";

  for (let index = 0; index < offset; index += 1) {
    const char = code[index];
    if (inComment) {
      if (char === "\n") {
        inComment = false;
        lineLastCode = "";
      }
      continue;
    }
    if (quote) {
      if (quote.length === 3) {
        if (code.slice(index, index + 3) === quote) {
          quote = "";
          index += 2;
        }
      } else if (char === "\\") {
        index += 1;
      } else if (char === quote) {
        quote = "";
      }
      continue;
    }
    if (char === "#") {
      inComment = true;
    } else if (char === "\n") {
      lineLastCode = "";
    } else if (char === "'" || char === '"') {
      const triple = char.repeat(3);
      quote = code.slice(index, index + 3) === triple ? triple : char;
      if (quote.length === 3) index += 2;
    } else if (!/\s/.test(char)) {
      lineLastCode = char;
    }
  }
  return lineLastCode === ":";
}

function replaceRange(code: string, start: number, end: number, inserted: string): PythonEditResult {
  const value = code.slice(0, start) + inserted + code.slice(end);
  const caret = start + inserted.length;
  return { value, selectionStart: caret, selectionEnd: caret };
}

function applyEdits(code: string, edits: TextEdit[], start: number, end: number): PythonEditResult {
  const ordered = [...edits].sort((left, right) => right.at - left.at);
  let value = code;
  for (const edit of ordered) value = value.slice(0, edit.at) + edit.inserted + value.slice(edit.at + edit.removed);

  const mapOffset = (offset: number): number => {
    let mapped = offset;
    for (const edit of edits) {
      if (offset > edit.at + edit.removed) mapped += edit.inserted.length - edit.removed;
      else if (offset > edit.at) mapped = edit.at + edit.inserted.length;
    }
    return mapped;
  };
  return { value, selectionStart: mapOffset(start), selectionEnd: mapOffset(end) };
}

function selectedLineStarts(code: string, start: number, end: number): number[] {
  const first = lineStart(code, start);
  const lastOffset = end > start && end > 0 && code[end - 1] === "\n" ? end - 1 : end;
  const last = lineStart(code, lastOffset);
  const starts: number[] = [];
  for (let offset = first; offset <= last; ) {
    starts.push(offset);
    const newline = code.indexOf("\n", offset);
    if (newline < 0 || newline >= last) break;
    offset = newline + 1;
  }
  return starts;
}

function indentSelection(code: string, start: number, end: number, tabSize: number, dedent: boolean): PythonEditResult {
  const edits: TextEdit[] = [];
  for (const at of selectedLineStarts(code, start, end)) {
    const line = code.slice(at, code.indexOf("\n", at) < 0 ? code.length : code.indexOf("\n", at));
    const leading = line.match(/^[ \t]*/)?.[0] ?? "";
    if (dedent) {
      const removed = leading.endsWith("\t") ? 1 : Math.min(tabSize, leading.length);
      if (removed > 0) edits.push({ at, removed, inserted: "" });
    } else {
      edits.push({ at, removed: 0, inserted: " ".repeat(tabSize) });
    }
  }
  return edits.length ? applyEdits(code, edits, start, end) : { value: code, selectionStart: start, selectionEnd: end };
}

function smartBackspace(code: string, start: number, end: number, tabSize: number): PythonEditResult | null {
  if (start !== end || start === 0) return null;
  const prefix = code.slice(lineStart(code, start), start);
  if (/^[ \t]+$/.test(prefix)) {
    const remainder = prefix.length % tabSize;
    const remove = prefix.endsWith("\t") ? 1 : Math.min(prefix.length, remainder === 0 ? tabSize : remainder);
    return replaceRange(code, start - remove, start, "");
  }
  return null;
}

export function applyPythonEdit(request: PythonEditRequest): PythonEditResult | null {
  const tabSize = Math.max(1, Math.min(8, Math.round(request.tabSize ?? 4)));
  const [start, end] = normalizedRange(request.code, request.selectionStart, request.selectionEnd);

  if (request.key === "Enter") {
    const indent = lineIndent(request.code, start);
    const extra = currentLineEndsWithBlockColon(request.code, start) ? " ".repeat(tabSize) : "";
    return replaceRange(request.code, start, end, "\n" + indent + extra);
  }
  if (request.key === "Backspace") return smartBackspace(request.code, start, end, tabSize);
  if (request.key === "Tab") {
    if (start === end && !request.shiftKey) {
      const column = request.code.slice(lineStart(request.code, start), start).replace(/\t/g, " ".repeat(tabSize)).length;
      return replaceRange(request.code, start, end, " ".repeat(tabSize - column % tabSize));
    }
    return indentSelection(request.code, start, end, tabSize, Boolean(request.shiftKey));
  }
  if (!request.shiftKey && start === end && [")", "]", "}"].includes(request.key) && request.code[start] === request.key) {
    const bracketState = findPythonBracketState(request.code, start + 1);
    if (bracketState.active !== start || bracketState.match === null) return null;
    return { value: request.code, selectionStart: start + 1, selectionEnd: start + 1 };
  }
  return null;
}

function isJavaScriptStackLine(line: string): boolean {
  const trimmed = line.trim();
  if (/^wasm-function\[\d+\](?::0x[0-9a-f]+)?$/i.test(trimmed)) return true;
  if (!/^at\s+/i.test(trimmed)) return false;
  return (
    /(?:wasm-function\[|\.(?:m?js|wasm)(?::|\)|$))/i.test(trimmed) ||
    /\([^)]*:\d+:\d+\)\s*$/.test(trimmed)
  );
}

function pythonFramePath(line: string): string | null {
  return line.match(/^\s*File\s+["']([^"']+)["'],\s+line\s+\d+(?:,\s+in\s+.*)?$/)?.[1] ?? null;
}

function isInternalPyodideFrame(path: string): boolean {
  return /(?:^|[\\/])_?pyodide(?:[\\/]|$)/i.test(path);
}

function withoutInternalPyodideFrames(lines: string[]): string[] {
  const cleaned: string[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const path = pythonFramePath(lines[index]);
    if (!path || !isInternalPyodideFrame(path)) {
      cleaned.push(lines[index]);
      continue;
    }
    index += 1;
    while (index < lines.length) {
      if (pythonFramePath(lines[index])) break;
      const line = lines[index];
      if (line && !/^\s/.test(line)) break;
      index += 1;
    }
    index -= 1;
  }
  return cleaned;
}

export function cleanPythonTraceback(error: unknown): string {
  const raw = String(error instanceof Error ? error.message : error ?? "Python execution failed").replace(/\r\n?/g, "\n");
  const lines = raw.split("\n").filter((line) => !isJavaScriptStackLine(line));
  const start = lines.findIndex((line) => line.includes("Traceback (most recent call last):") || /\bFile [\"'](?:main\.py|<exec>)/.test(line));
  const selected = start < 0 ? lines : withoutInternalPyodideFrames(lines.slice(start));
  const cleaned = selected.map((line) => line.replace(/^PythonError:\s*/, ""));
  return cleaned.join("\n").trim().slice(0, 200000);
}
