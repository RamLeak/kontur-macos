"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CodeEditor } from "./code-editor";
import { Icon } from "./icon";
import { InfiniteBoard } from "./infinite-board";
import { LocalAI } from "./local-ai";
import { LocalCatalog } from "./local-catalog";
import { LocalStatement } from "./local-statement";
import { Modal } from "./modal";
import { ResizableWorkspace } from "./resizable-workspace";
import { parseBoard } from "@/lib/board-types";
import type { BoardDocument } from "@/lib/board-types";
import { progressIndex, taskKey } from "@/lib/catalog";
import { acknowledgeClose } from "@/lib/close";
import { transferNotes } from "@/lib/transfer-notes";
import { readStatementBlocks } from "@/lib/statement-blocks";
import { native, safeError } from "@/lib/native";
import { SaveQueue } from "@/lib/save-queue";
import type { SaveState } from "@/lib/save-queue";
import { captureSurfaces, flushSurfaces } from "@/lib/surface-lifecycle";
import type { CreateDocument, Document, ImportPreview, JsonObject, MarkAttempt, Snapshot, Task } from "@/lib/native-types";

export function LocalWorkspace() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [editing, setEditing] = useState<SaveState<Document> | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [panel, setPanel] = useState<"navigation" | "assistant" | null>("navigation");
  const [tab, setTab] = useState<"code" | "board">("code");
  const [mobilePane, setMobilePane] = useState<"task" | "tools">("tools");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [wrap, setWrap] = useState(false);
  const [compact, setCompact] = useState(false);
  const [closing, setClosing] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [importPreview, setImportPreview] = useState<{ token: string; preview: ImportPreview } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [initialBoard, setInitialBoard] = useState<{ id: string; board?: BoardDocument; error?: string } | null>(null);
  const queue = useRef<SaveQueue<Document> | null>(null);
  const retainedQueues = useRef(new Map<string, SaveQueue<Document>>());
  const activeAction = useRef<Promise<void> | null>(null);
  const startup = useRef<Promise<void> | null>(null);
  const closingRef = useRef(false);
  const lock = useRef(false);
  const mounted = useRef(true);
  const side = useRef<HTMLElement>(null);
  const lastPanelFocus = useRef<HTMLElement | null>(null);
  const current = editing?.value ?? null;
  const locked = !!busy || closing;
  const progress = useMemo(() => progressIndex(snapshot?.progress ?? []), [snapshot?.progress]);
  const notes = useMemo(() => transferNotes(snapshot?.preferences.values ?? {}), [snapshot?.preferences.values]);
  const task = useMemo(() => snapshot?.tasks.find(item => item.sourceId === current?.taskSourceId && item.revision === current?.taskRevision), [snapshot?.tasks, current?.taskSourceId, current?.taskRevision]);
  const currentProgress = current?.taskSourceId && current.taskRevision ? progress.get(taskKey({ sourceId: current.taskSourceId, revision: current.taskRevision })) : undefined;
  const assets = useMemo(() => task ? (snapshot?.assets ?? []).filter(asset => task.assetIds.includes(asset.id)) : [], [task, snapshot?.assets]);
  const statementContext = useMemo(() => {
    const blocks = readStatementBlocks(task?.blocks ?? null);
    return [current?.statement ?? "", blocks.matrix ? [...(blocks.matrix.headers.length ? [blocks.matrix.headers.join("\t")] : []), ...blocks.matrix.rows.map(row => row.join("\t"))].join("\n") : "", blocks.latex ?? "", ...blocks.subtasks.map(child => child.statement)].filter(Boolean).join("\n\n");
  }, [current?.statement, task?.blocks]);
  const saveError = editing?.error ? safeError(editing.error, "Не удалось сохранить работу на диск. Текст остаётся в редакторе.") : null;

  const attach = useCallback((document: Document) => {
    const previous = queue.current;
    if (previous && !previous.state.dirty && !previous.state.saving) { previous.dispose(); retainedQueues.current.delete(previous.state.value.id); }
    const next: SaveQueue<Document> = new SaveQueue(document, (value, expectedRevision) => native.save({
      documentId: value.id, expectedRevision, title: value.title, typeNumber: value.typeNumber,
      code: value.code, statement: value.statement, board: value.board,
    }), state => {
      if (!mounted.current || queue.current !== next) return;
      setEditing({ ...state });
      if (!state.dirty && !state.saving && !state.error) setSnapshot(previous => previous ? { ...previous, documents: previous.documents.map(item => item.id === state.value.id ? state.value : item) } : previous);
    });
    queue.current = next;
    retainedQueues.current.set(document.id, next);
    setEditing(next.state);
    setSnapshot(previous => previous ? { ...previous, preferences: { ...previous.preferences, selectedDocumentId: document.id }, documents: [...previous.documents.filter(item => item.id !== document.id), document] } : previous);
    try { setInitialBoard({ id: document.id, board: { ...parseBoard(document.board, document.id), version: document.revision, updated_at: document.updatedAt } }); }
    catch { setInitialBoard({ id: document.id, error: "Формат этой доски пока не поддерживается. Она сохранена вместе с документом и доступна при экспорте." }); }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let alive = true;
    setLoading(true); setError(null);
    const operation = (async () => {
      try {
        const loaded = await native.snapshot();
        if (!alive) return;
        setSnapshot(loaded);
        const preference = loaded.preferences.values.theme;
        if (preference === "light" || preference === "dark") setTheme(preference);
        else setTheme(window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
        const selected = loaded.documents.find(item => item.id === loaded.preferences.selectedDocumentId) ?? loaded.documents[0];
        const document = selected ? await native.document(selected.id) : await native.create({});
        if (alive) attach(document);
      } catch (reason) { if (alive) setError(safeError(reason).message); }
      finally { if (alive) setLoading(false); }
    })();
    startup.current = operation;
    return () => { alive = false; mounted.current = false; for (const item of retainedQueues.current.values()) item.dispose(); };
  }, [attempt, attach]);

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const update = () => { setCompact(media.matches); if (media.matches) setPanel(null); };
    update(); media.addEventListener("change", update); return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!compact || !panel || !side.current) return;
    lastPanelFocus.current = document.activeElement as HTMLElement | null;
    const controls = () => Array.from(side.current?.querySelectorAll<HTMLElement>("button:not(:disabled),input,select,textarea,[tabindex='0']") ?? []).filter(element => element.getClientRects().length > 0 && !element.closest("[hidden]"));
    const timer = requestAnimationFrame(() => controls()[0]?.focus());
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setPanel(null); }
      if (event.key !== "Tab") return;
      const items = controls(); const first = items[0]; const last = items.at(-1);
      if (!first) { event.preventDefault(); side.current?.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keys);
    return () => { cancelAnimationFrame(timer); document.removeEventListener("keydown", keys); lastPanelFocus.current?.focus(); };
  }, [compact, panel]);

  function edit(patch: Partial<Pick<Document, "title" | "code" | "statement" | "board">>) {
    if (lock.current || closingRef.current) return;
    queue.current?.edit(patch);
  }
  const flush = useCallback(async () => {
    await flushSurfaces();
    for (const item of retainedQueues.current.values()) await item.flush();
    return queue.current?.state.value;
  }, []);
  function action(label: string, run: () => Promise<void>, flushFirst = true): Promise<void> {
    if (lock.current || closingRef.current) return Promise.resolve();
    lock.current = true; setBusy(label); setError(null); setNotice(null);
    const operation = (async () => {
      try { if (flushFirst) await flush(); await run(); }
      catch (reason) { if (mounted.current) setError(safeError(reason).message); }
      finally { lock.current = false; if (mounted.current) setBusy(null); }
    })();
    activeAction.current = operation;
    void operation.finally(() => { if (activeAction.current === operation) activeAction.current = null; });
    return operation;
  }
  function togglePanel(next: "navigation" | "assistant") { setPanel(value => value === next ? null : next); }
  function openSettings() { setPanel(null); setSettingsOpen(true); }
  function closeSettings() {
    if (busy) return;
    setSettingsOpen(false); setImportPreview(null);
    requestAnimationFrame(() => document.getElementById("local-settings-button")?.focus());
  }
  async function refresh() { const loaded = await native.snapshot(); setSnapshot(loaded); }
  function create(input: CreateDocument = {}) { void action("Создаём работу…", async () => { attach(await native.create(input)); }); }
  function openDocument(document: Document) {
    if (document.id === queue.current?.state.value.id) { if (compact) setPanel(null); return; }
    void action("Открываем работу…", async () => { attach(await native.document(document.id)); if (compact) setPanel(null); });
  }
  function openTask(task: Task) {
    const existing = snapshot?.documents.filter(item => item.taskSourceId === task.sourceId && item.taskRevision === task.revision).sort((a, b) => b.updatedAt - a.updatedAt)[0];
    if (existing) openDocument(existing);
    else void action("Открываем задание…", async () => { attach(await native.create({ taskSourceId: task.sourceId, taskRevision: task.revision })); if (compact) setPanel(null); });
  }
  function previewImport() { void action("Проверяем файл…", async () => { setImportPreview(await native.previewImport()); }); }
  function confirmImport() {
    if (!importPreview) return;
    const token = importPreview.token;
    void action("Импортируем…", async () => {
      const result = await native.confirmImport(token); setImportPreview(null);
      await refresh();
      // Import may retain a conflicting copy; load the selected persisted document again.
      if (queue.current) attach(await native.document(queue.current.state.value.id));
      setNotice(`Импортировано: ${result.importedTasks} заданий, ${result.importedDocuments} работ, ${result.importedAttempts} попыток. Совпадений: ${result.duplicateRecords}. Копий при конфликте: ${result.documentConflicts}.`);
    });
  }
  function exportBundle() { void action("Экспортируем…", async () => { const result = await native.export(); if (result) setNotice(`Файл сохранён: ${result.path}`); }); }
  async function mark(input: MarkAttempt) {
    if (lock.current || closingRef.current) throw new Error("Busy");
    lock.current = true; setBusy("Записываем попытку…");
    const operation = (async () => { try { await flush(); await native.mark(input); await refresh(); }
      finally { lock.current = false; if (mounted.current) setBusy(null); } })();
    activeAction.current = operation;
    try { await operation; } finally { if (activeAction.current === operation) activeAction.current = null; }
  }
  const persistBoard = useCallback(async (board: BoardDocument): Promise<BoardDocument> => {
    const active = retainedQueues.current.get(board.id);
    if (!active) throw new Error("No document");
    if (JSON.stringify(active.state.value.board) !== JSON.stringify(board)) active.edit({ board: JSON.parse(JSON.stringify(board)) as JsonObject });
    const saved = await active.flush();
    return { ...board, version: saved.revision, updated_at: saved.updatedAt };
  }, []);
  function copyDraft() {
    void action("Сохраняем копию…", async () => {
      captureSurfaces();
      const previousQueue = queue.current;
      const previous = previousQueue?.state.value;
      if (!previous) return;
      const input: CreateDocument = { title: `${previous.title} (копия)`, ...(previous.typeNumber ? { typeNumber: previous.typeNumber } : {}), ...(previous.taskSourceId && previous.taskRevision ? { taskSourceId: previous.taskSourceId, taskRevision: previous.taskRevision } : {}) };
      const copy = await native.create(input);
      let board = previous.board;
      let parsed: BoardDocument | null = null;
      try { parsed = { ...parseBoard(previous.board, previous.id), id: copy.id, version: copy.revision, updated_at: copy.updatedAt }; board = JSON.parse(JSON.stringify(parsed)) as JsonObject; }
      catch { /* Preserve a populated unknown board exactly for export/recovery. */ }
      attach(copy);
      queue.current!.edit({ code: previous.code, statement: previous.statement, board });
      if (parsed) setInitialBoard({ id: copy.id, board: parsed });
      else setInitialBoard({ id: copy.id, error: "Формат этой доски пока не поддерживается. Исходные данные сохранены в копии." });
      // Explicit copy transfers this exact draft to the new queue before retiring it.
      if (previousQueue) { previousQueue.dispose(); retainedQueues.current.delete(previous.id); }
      await queue.current!.flush();
      setNotice("Копия сохранена. Обе версии доступны в «Мои работы».");
    }, false);
  }

  useEffect(() => {
    const requestClose = async ({ nonce }: { nonce: string }) => {
      if (closingRef.current) throw new Error("CLOSE_IN_PROGRESS");
      if (typeof nonce !== "string" || !nonce) throw new Error("INVALID_CLOSE_CHALLENGE");
      closingRef.current = true; setClosing(true); setError(null);
      try {
        await acknowledgeClose(nonce, {
          waitForPending: async () => { await startup.current; await activeAction.current; },
          flush,
          documents: () => [...retainedQueues.current.values()].map(item => item.state),
          confirm: native.confirmClose,
        });
      } catch (reason) {
        setError(safeError(reason, "Работа ещё не сохранена. Закрытие отменено; повторите сохранение.").message);
        throw reason;
      } finally { closingRef.current = false; if (mounted.current) setClosing(false); }
    };
    window.__KONTUR_LOCAL_REQUEST_CLOSE__ = requestClose;
    return () => { if (window.__KONTUR_LOCAL_REQUEST_CLOSE__ === requestClose) delete window.__KONTUR_LOCAL_REQUEST_CLOSE__; };
  }, [flush]);

  useEffect(() => {
    const hidden = () => { if (document.visibilityState === "hidden") void flush().catch(() => undefined); };
    const keys = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void action("Сохраняем…", async () => {}); }
    };
    document.addEventListener("visibilitychange", hidden); document.addEventListener("keydown", keys);
    return () => { document.removeEventListener("visibilitychange", hidden); document.removeEventListener("keydown", keys); };
  }, [flush]);

  return <div className="app-shell kontur-app-shell is-workspace local-shell" data-testid="local-workspace">
    <header className="kontur-topbar local-topbar" inert={closing || (compact && !!panel)}>
      <span className="kontur-brand"><span className="kontur-brand-mark">К</span><span>Контур <small>Локальный</small></span></span>
      <label className="local-title"><span className="sr-only">Название работы</span><input aria-label="Название работы" value={current?.title ?? ""} placeholder="Своя работа" readOnly={loading || locked || !current} onChange={event => edit({ title: event.target.value })} /></label>
      <button type="button" className="icon-button" aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"} onClick={() => setTheme(value => value === "light" ? "dark" : "light")}><Icon name="circle" size={18} /></button>
      <button type="button" className="outline-button local-export" onClick={exportBundle} disabled={loading || !!busy || !snapshot}>Экспорт</button>
      <button type="button" id="local-settings-button" className="icon-button" aria-label="Настройки" title="Настройки" aria-haspopup="dialog" onClick={openSettings}><Icon name="settings" size={19} /></button>
      <button type="button" className="icon-button" aria-label="Помощник" aria-expanded={panel === "assistant"} onClick={() => togglePanel("assistant")}><Icon name="spark" size={19} /></button>
      <button type="button" className="icon-button" aria-label="Каталог и мои работы" aria-expanded={panel === "navigation"} onClick={() => togglePanel("navigation")}><Icon name="panel" size={20} /></button>
    </header>
    {(error || notice || busy || loading || closing) && <div className={"local-notice" + (error ? " is-error" : "")} role={error ? "alert" : "status"}>{error ?? (closing ? "Сохраняем работу перед закрытием…" : busy) ?? (loading ? "Открываем локальное хранилище…" : notice)}{!loading && !current && error && <button type="button" className="outline-button" onClick={() => setAttempt(value => value + 1)}>Повторить</button>}{notice && !busy && <button type="button" className="icon-button" aria-label="Закрыть уведомление" onClick={() => setNotice(null)}><Icon name="close" size={16} /></button>}</div>}
    {notes.length > 0 && <details className="local-transfer-notes"><summary>Заметки к переносу · {notes.length}</summary><ul>{notes.map(note => <li key={note}>{note}</li>)}</ul></details>}
    <div className="local-body">
      <main className="workspace-page local-workspace" inert={closing || (compact && !!panel)}>
        <div className="workspace-mobile-switch"><button type="button" aria-pressed={mobilePane === "task"} onClick={() => setMobilePane("task")}>Условие</button><button type="button" aria-pressed={mobilePane === "tools"} onClick={() => setMobilePane("tools")}>Решение</button></div>
        {saveError && <div className="local-save-error" role="alert"><span>{saveError.message} Черновик остаётся в редакторе.</span><button type="button" className="outline-button" disabled={!!busy} onClick={() => void action("Сохраняем…", async () => {})}>Повторить</button><button type="button" className="outline-button" disabled={!!busy} onClick={copyDraft}>Сохранить копию</button></div>}
        <ResizableWorkspace focusedPane="split" mobilePane={mobilePane}>
          <LocalStatement key={current?.id ?? "empty"} document={current} task={task} attempts={snapshot?.attempts ?? []} assets={assets} progress={currentProgress} readOnly={loading || locked || !current} onStatement={statement => edit({ statement })} onMark={mark} />
          <section className="workspace-rail local-tools" aria-label="Решение">
            <div className="rail-tabs" role="tablist" aria-label="Рабочие инструменты">{(["code", "board"] as const).map(value => <button type="button" id={`local-tab-${value}`} key={value} role="tab" aria-selected={tab === value} aria-controls={`local-panel-${value}`} tabIndex={tab === value ? 0 : -1} className={"rail-tab" + (tab === value ? " is-active" : "")} onClick={() => setTab(value)} onKeyDown={event => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "code" : event.key === "End" ? "board" : value === "code" ? "board" : "code"; setTab(next); document.getElementById(`local-tab-${next}`)?.focus(); } }}>{value === "code" ? "Код" : "Доска"}</button>)}<span className="rail-tabs-spacer" /><span className="local-save-status" role="status">{saveError ? "Не сохранено" : editing?.saving ? "Сохраняем…" : editing?.dirty ? "Есть изменения" : current ? "Сохранено" : ""}</span></div>
            <div className="workspace-tool-panel local-tool-panel">
              <section role="tabpanel" id="local-panel-code" aria-labelledby="local-tab-code" hidden={tab !== "code"} className="workspace-tab-content"><div className={"code-panel" + (theme === "light" ? " editor-light" : "")}><div className="panel-toolbar"><span className="language-label">Python</span><span className="panel-toolbar-spacer" /><label><input type="checkbox" checked={wrap} onChange={event => setWrap(event.target.checked)} />Перенос строк</label><button type="button" className="icon-button" aria-label="Сохранить работу" disabled={!current || !!busy} onClick={() => void action("Сохраняем…", async () => {})}><Icon name="save" size={17} /></button></div><CodeEditor key={current?.id ?? "empty"} value={current?.code ?? ""} onChange={code => edit({ code })} readOnly={loading || !!busy || !current} draftKey={current?.id} onBlur={() => { void queue.current?.flush().catch(() => undefined); }} wrap={wrap} /><footer className="local-editor-footer"><span>{current?.isOwnWork !== false ? "Своя работа" : "Локальное решение"}</span><span>{current ? `Версия ${current.revision}` : ""}</span></footer></div></section>
              <section role="tabpanel" id="local-panel-board" aria-labelledby="local-tab-board" hidden={tab !== "board"} className="workspace-tab-content">{initialBoard?.error ? <p className="panel-error" role="status">{initialBoard.error}</p> : current && initialBoard?.board ? <InfiniteBoard key={current.id} sessionId={current.id} initialBoard={initialBoard.board} active={tab === "board"} disabled={loading || !!busy} persist={persistBoard} onChange={board => retainedQueues.current.get(board.id)?.edit({ board: JSON.parse(JSON.stringify(board)) as JsonObject })} /> : <p className="local-muted">Доска появится после открытия хранилища.</p>}</section>
            </div>
          </section>
        </ResizableWorkspace>
      </main>
      {compact && panel && <button type="button" className="local-panel-scrim" aria-label="Закрыть правую панель" onClick={() => setPanel(null)} />}
      <aside ref={side} className="local-side" hidden={!panel} role={compact && panel ? "dialog" : undefined} aria-modal={compact && panel ? true : undefined} aria-label={panel === "assistant" ? "Помощник" : "Каталог и мои работы"} tabIndex={-1}>
        <div className="local-side-header"><strong>{panel === "assistant" ? "Помощник" : "Навигация"}</strong><button type="button" className="icon-button" aria-label="Закрыть правую панель" onClick={() => setPanel(null)}><Icon name="close" size={19} /></button></div>
        <div hidden={panel !== "navigation"} className="local-navigation-host"><LocalCatalog snapshot={snapshot} selectedId={current?.id} busy={loading || !!busy} onTask={openTask} onDocument={openDocument} onCreate={() => create()} onSettings={openSettings} /></div>
        <LocalAI active={panel === "assistant"} document={current} statementText={statementContext} />
      </aside>
    </div>
    <Modal open={settingsOpen} title="Настройки" onClose={closeSettings}>
      <section className="local-settings-data" aria-labelledby="local-settings-data-title">
        <h3 id="local-settings-data-title">Данные</h3>
        <p>Добавьте файл .kontur с заданиями, работами и историей решений.</p>
        {error && <p className="panel-error" role="alert">{error}</p>}
        {notice && !busy && <p className="local-muted" role="status">{notice}</p>}
        {!importPreview && <button type="button" className="outline-button" disabled={loading || !!busy || !snapshot} onClick={previewImport}>{busy ? "Проверяем файл…" : "Добавить файл .kontur"}</button>}
        {importPreview && <section aria-label="Проверка файла импорта">
          <p>Файл проверен. Данные будут добавлены в локальное хранилище.</p>
          <dl className="local-import-counts"><dt>Заданий</dt><dd>{importPreview.preview.taskCount} · новых {importPreview.preview.newTasks}</dd><dt>Попыток</dt><dd>{importPreview.preview.attemptCount} · новых {importPreview.preview.newAttempts}</dd><dt>Работ</dt><dd>{importPreview.preview.documentCount} · новых {importPreview.preview.newDocuments}</dd><dt>Вложений</dt><dd>{importPreview.preview.assetCount}</dd><dt>Совпадающих записей</dt><dd>{importPreview.preview.duplicateRecords}</dd><dt>Конфликтов работ</dt><dd>{importPreview.preview.documentConflicts}</dd></dl>
          <p className="local-muted">При конфликте сохранятся обе версии. Перед импортом создаётся резервная копия.</p>
          <div className="local-import-actions">{error && <button type="button" className="outline-button" disabled={!!busy} onClick={() => { setImportPreview(null); previewImport(); }}>Другой файл</button>}<button type="button" className="outline-button" disabled={!!busy} onClick={() => setImportPreview(null)}>Отмена</button><button type="button" className="primary-button" disabled={!!busy} onClick={confirmImport}>{busy ? "Импортируем…" : "Импортировать"}</button></div>
        </section>}
      </section>
    </Modal>
  </div>;
}
