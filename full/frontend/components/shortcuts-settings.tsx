"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {createPortal} from "react-dom";
import { Icon } from "@/components/icon";
import {
  COMMANDS,
  captureShortcutFromKeyboardEvent,
  type CommandId,
  type CommandMetadata,
  detectShortcutPlatform,
  findShortcutConflicts,
  formatShortcut,
  getShortcutProfile,
  isReservedShortcut,
  resetShortcutProfile,
  setShortcutBinding,
  SHORTCUTS_CHANGED,
  type ShortcutPlatform,
  type ShortcutProfile,
} from "@/lib/kontur-shortcuts";
import styles from "./settings.module.css";

type ConfirmAction = { kind: "clear" | "reset"; commandId?: CommandId };

const scopeLabels: Record<CommandMetadata["scope"], string> = {
  global: "Везде",
  workspace: "Рабочее пространство",
  editor: "Редактор",
  canvas: "Доска",
  exam: "Экзамен",
};

export function ShortcutsSettings() {
  const [mounted,setMounted]=useState(false);
  useEffect(()=>setMounted(true),[]);
  const [platform, setPlatform] = useState<ShortcutPlatform>(() => detectShortcutPlatform());
  const [profile, setProfile] = useState<ShortcutProfile>(() => getShortcutProfile(detectShortcutPlatform()));
  const [query, setQuery] = useState("");
  const [recording, setRecording] = useState<CommandMetadata | null>(null);
  const [capturedBinding, setCapturedBinding] = useState<string | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [captureConflicts, setCaptureConflicts] = useState<CommandMetadata[]>([]);
  const [allowConflict, setAllowConflict] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmDialogRef = useRef<HTMLDivElement>(null);
  const triggerRefs = useRef<Partial<Record<CommandId, HTMLButtonElement | null>>>({});

  useEffect(() => {
    const detected = detectShortcutPlatform();
    setPlatform(detected);
    setProfile(getShortcutProfile(detected));
    const refresh = () => setProfile(getShortcutProfile(detected));
    window.addEventListener(SHORTCUTS_CHANGED, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(SHORTCUTS_CHANGED, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  useEffect(()=>{
    if(!mounted||(!recording&&!confirm))return;
    const app=document.querySelector<HTMLElement>(".app-shell");const previous=app?.inert??false;
    if(app)app.inert=true;
    return()=>{if(app)app.inert=previous;};
  },[mounted,recording,confirm]);

  useEffect(() => {
    if (!recording) return;
    const timer = window.setTimeout(() => recorderRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [recording]);

  useEffect(() => {
    if (!recording) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeRecorder();
        return;
      }
      if (event.key === "Tab") {
        event.stopPropagation();
        trapDialogFocus(event);
        return;
      }
      if (event.target !== recorderRef.current) return;
      // The recorder owns the physical key event after IME/repeat guards, so
      // app shortcuts cannot run before it has decided what to do.
      if (event.repeat || event.isComposing || event.keyCode === 229) return;
      const binding = captureShortcutFromKeyboardEvent(event);
      if (!binding) {
        setCaptureError("Нажмите сочетание клавиш, а не отдельный модификатор.");
        return;
      }
      setCapturedBinding(binding);
      setCaptureError(isReservedShortcut(platform, binding) ? "Эта комбинация занята системой или браузером." : null);
      setCaptureConflicts(isReservedShortcut(platform, binding) ? [] : findShortcutConflicts(platform, recording.id, binding));
      setAllowConflict(false);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [platform, recording]);

  useEffect(() => {
    if (!confirm) return;
    const timer = window.setTimeout(() => confirmDialogRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setConfirm(null);
        return;
      }
      if (event.key === "Tab") trapDialogFocus(event, confirmDialogRef.current);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [confirm]);

  const visibleCommands = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return COMMANDS;
    return COMMANDS.filter((command) => [command.label, command.description, scopeLabels[command.scope], command.id].join(" ").toLocaleLowerCase().includes(needle));
  }, [query]);

  function startRecording(command: CommandMetadata) {
    setError(null);
    setNotice(null);
    setCaptureError(null);
    setCapturedBinding(null);
    setCaptureConflicts([]);
    setAllowConflict(false);
    setRecording(command);
  }

  function closeRecorder() {
    const commandId = recording?.id;
    setRecording(null);
    setCapturedBinding(null);
    setCaptureError(null);
    setCaptureConflicts([]);
    setAllowConflict(false);
    if (commandId) window.setTimeout(() => triggerRefs.current[commandId]?.focus(), 0);
  }

  function assignCaptured() {
    if (!recording || !capturedBinding || captureError || (captureConflicts.length > 0 && !allowConflict)) return;
    try {
      const next = setShortcutBinding(platform, recording.id, capturedBinding);
      setProfile(next);
      setNotice("Назначено: " + recording.label + " · " + formatShortcut(capturedBinding, platform));
      closeRecorder();
    } catch (reason) {
      setCaptureError(reason instanceof Error ? reason.message : "Не удалось назначить сочетание.");
    }
  }

  function clearBinding(commandId: CommandId) {
    setConfirm({ kind: "clear", commandId });
  }

  function resetProfile() {
    setConfirm({ kind: "reset" });
  }

  function confirmAction() {
    if (!confirm) return;
    if (confirm.kind === "reset") {
      setProfile(resetShortcutProfile(platform));
      setNotice("Профиль " + platformLabel(platform) + " сброшен.");
    } else if (confirm.commandId) {
      setProfile(setShortcutBinding(platform, confirm.commandId, null));
      setNotice("Назначение очищено.");
    }
    setConfirm(null);
  }

  function selectPlatform(next: ShortcutPlatform) {
    setPlatform(next);
    setProfile(getShortcutProfile(next));
    setNotice(null);
    setError(null);
  }

  function trapDialogFocus(event: KeyboardEvent, dialogOverride?: HTMLDivElement | null) {
    const dialog = dialogOverride ?? dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), [href], input:not([disabled])"));
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const capturedText = capturedBinding ? formatShortcut(capturedBinding, platform) : "Ожидание сочетания";

  return (
    <section className={styles.shortcutPanel} aria-labelledby="shortcut-settings-title">
      <div className={styles.panelHeading}>
        <div>
          <span className="eyebrow">Управление</span>
          <h2 id="shortcut-settings-title">Горячие клавиши</h2>
          <p>Назначайте сочетания отдельно для Windows и macOS. Системные и конфликтующие комбинации отмечаются до применения.</p>
        </div>
        <button className="secondary-button" onClick={resetProfile} type="button">Сбросить профиль</button>
      </div>

      <div className={styles.shortcutToolbar}>
        <div className={styles.profileSwitch} aria-label="Профиль платформы" role="group">
          <button aria-pressed={platform === "windows"} className={platform === "windows" ? styles.profileActive : ""} onClick={() => selectPlatform("windows")} type="button">Windows</button>
          <button aria-pressed={platform === "macos"} className={platform === "macos" ? styles.profileActive : ""} onClick={() => selectPlatform("macos")} type="button">macOS</button>
        </div>
        <label className={styles.shortcutSearch}>
          <Icon name="search" size={16} />
          <span className="sr-only">Поиск по действию</span>
          <input aria-label="Поиск по действию" onChange={(event) => setQuery(event.target.value)} placeholder="Поиск по действию" type="search" value={query} />
        </label>
      </div>

      {notice ? <p className={styles.shortcutNotice} role="status">{notice}</p> : null}
      {error ? <p className={styles.shortcutError} role="alert">{error}</p> : null}

      <div className={styles.shortcutTable} role="table" aria-label={"Горячие клавиши · " + platformLabel(platform)}>
        <div className={styles.shortcutTableHeader} role="row">
          <span role="columnheader">Действие</span>
          <span role="columnheader">Сочетание</span>
          <span role="columnheader">Где действует</span>
          <span role="columnheader">Управление</span>
        </div>
        {visibleCommands.map((command) => {
          const binding = profile[command.id];
          const conflicts = binding ? findShortcutConflicts(platform, command.id, binding) : [];
          return (
            <div className={styles.shortcutRow} key={command.id} role="row">
              <div className={styles.shortcutCommand} role="cell">
                <strong>{command.label}</strong>
                <small>{command.description}</small>
              </div>
              <div className={styles.shortcutBinding} role="cell">
                <kbd>{formatShortcut(binding, platform)}</kbd>
                {conflicts.length ? <span className={styles.shortcutConflict} title={conflicts.map((item) => item.label).join(", ")}>Конфликт</span> : null}
              </div>
              <span className={styles.shortcutScope} role="cell">{scopeLabels[command.scope]}</span>
              <div className={styles.shortcutActions} role="cell">
                <button aria-label={"Изменить: " + command.label} className="icon-button compact" onClick={() => startRecording(command)} ref={(element) => { triggerRefs.current[command.id] = element; }} title="Изменить" type="button"><Icon name="pen" size={15} /></button>
                <button aria-label={"Очистить: " + command.label} className="icon-button compact" disabled={!binding} onClick={() => clearBinding(command.id)} title="Очистить" type="button"><Icon name="close" size={15} /></button>
                <button aria-label={"Вернуть по умолчанию: " + command.label} className="icon-button compact" onClick={() => setProfile(setShortcutBinding(platform, command.id, command.defaults[platform]))} title="Вернуть по умолчанию" type="button"><Icon name="refresh" size={15} /></button>
              </div>
            </div>
          );
        })}
        {!visibleCommands.length ? <p className={styles.shortcutEmpty}>Действия не найдены.</p> : null}
      </div>

      {mounted&&recording ? createPortal(
        <div className={styles.shortcutDialogLayer} role="presentation">
          <div aria-hidden="true" className={styles.shortcutDialogUnderlay} />
          <div aria-labelledby="shortcut-recorder-title" aria-modal="true" className={styles.shortcutDialog} ref={dialogRef} role="dialog">
            <h3 id="shortcut-recorder-title">Назначить сочетание</h3>
            <p>{recording.label}</p>
            <input
              aria-label="Запись сочетания клавиш"
              className={styles.shortcutRecorder}
              data-shortcut-recorder="true"
              onChange={() => undefined}
              placeholder="Нажмите сочетание клавиш"
              readOnly
              ref={recorderRef}
              value={capturedText}
            />
            {captureError ? <p className={styles.shortcutError} role="alert">{captureError}</p> : null}
            {captureConflicts.length ? (
              <div className={styles.shortcutConflictMessage} role="alert">
                <p>Конфликт с: {captureConflicts.map((item) => item.label).join(", ")}.</p>
                <button className="secondary-button" onClick={() => setAllowConflict(true)} type="button">Назначить при конфликте</button>
              </div>
            ) : null}
            <div className={styles.shortcutDialogActions}>
              <button className="secondary-button" onClick={closeRecorder} type="button">Отмена</button>
              <button className="primary-button" disabled={!capturedBinding || Boolean(captureError) || (captureConflicts.length > 0 && !allowConflict)} onClick={assignCaptured} type="button">Назначить</button>
            </div>
          </div>
        </div>,document.body
      ) : null}

      {mounted&&confirm ? createPortal(
        <div className={styles.shortcutDialogLayer} role="presentation">
          <div aria-hidden="true" className={styles.shortcutDialogUnderlay} />
          <div aria-labelledby="shortcut-confirm-title" aria-modal="true" className={styles.shortcutDialog} ref={confirmDialogRef} role="dialog">
            <h3 id="shortcut-confirm-title">{confirm.kind === "reset" ? "Сбросить профиль?" : "Очистить назначение?"}</h3>
            <p>{confirm.kind === "reset" ? "Все сочетания профиля " + platformLabel(platform) + " вернутся к исходным значениям." : "Сочетание будет удалено для выбранного действия."}</p>
            <div className={styles.shortcutDialogActions}>
              <button className="secondary-button" onClick={() => setConfirm(null)} type="button">Отмена</button>
              <button className="primary-button" onClick={confirmAction} type="button">Подтвердить</button>
            </div>
          </div>
        </div>,document.body
      ) : null}
    </section>
  );
}

function platformLabel(platform: ShortcutPlatform): string {
  return platform === "macos" ? "macOS" : "Windows";
}
