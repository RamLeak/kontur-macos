/** Data-only v1 backup schema. Keep docs/CLIENT_STATE_CONTRACT.md in sync. */
import { normalizeShortcutBinding, isReservedShortcut } from "./kontur-shortcuts";
export type ClientJson = null | boolean | number | string | ClientJson[] | { [key: string]: ClientJson };
export type ClientStateValue = { version: 1; entries: Record<string, ClientJson> };
export type ClientStateRecord = { version: number; value: ClientStateValue };
export const CLIENT_STATE_MAX_BYTES = 8 * 1024 * 1024;
export const CLIENT_STATE_OUTBOX = "kontur:offline-client-state-outbox:v1";
const unsafe = new Set(["__proto__", "prototype", "constructor"]);
const commands = new Set("assistant.toggle panel.toggle focus.toggle surface.toggle code.run answer.check task.previous task.next task.bookmark board.pen board.marker board.pan board.line board.rect board.ellipse board.text board.eraser board.undo board.redo board.clear board.zoom.in board.zoom.out board.zoom.reset".split(" "));
function boolean(v: unknown) { return typeof v === "boolean"; }
function number(v: unknown, min: number, max: number): v is number { return typeof v === "number" && Number.isFinite(v) && v >= min && v <= max; }
function integer(v: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): v is number { return number(v, min, max) && Number.isSafeInteger(v); }
function string(v: unknown, max: number): v is string { return typeof v === "string" && v.length <= max; }
function id(v: unknown): v is string { return typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(v) && !unsafe.has(v); }
function color(v: unknown) { return typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v); }
function record(v: unknown): v is Record<string, unknown> { return !!v && typeof v === "object" && !Array.isArray(v) && [Object.prototype, null].includes(Object.getPrototypeOf(v)); }
function keys(v: Record<string, unknown>, required: string[], optional: string[] = []) { return required.every(k => Object.hasOwn(v, k)) && Object.keys(v).every(k => !unsafe.has(k) && (required.includes(k) || optional.includes(k))); }
function bytes(v: unknown) { return new TextEncoder().encode(JSON.stringify(v)).length; }
type Fields = Record<string, readonly ClientJson[] | ((v: unknown) => boolean)>;
const preferences: Fields = {
  theme: ["system", "light", "dark"], textSize: ["standard", "large"], density: ["comfortable", "compact"], editorTheme: ["interface", "light", "charcoal"],
  boardBackground: ["blank", "grid", "dots"], assistantWidth: ["narrow", "standard", "wide"], hintMode: ["hint", "explain"], model: ["auto", "luna", "terra", "sol"], effort: ["low", "medium", "high", "xhigh"],
  tutorMode: ["hint", "hint_ladder", "feynman", "socratic", "debug", "review", "compare", "full_solution"],
  reducedMotion: boolean, autoHideNavigation: boolean, rightPanelVisible: boolean, restoreWorkspace: boolean, editorWrap: boolean, editorLineNumbers: boolean,
  defaultTaskType: v => integer(v, 1, 27), editorFontSize: [12, 14, 16], editorTabSize: [2, 4, 8], fileCacheMb: [32, 64, 128, 256, 512, 1024],
  outputHeight: v => number(v, 100, 360), boardWidth: v => number(v, 1, 24), boardMarkerWidth: v => number(v, 1, 24),
  boardOpacity: v => number(v, .1, 1), boardMarkerOpacity: v => number(v, .1, 1), drawingSmoothing: v => number(v, 0, 1), boardColor: color, boardMarkerColor: color,
};
const filters: Fields = {
  line: v => integer(v, 1, 27), query: v => string(v, 500), year: ["Все годы", "2027", "2026", "2025", "2024"],
  difficulty: ["Все сложности", "easy", "medium", "hard"], files: ["Все файлы", "С файлами", "Без файлов"], learningStatus: ["", "neutral", "started", "incorrect", "correct"], bookmarksOnly: boolean,
};
function fields(v: unknown, schema: Fields) { return record(v) && Object.entries(v).every(([k, item]) => Object.hasOwn(schema, k) && (typeof schema[k] === "function" ? (schema[k] as (v: unknown) => boolean)(item) : (schema[k] as readonly unknown[]).includes(item))); }
function shortcut(v: unknown, platform: "windows" | "macos") {
  if (v === null) return true;
  if (typeof v !== "string" || !/^(?:(?:Ctrl|Alt|Shift|Cmd)\+){0,4}[^+\s\x00-\x1f\x7f]{1,20}$/u.test(v)) return false;
  const parts = v.split("+"), key = parts.pop()!;
  if (new Set(parts).size !== parts.length || ["Ctrl", "Alt", "Shift", "Cmd"].includes(key)) return false;
  try { return normalizeShortcutBinding(key) === key && !isReservedShortcut(platform, v); } catch { return false; }
}
function validCodeRun(v: unknown) {
  if (!record(v) || !keys(v, ["id", "status", "stdout", "stderr", "duration_ms", "exit_code", "created_at"], ["code_version", "reason"])) return false;
  return id(v.id) && ["queued", "running", "succeeded", "completed", "failed", "timed_out", "unavailable"].includes(v.status as string)
    && string(v.stdout, 200000) && string(v.stderr, 200000) && (v.duration_ms === null || number(v.duration_ms, 0, 1e12))
    && (v.exit_code === null || integer(v.exit_code, -2147483648, 2147483647)) && typeof v.created_at === "string"
    && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(v.created_at) && Number.isFinite(Date.parse(v.created_at)) && new Date(v.created_at).toISOString() === v.created_at
    && (!Object.hasOwn(v, "code_version") || v.code_version === null || integer(v.code_version)) && (!Object.hasOwn(v, "reason") || v.reason === null || string(v.reason, 2000));
}
export function validateClientStateEntry(key: string, value: unknown): asserts value is ClientJson {
  if (!isClientStateKey(key)) throw new Error("Неизвестный ключ локальных настроек.");
  let valid = false;
  if (key === "kontur:interface-preferences:v1") valid = fields(value, preferences) && bytes(value) <= 16384;
  else if (key.startsWith("kontur:shortcut-profile:")) valid = record(value) && bytes(value) <= 16384 && Object.entries(value).every(([command, binding]) => commands.has(command) && shortcut(binding, key.includes(":windows:") ? "windows" : "macos"));
  else if (key.startsWith("kontur:workspace-split:") || key.startsWith("kontur:assistant-split:")) valid = number(value, 0, 100);
  else if (key === "kontur:assistant-technical-mode:v1") valid = boolean(value);
  else if (key === "kontur:ui:task-bank:filters:v1") valid = fields(value, filters) && bytes(value) <= 8192;
  else if (key === "kontur:ui:task-bank:return:v1") {
    if (record(value) && keys(value, ["filterKey", "revisionId", "scrollTop"]) && string(value.filterKey, 4096) && id(value.revisionId) && number(value.scrollTop, 0, 1e9) && bytes(value) <= 8192) {
      try { valid = fields(JSON.parse(value.filterKey), filters); } catch { /* Invalid checkpoint. */ }
    }
  } else if (key.startsWith("kontur:exam-ui:v1:")) {
    valid = record(value) && keys(value, ["itemId", "flags", "surfaces"]) && (value.itemId === null || id(value.itemId)) && record(value.flags) && record(value.surfaces)
      && Object.keys(value.flags).length <= 512 && Object.keys(value.surfaces).length <= 512 && Object.entries(value.flags).every(([key, flag]) => id(key) && boolean(flag))
      && Object.entries(value.surfaces).every(([key, surface]) => id(key) && ["code", "board"].includes(surface as string)) && bytes(value) <= 65536;
  } else if (key.startsWith("kontur:study-checkpoint:v1:")) valid = record(value) && keys(value, ["runId", "question"]) && id(value.runId) && string(value.question, 20000);
  else if (key.startsWith("kontur:device-python-runs:v1:")) valid = Array.isArray(value) && value.length <= 20 && value.every(validCodeRun);
  if (!valid) throw new Error("Локальные настройки содержат недопустимые данные.");
}
export function validateClientStateValue(value: unknown): ClientStateValue {
  if (!record(value) || !keys(value, ["version", "entries"]) || value.version !== 1 || !record(value.entries) || Object.keys(value.entries).length > 4096) throw new Error("Недопустимый формат локальных настроек.");
  for (const [key, entry] of Object.entries(value.entries)) validateClientStateEntry(key, entry);
  if (bytes({ expectedVersion: Number.MAX_SAFE_INTEGER, value }) > CLIENT_STATE_MAX_BYTES) throw new Error("Локальные настройки и история превышают 8 МиБ. Данные сохранены на устройстве; резервная копия пока недоступна.");
  return value as ClientStateValue;
}
export function validateClientStateRecord(value: unknown): ClientStateRecord {
  if (!record(value) || !keys(value, ["version", "value"]) || !integer(value.version)) throw new Error("Недопустимое подтверждение локальных настроек.");
  return { version: value.version, value: validateClientStateValue(value.value) };
}
export function emptyClientState(): ClientStateValue { return { version: 1, entries: {} }; }
export function clientStateFingerprint(value: ClientStateValue): string {
  const sort = (v: ClientJson): ClientJson => Array.isArray(v) ? v.map(sort) : v !== null && typeof v === "object" ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, v]) => [k, sort(v)])) : v;
  return JSON.stringify(sort(value));
}
export function parseClientStorageValue(key: string, raw: string): ClientJson {
  const value: unknown = key.startsWith("kontur:workspace-split:") || key.startsWith("kontur:assistant-split:") ? (raw.trim() ? Number(raw) : NaN) : JSON.parse(raw);
  validateClientStateEntry(key, value); return value;
}
export function serializeClientStorageValue(key: string, value: ClientJson): string { validateClientStateEntry(key, value); return key.startsWith("kontur:workspace-split:") || key.startsWith("kontur:assistant-split:") ? String(value) : JSON.stringify(value); }
export function isClientStateKey(key: string): boolean {
  if (key.length > 512) return false;
  if (["kontur:interface-preferences:v1", "kontur:shortcut-profile:windows:v1", "kontur:shortcut-profile:macos:v1", "kontur:workspace-split:v3", "kontur:workspace-split:v2", "kontur:assistant-technical-mode:v1", "kontur:ui:task-bank:filters:v1", "kontur:ui:task-bank:return:v1"].includes(key) || /^kontur:assistant-split:v1:(narrow|standard|wide)$/.test(key)) return true;
  if (key.startsWith("kontur:exam-ui:v1:")) return id(key.slice("kontur:exam-ui:v1:".length));
  if (key.startsWith("kontur:study-checkpoint:v1:")) { const match = /^exam:(.+):item:(.+)$/.exec(key.slice("kontur:study-checkpoint:v1:".length)); return !!match && id(match[1]) && id(match[2]); }
  if (key.startsWith("kontur:device-python-runs:v1:")) {
    try {
      const suffix = key.slice("kontur:device-python-runs:v1:".length), scope = decodeURIComponent(suffix);
      const practice = /^practice:(.+):browser$/.exec(scope);
      const exam = /^exam:([^:]+):([^:]+):item:([^:]+):browser$/.exec(scope);
      return encodeURIComponent(scope) === suffix
        && (!!practice && id(practice[1]) || !!exam && exam.slice(1).every(id));
    } catch { return false; }
  }
  return false;
}
