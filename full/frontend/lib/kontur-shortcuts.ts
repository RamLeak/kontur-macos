"use client";

import { useEffect, useRef } from "react";

export type ShortcutPlatform = "windows" | "macos";
export type ShortcutScope = "global" | "workspace" | "editor" | "canvas" | "exam";

export type CommandId =
  | "assistant.toggle"
  | "panel.toggle"
  | "focus.toggle"
  | "surface.toggle"
  | "code.run"
  | "answer.check"
  | "task.previous"
  | "task.next"
  | "task.bookmark"
  | "board.pen"
  | "board.marker"
  | "board.pan"
  | "board.line"
  | "board.rect"
  | "board.ellipse"
  | "board.text"
  | "board.eraser"
  | "board.undo"
  | "board.redo"
  | "board.clear"
  | "board.zoom.in"
  | "board.zoom.out"
  | "board.zoom.reset";

export interface CommandMetadata {
  readonly id: CommandId;
  readonly label: string;
  readonly description: string;
  readonly scope: ShortcutScope;
  readonly category: "workspace" | "editor" | "navigation" | "board";
  readonly letter: boolean;
  readonly defaults: Readonly<Record<ShortcutPlatform, string | null>>;
}

export type ShortcutProfile = Record<CommandId, string | null>;

export interface ShortcutKeyboardEventLike {
  key?: string;
  code?: string;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  metaKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
  keyCode?: number;
  defaultPrevented?: boolean;
  target?: unknown;
  getModifierState?: (key: string) => boolean;
  preventDefault?: () => void;
  stopPropagation?: () => void;
}

type ParsedShortcut = {
  ctrl: boolean;
  alt: boolean;
  shift: boolean;
  cmd: boolean;
  key: string;
};

const RU_TO_CODE: Readonly<Record<string, string>> = Object.freeze({
  а: "KeyF", б: "Comma", в: "KeyD", г: "KeyU", д: "KeyL", е: "KeyT", ё: "Backquote", ж: "Semicolon", з: "KeyP", и: "KeyB",
  й: "KeyQ", к: "KeyR", л: "KeyK", м: "KeyV", н: "KeyY", о: "KeyJ", п: "KeyG", р: "KeyH", с: "KeyC", т: "KeyN",
  у: "KeyE", ф: "KeyA", х: "BracketLeft", ц: "KeyW", ч: "KeyX", ш: "KeyI", щ: "KeyO", ъ: "BracketRight", ы: "KeyS", ь: "KeyM",
  э: "Quote", ю: "Period", я: "KeyZ", ч_: "KeyX",
});

const CODE_TO_RU: Readonly<Record<string, string>> = Object.freeze(Object.entries(RU_TO_CODE).reduce<Record<string, string>>((result, [key, code]) => {
  if (!result[code]) result[code] = key;
  return result;
}, {}));

const MODIFIER_ALIASES: Readonly<Record<string, keyof Pick<ParsedShortcut, "ctrl" | "alt" | "shift" | "cmd">>> = Object.freeze({
  ctrl: "ctrl", control: "ctrl", ctl: "ctrl",
  alt: "alt", option: "alt", opt: "alt",
  shift: "shift",
  cmd: "cmd", command: "cmd", meta: "cmd", win: "cmd", windows: "cmd",
});

const KEY_ALIASES: Readonly<Record<string, string>> = Object.freeze({
  esc: "Escape", escape: "Escape", return: "Enter", enter: "Enter", spacebar: "Space", space: "Space",
  left: "ArrowLeft", right: "ArrowRight", up: "ArrowUp", down: "ArrowDown",
  arrowleft: "ArrowLeft", arrowright: "ArrowRight", arrowup: "ArrowUp", arrowdown: "ArrowDown",
  backspace: "Backspace", delete: "Delete", del: "Delete", tab: "Tab", home: "Home", end: "End",
  pageup: "PageUp", pagedown: "PageDown", insert: "Insert", plus: "Plus", minus: "Minus",
});

const DISPLAY_KEYS: Readonly<Record<string, string>> = Object.freeze({
  ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓", Enter: "↩", Escape: "⎋", Space: "Space",
  Backspace: "⌫", Delete: "⌫", Tab: "⇥", PageUp: "PageUp", PageDown: "PageDown", Plus: "+", Minus: "−",
});

const COMMANDS_DATA: CommandMetadata[] = [
  { id: "assistant.toggle", label: "Показать / скрыть ИИ", description: "Правая панель помощника", scope: "workspace", category: "workspace", letter: true, defaults: { windows: "Ctrl+Alt+KeyA", macos: null } },
  { id: "panel.toggle", label: "Показать / скрыть правую панель", description: "Правая панель рабочего пространства", scope: "global", category: "workspace", letter: true, defaults: { windows: "Ctrl+Alt+KeyP", macos: null } },
  { id: "focus.toggle", label: "Фокус", description: "Скрыть навигацию и оставить рабочую поверхность", scope: "workspace", category: "workspace", letter: true, defaults: { windows: "Ctrl+Alt+KeyF", macos: null } },
  { id: "surface.toggle", label: "Код / Доска", description: "Переключить поверхность решения", scope: "workspace", category: "workspace", letter: true, defaults: { windows: "Ctrl+Alt+KeyD", macos: null } },
  { id: "code.run", label: "Запустить код", description: "Запуск текущей версии кода", scope: "editor", category: "editor", letter: false, defaults: { windows: "Ctrl+Enter", macos: "Cmd+Enter" } },
  { id: "answer.check", label: "Проверить ответ", description: "Проверка ответа в учебном режиме", scope: "workspace", category: "workspace", letter: false, defaults: { windows: "Ctrl+Alt+Enter", macos: null } },
  { id: "task.previous", label: "Предыдущее задание", description: "Перейти к предыдущему заданию", scope: "global", category: "navigation", letter: false, defaults: { windows: "Alt+ArrowLeft", macos: "Alt+ArrowLeft" } },
  { id: "task.next", label: "Следующее задание", description: "Перейти к следующему заданию", scope: "global", category: "navigation", letter: false, defaults: { windows: "Alt+ArrowRight", macos: "Alt+ArrowRight" } },
  { id: "task.bookmark", label: "Закладка", description: "Добавить или убрать закладку задания", scope: "workspace", category: "navigation", letter: false, defaults: { windows: null, macos: null } },
  { id: "board.pen", label: "Перо", description: "Выбрать инструмент пера", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyP", macos: "KeyP" } },
  { id: "board.marker", label: "Маркер", description: "Выбрать инструмент маркера", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyH", macos: "KeyH" } },
  { id: "board.pan", label: "Панорамирование", description: "Временно перемещать доску", scope: "canvas", category: "board", letter: false, defaults: { windows: "Space", macos: "Space" } },
  { id: "board.line", label: "Линия", description: "Выбрать инструмент линии", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyL", macos: "KeyL" } },
  { id: "board.rect", label: "Прямоугольник", description: "Выбрать инструмент прямоугольника", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyR", macos: "KeyR" } },
  { id: "board.ellipse", label: "Эллипс", description: "Выбрать инструмент эллипса", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyO", macos: "KeyO" } },
  { id: "board.text", label: "Текст", description: "Выбрать инструмент текста", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyT", macos: "KeyT" } },
  { id: "board.eraser", label: "Ластик", description: "Выбрать инструмент ластика", scope: "canvas", category: "board", letter: true, defaults: { windows: "KeyE", macos: "KeyE" } },
  { id: "board.undo", label: "Отменить", description: "Отменить последнее действие на доске", scope: "canvas", category: "board", letter: false, defaults: { windows: "Ctrl+KeyZ", macos: "Cmd+KeyZ" } },
  { id: "board.redo", label: "Повторить", description: "Повторить отменённое действие на доске", scope: "canvas", category: "board", letter: false, defaults: { windows: "Ctrl+Shift+KeyZ", macos: "Cmd+Shift+KeyZ" } },
  { id: "board.clear", label: "Очистить доску", description: "Удалить все штрихи и заметки с доски", scope: "canvas", category: "board", letter: false, defaults: { windows: null, macos: null } },
  { id: "board.zoom.in", label: "Увеличить доску", description: "Увеличить масштаб доски", scope: "canvas", category: "board", letter: false, defaults: { windows: null, macos: null } },
  { id: "board.zoom.out", label: "Уменьшить доску", description: "Уменьшить масштаб доски", scope: "canvas", category: "board", letter: false, defaults: { windows: null, macos: null } },
  { id: "board.zoom.reset", label: "Сбросить масштаб доски", description: "Вернуть исходный масштаб и положение", scope: "canvas", category: "board", letter: false, defaults: { windows: null, macos: null } },
];

export const COMMANDS: readonly CommandMetadata[] = Object.freeze(COMMANDS_DATA.map((command) => Object.freeze({ ...command, defaults: Object.freeze({ ...command.defaults }) })));
export const COMMAND_IDS: readonly CommandId[] = Object.freeze(COMMANDS.map((command) => command.id));

export const DEFAULT_SHORTCUT_PROFILES: Readonly<Record<ShortcutPlatform, Readonly<ShortcutProfile>>> = Object.freeze({
  windows: Object.freeze(makeDefaultProfile("windows")),
  macos: Object.freeze(makeDefaultProfile("macos")),
});

export const DEFAULT_PROFILES = DEFAULT_SHORTCUT_PROFILES;
export const SHORTCUTS_CHANGED = "kontur:shortcuts-changed";

function makeDefaultProfile(platform: ShortcutPlatform): ShortcutProfile {
  return COMMANDS_DATA.reduce<ShortcutProfile>((profile, command) => {
    profile[command.id] = command.defaults[platform];
    return profile;
  }, {} as ShortcutProfile);
}

function commandFor(id: CommandId): CommandMetadata {
  const command = COMMANDS.find((item) => item.id === id);
  if (!command) throw new Error(`Неизвестная команда: ${id}`);
  return command;
}

function cloneProfile(profile: Readonly<Record<CommandId, string | null>>): ShortcutProfile {
  return COMMAND_IDS.reduce<ShortcutProfile>((result, id) => {
    result[id] = profile[id] ?? null;
    return result;
  }, {} as ShortcutProfile);
}

function storageKey(platform: ShortcutPlatform): string {
  return `kontur:shortcut-profile:${platform}:v1`;
}

export function detectShortcutPlatform(): ShortcutPlatform {
  if (typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent)) return "macos";
  return "windows";
}

export function getShortcutProfile(platform: ShortcutPlatform): ShortcutProfile {
  const fallback = cloneProfile(DEFAULT_SHORTCUT_PROFILES[platform]);
  if (typeof window === "undefined") return fallback;
  try {
    const stored = JSON.parse(window.localStorage.getItem(storageKey(platform)) || "null") as Record<string, unknown> | null;
    if (!stored || typeof stored !== "object") return fallback;
    for (const command of COMMANDS) {
      if (!Object.prototype.hasOwnProperty.call(stored, command.id)) continue;
      const value = stored[command.id];
      if (value === null) {
        fallback[command.id] = null;
        continue;
      }
      if (typeof value !== "string") continue;
      try {
        const normalized = normalizeShortcutBinding(value);
        if (!isReservedShortcut(platform, normalized)) fallback[command.id] = normalized;
      } catch {
        // Ignore malformed entries and keep the shipped default.
      }
    }
  } catch {
    return fallback;
  }
  return fallback;
}

export function setShortcutBinding(platform: ShortcutPlatform, commandId: CommandId, binding: string | null): ShortcutProfile {
  commandFor(commandId);
  const normalized = binding == null ? null : normalizeShortcutBinding(binding);
  if (normalized && isReservedShortcut(platform, normalized)) {
    throw new Error("Эта комбинация занята системой или браузером");
  }
  const next = getShortcutProfile(platform);
  next[commandId] = normalized;
  if (typeof window !== "undefined") {
    window.localStorage.setItem(storageKey(platform), JSON.stringify(next));
    window.dispatchEvent(new Event(SHORTCUTS_CHANGED));
  }
  return next;
}

export function resetShortcutProfile(platform: ShortcutPlatform): ShortcutProfile {
  const next = cloneProfile(DEFAULT_SHORTCUT_PROFILES[platform]);
  if (typeof window !== "undefined") {
    window.localStorage.setItem(storageKey(platform), JSON.stringify(next));
    window.dispatchEvent(new Event(SHORTCUTS_CHANGED));
  }
  return next;
}

export function getShortcutBinding(platform: ShortcutPlatform, commandId: CommandId): string | null {
  commandFor(commandId);
  return getShortcutProfile(platform)[commandId];
}

export function normalizeShortcutBinding(binding: string): string {
  const parsed = parseShortcutBinding(binding);
  if (!parsed) throw new Error("Комбинация не задана");
  return serializeShortcut(parsed);
}

export function formatShortcut(binding: string | null | undefined, platform: ShortcutPlatform = detectShortcutPlatform()): string {
  if (!binding) return "Не назначено";
  let parsed: ParsedShortcut;
  try {
    parsed = parseShortcutBinding(binding) ?? (() => { throw new Error("invalid"); })();
  } catch {
    return "Не назначено";
  }
  const key = DISPLAY_KEYS[parsed.key] ?? displayCode(parsed.key);
  if (platform === "macos") {
    return `${parsed.ctrl ? "⌃" : ""}${parsed.alt ? "⌥" : ""}${parsed.shift ? "⇧" : ""}${parsed.cmd ? "⌘" : ""}${key}`;
  }
  return [parsed.ctrl ? "Ctrl" : "", parsed.alt ? "Alt" : "", parsed.shift ? "Shift" : "", parsed.cmd ? "Cmd" : "", key].filter(Boolean).join("+");
}

export function getShortcutFromKeyboardEvent(event: ShortcutKeyboardEventLike): string | null {
  if (event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229 || isAltGraph(event) || isModifierOnlyKey(event.key)) return null;
  const key = eventKeyCode(event);
  if (!key || key === "Dead" || key === "Unidentified") return null;
  const parsed: ParsedShortcut = {
    ctrl: Boolean(event.ctrlKey), alt: Boolean(event.altKey), shift: Boolean(event.shiftKey), cmd: Boolean(event.metaKey), key,
  };
  return serializeShortcut(parsed);
}

/**
 * Recorder-facing capture.  Read the event before preventing the default: a
 * real KeyboardEvent flips defaultPrevented synchronously, and the matcher
 * intentionally ignores already-handled events.
 */
export function captureShortcutFromKeyboardEvent(event: ShortcutKeyboardEventLike): string | null {
  if (event.repeat || event.isComposing || event.keyCode === 229) {
    event.preventDefault?.();
    event.stopPropagation?.();
    return null;
  }
  const binding = getShortcutFromKeyboardEvent(event);
  event.preventDefault?.();
  event.stopPropagation?.();
  return binding;
}

// Keep the legacy pure helper behavior for callers outside the recorder.
export const captureShortcut = getShortcutFromKeyboardEvent;

export function matchesShortcut(event: ShortcutKeyboardEventLike, binding: string | null | undefined): boolean {
  if (!binding || event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229 || isAltGraph(event)) return false;
  let expected: ParsedShortcut;
  try {
    expected = parseShortcutBinding(binding) ?? (() => { throw new Error("invalid"); })();
  } catch {
    return false;
  }
  const actualCtrl = Boolean(event.ctrlKey);
  const actualAlt = Boolean(event.altKey);
  const actualShift = Boolean(event.shiftKey);
  const actualCmd = Boolean(event.metaKey);
  if (expected.ctrl !== actualCtrl || expected.alt !== actualAlt || expected.shift !== actualShift || expected.cmd !== actualCmd) return false;
  return keyMatches(event, expected.key);
}

export function isShortcutTargetBlocked(event: ShortcutKeyboardEventLike, binding: string | null | undefined): boolean {
  if (isRecorderTarget(event.target)) return true;
  if (!isTextInputTarget(event.target)) return false;
  let parsed: ParsedShortcut | null;
  try {
    parsed = binding ? parseShortcutBinding(binding) : null;
  } catch {
    return true;
  }
  if (!parsed) return true;
  if (isTextInputAppChord(parsed)) return false;
  if (isNativeTextEditingShortcut(parsed)) return true;
  if (parsed.alt && !parsed.ctrl && !parsed.cmd && isLetterLikeShortcutKey(parsed.key)) return true;
  return isLetterLikeShortcutKey(parsed.key) || parsed.key === "Space";
}

export function isTextInputTarget(target: unknown): boolean {
  const element = asElement(target);
  if (!element) return false;
  const tagName = element.tagName.toLowerCase();
  return tagName === "input" || tagName === "textarea" || tagName === "select" || Boolean(element.isContentEditable) || hasClosestAttribute(element, "contenteditable") || hasClosestAttribute(element, "data-shortcut-recorder");
}

export function isRecorderTarget(target: unknown): boolean {
  const element = asElement(target);
  return Boolean(element && hasClosestAttribute(element, "data-shortcut-recorder"));
}

export function isReservedShortcut(platform: ShortcutPlatform, binding: string | null | undefined): boolean {
  if (!binding) return false;
  let parsed: ParsedShortcut;
  try {
    parsed = parseShortcutBinding(binding) ?? (() => { throw new Error("invalid"); })();
  } catch {
    return true;
  }
  // Bare Escape belongs to the shell's one-step back behavior.  Keeping it
  // reserved also prevents older/local profiles from hijacking navigation.
  if (!parsed.ctrl && !parsed.alt && !parsed.shift && !parsed.cmd && parsed.key === "Escape") return true;
  // Bare F11 belongs to the shell's Fullscreen API toggle.  Keep it out of
  // custom profiles so an older stored binding cannot steal browser behavior.
  if (!parsed.ctrl && !parsed.alt && !parsed.shift && !parsed.cmd && parsed.key === "F11") return true;
  if (platform === "macos" && parsed.ctrl && parsed.alt) return true;
  const key = parsed.key;
  if (platform === "windows") {
    if (parsed.ctrl && !parsed.alt && !parsed.cmd && !parsed.shift && key === "Escape") return true;
    if (parsed.alt && !parsed.ctrl && !parsed.cmd && !parsed.shift && key === "F4") return true;
    if (parsed.alt && !parsed.ctrl && !parsed.cmd && !parsed.shift && key === "Tab") return true;
    if (parsed.ctrl && !parsed.alt && !parsed.cmd && !parsed.shift && ["KeyL", "KeyT", "KeyN", "KeyW", "KeyR", "KeyP", "KeyS", "KeyF", "KeyD", "KeyH", "KeyJ", "KeyK", "KeyU"].includes(key)) return true;
    if (parsed.ctrl && parsed.shift && !parsed.alt && !parsed.cmd && key === "Escape") return true;
    if (parsed.ctrl && parsed.alt && key === "Delete") return true;
    if (parsed.cmd && !parsed.ctrl && !parsed.alt && !parsed.shift && key === "KeyL") return true;
  } else {
    if (parsed.cmd && !parsed.ctrl && !parsed.alt && !parsed.shift && ["Tab", "Space", "KeyQ", "KeyW", "KeyT", "KeyN", "KeyL", "KeyR", "KeyP", "KeyS", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyU"].includes(key)) return true;
    if (parsed.cmd && parsed.alt && key === "Escape") return true;
  }
  return false;
}

export function scopesOverlap(left: ShortcutScope, right: ShortcutScope): boolean {
  if (left === right || left === "global" || right === "global") return true;
  if (left === "workspace" || right === "workspace") return true;
  return false;
}

export function commandsOverlap(left: CommandId, right: CommandId): boolean {
  return scopesOverlap(commandFor(left).scope, commandFor(right).scope);
}

export function findShortcutConflicts(platform: ShortcutPlatform, commandId: CommandId, binding: string | null | undefined): CommandMetadata[] {
  commandFor(commandId);
  if (!binding) return [];
  let normalized: string;
  try {
    normalized = normalizeShortcutBinding(binding);
  } catch {
    return [];
  }
  const profile = getShortcutProfile(platform);
  return COMMANDS.filter((command) => command.id !== commandId && commandsOverlap(commandId, command.id) && profile[command.id] != null && normalizeShortcutBinding(profile[command.id] as string) === normalized);
}

export function useShortcuts(scope: ShortcutScope, actions: Partial<Record<CommandId, () => void>>): void {
  const actionsRef = useRef(actions);
  actionsRef.current = actions;
  const platform = detectShortcutPlatform();
  useEffect(() => {
    let profile = getShortcutProfile(platform);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || isRecorderTarget(event.target) || (scope === "canvas" && isTextInputTarget(event.target))) return;
      const modal = document.querySelector<HTMLElement>('[role="dialog"][aria-modal="true"]');
      if (modal && modal.id !== "kontur-right-panel") return;
      const candidates = COMMANDS.filter((command) => {
        const action = actionsRef.current[command.id];
        const binding = profile[command.id];
        return Boolean(action && binding && isCommandActive(command.scope, scope) && !isShortcutTargetBlocked(event, binding) && matchesShortcut(event, binding));
      }).sort((left, right) => scopePriority(right.scope) - scopePriority(left.scope));
      const command = candidates[0];
      const action = command ? actionsRef.current[command.id] : undefined;
      if (!command || !action) return;
      event.preventDefault();
      action();
    };
    const refresh = () => { profile = getShortcutProfile(platform); };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener(SHORTCUTS_CHANGED, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener(SHORTCUTS_CHANGED, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [platform, scope]);
}

export function isCommandActive(commandScope: ShortcutScope, activeScope: ShortcutScope): boolean {
  if (activeScope === "global") return commandScope === "global";
  if (commandScope === "global") return true;
  if (commandScope === "workspace") return activeScope === "workspace" || activeScope === "editor" || activeScope === "canvas" || activeScope === "exam";
  return commandScope === activeScope;
}

function scopePriority(scope: ShortcutScope): number {
  return scope === "global" ? 0 : scope === "workspace" ? 1 : 2;
}

function isTextInputAppChord(parsed: ParsedShortcut): boolean {
  return (parsed.ctrl && parsed.alt) || (parsed.ctrl && parsed.shift) || (parsed.cmd && parsed.shift);
}

function isNativeTextEditingShortcut(parsed: ParsedShortcut): boolean {
  if ((!parsed.ctrl && !parsed.cmd) || parsed.alt || parsed.shift) return false;
  return ["KeyC", "KeyV", "KeyX", "KeyA", "KeyZ"].includes(parsed.key);
}

function isLetterLikeShortcutKey(key: string): boolean {
  return /^Key[A-Z]$/.test(key) || /^Digit[0-9]$/.test(key) || /^Numpad[0-9]$/.test(key) || /^\p{L}$/u.test(key);
}

function parseShortcutBinding(binding: string): ParsedShortcut | null {
  const value = typeof binding === "string" ? binding.trim() : "";
  if (!value) return null;
  const parts = value.split("+").map((part) => part.trim()).filter(Boolean);
  if (!parts.length) return null;
  const parsed: ParsedShortcut = { ctrl: false, alt: false, shift: false, cmd: false, key: "" };
  for (const part of parts.slice(0, -1)) {
    const modifier = MODIFIER_ALIASES[part.toLowerCase()];
    if (!modifier) throw new Error("Неизвестный модификатор");
    parsed[modifier] = true;
  }
  const keyPart = parts.at(-1) ?? "";
  if (MODIFIER_ALIASES[keyPart.toLowerCase()]) throw new Error("Комбинация не содержит клавишу");
  parsed.key = normalizeKeyToken(keyPart);
  if (!parsed.key) throw new Error("Неизвестная клавиша");
  return parsed;
}

function normalizeKeyToken(token: string): string {
  const trimmed = token.trim();
  const alias = KEY_ALIASES[trimmed.toLowerCase()];
  if (alias) return alias;
  if (/^key[a-z]$/i.test(trimmed)) return `Key${trimmed.slice(-1).toUpperCase()}`;
  if (/^digit[0-9]$/i.test(trimmed)) return `Digit${trimmed.slice(-1)}`;
  if (/^numpad[0-9]$/i.test(trimmed)) return `Numpad${trimmed.slice(-1)}`;
  if (/^f(?:[1-9]|1[0-2])$/i.test(trimmed)) return trimmed.toUpperCase();
  if (/^arrow(?:left|right|up|down)$/i.test(trimmed)) return `Arrow${trimmed.slice(5).toLowerCase().replace(/^./, (char) => char.toUpperCase())}`;
  const russianCode = RU_TO_CODE[trimmed.toLowerCase()];
  if (russianCode) return russianCode;
  if (/^[a-z]$/i.test(trimmed)) return `Key${trimmed.toUpperCase()}`;
  if (/^[0-9]$/.test(trimmed)) return `Digit${trimmed}`;
  if (/^[\p{L}]$/u.test(trimmed)) return trimmed;
  if (["Backquote", "Minus", "Equal", "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash"].includes(trimmed)) return trimmed;
  return trimmed.length <= 20 ? trimmed : "";
}

function serializeShortcut(parsed: ParsedShortcut): string {
  const modifiers = [parsed.ctrl ? "Ctrl" : "", parsed.alt ? "Alt" : "", parsed.shift ? "Shift" : "", parsed.cmd ? "Cmd" : ""].filter(Boolean);
  return [...modifiers, parsed.key].join("+");
}

function displayCode(code: string): string {
  if (/^Key[A-Z]$/.test(code)) return code.slice(-1);
  if (/^(?:Digit|Numpad)[0-9]$/.test(code)) return code.slice(-1);
  return code;
}

function eventKeyCode(event: ShortcutKeyboardEventLike): string {
  const code = event.code?.trim() ?? "";
  if (code) return normalizeKeyToken(code);
  const key = event.key?.trim() ?? "";
  if (!key) return "";
  return normalizeKeyToken(key);
}

function keyMatches(event: ShortcutKeyboardEventLike, expected: string): boolean {
  const code = event.code?.trim() ?? "";
  if (code && normalizeKeyToken(code) === expected) return true;
  const key = event.key?.trim() ?? "";
  if (!key) return false;
  if (normalizeKeyToken(key) === expected) return true;
  return CODE_TO_RU[expected] === key.toLowerCase();
}

function isAltGraph(event: ShortcutKeyboardEventLike): boolean {
  return Boolean(event.getModifierState?.("AltGraph"));
}

function isModifierOnlyKey(key: string | undefined): boolean {
  return ["Alt", "AltGraph", "Control", "Meta", "OS", "Shift", "CapsLock", "Fn"].includes(key ?? "");
}

function asElement(value: unknown): (HTMLElement & { isContentEditable?: boolean }) | null {
  if (!value || typeof value !== "object") return null;
  return value as HTMLElement & { isContentEditable?: boolean };
}

function hasClosestAttribute(element: HTMLElement, attribute: string): boolean {
  if (element.hasAttribute?.(attribute)) return true;
  return Boolean(element.closest?.(`[${attribute}]`));
}
