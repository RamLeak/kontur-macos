"use client";

import { useDeferredValue, useMemo, useState } from "react";
import { filterTasks, progressIndex, progressLabels, taskKey } from "@/lib/catalog";
import type { Document, ProgressStatus, Snapshot, Task } from "@/lib/native-types";

export function LocalCatalog({ snapshot, selectedId, busy, onTask, onDocument, onCreate, onSettings }: {
  snapshot: Snapshot | null; selectedId?: string; busy: boolean;
  onTask: (task: Task) => void; onDocument: (document: Document) => void;
  onCreate: () => void; onSettings: () => void;
}) {
  const [section, setSection] = useState<"catalog" | "documents">("catalog");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(80);
  const search = useDeferredValue(query);
  const progress = useMemo(() => progressIndex(snapshot?.progress ?? []), [snapshot?.progress]);
  const tasks = useMemo(() => filterTasks(snapshot?.tasks ?? [], type, status, search, progress), [snapshot?.tasks, type, status, search, progress]);
  const counts = useMemo(() => { const result = Array(28).fill(0) as number[]; for (const task of snapshot?.tasks ?? []) result[task.typeNumber] += 1; return result; }, [snapshot?.tasks]);
  const documents = useMemo(() => [...snapshot?.documents ?? []].sort((a, b) => b.updatedAt - a.updatedAt), [snapshot?.documents]);

  return <section className="local-navigation" aria-label="Каталог и мои работы">
    <div className="local-navigation-actions"><button type="button" className="outline-button" onClick={onCreate} disabled={busy || !snapshot}>Новая работа</button></div>
    <div className="local-nav-tabs" aria-label="Раздел"><button type="button" aria-pressed={section === "catalog"} onClick={() => setSection("catalog")}>Каталог</button><button type="button" aria-pressed={section === "documents"} onClick={() => setSection("documents")}>Мои работы <span>{documents.length}</span></button></div>
    {section === "catalog" ? <>
      <div className="local-catalog-filters">
        <label className="local-field"><span>Тип задания</span><select aria-label="Тип задания" value={type} onChange={event => { setType(event.target.value); setLimit(80); }}><option value="">Все типы · {snapshot?.tasks.length ?? 0}</option>{Array.from({ length: 27 }, (_, index) => index + 1).map(number => <option key={number} value={number}>Тип {number} · {counts[number]}</option>)}</select></label>
        <label className="local-field"><span>Прогресс</span><select value={status} onChange={event => { setStatus(event.target.value); setLimit(80); }}><option value="">Любой статус</option>{Object.entries(progressLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="local-field"><span className="sr-only">Поиск по каталогу</span><input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(80); }} placeholder="Найти задание…" /></label>
      </div>
      <p className="local-list-count" role="status">{tasks.length} заданий{search !== query ? " · ищем…" : ""}</p>
      <div className="local-catalog-list">
        {tasks.slice(0, limit).map(task => { const item = progress.get(taskKey(task)); const state: ProgressStatus = item?.status ?? "not_started"; return <button type="button" className="local-task-row" key={taskKey(task)} onClick={() => onTask(task)} disabled={busy}><span className="local-task-type">Тип {task.typeNumber}</span><strong>{task.title}</strong><span className={"local-progress is-" + state}>{progressLabels[state]}{item?.attemptCount ? ` · ${item.attemptCount}` : ""}</span>{item?.everSolved && state !== "solved" && <span className="local-ever-solved">Решалось верно раньше</span>}{item?.everSolved && item.lastVerdict && item.lastVerdict !== "correct" && <span className="local-muted">Последняя попытка: {item.lastVerdict === "error" ? "ошибка" : item.lastVerdict === "wrong" ? "неверный ответ" : "не проверено"}</span>}</button>; })}
        {tasks.length > limit && <button type="button" className="outline-button" onClick={() => setLimit(value => value + 80)}>Показать ещё</button>}
        {snapshot && !snapshot.tasks.length && <div className="local-empty"><h2>Каталог пока пуст</h2><p>Добавьте файл .kontur с заданиями и историей решений в настройках.</p><button type="button" className="primary-button" onClick={onSettings} disabled={busy}>Открыть настройки</button><p>Для своих заметок и кода создайте новую работу.</p></div>}
        {snapshot && !!snapshot.tasks.length && !tasks.length && <p className="local-muted">По этим фильтрам заданий нет.</p>}
      </div>
    </> : <div className="local-catalog-list local-documents">{documents.map(document => <button type="button" className="local-task-row" aria-current={document.id === selectedId ? "page" : undefined} key={document.id} onClick={() => onDocument(document)} disabled={busy}><strong>{document.title}</strong><span className="local-muted">{document.isOwnWork ? "Своя работа" : "Из каталога"}{document.typeNumber ? ` · Тип ${document.typeNumber}` : ""}</span><time dateTime={new Date(document.updatedAt).toISOString()}>{new Date(document.updatedAt).toLocaleString("ru-RU")}</time></button>)}{!documents.length && <p className="local-muted">Сохранённые работы появятся здесь.</p>}</div>}
  </section>;
}
