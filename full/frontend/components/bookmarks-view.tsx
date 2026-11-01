"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Icon } from "@/components/icon";
import {TaskImages} from "@/components/task-images";
import styles from "./bookmarks-view.module.css";
import { getTaskRevision } from "@/lib/api";
import { BOOKMARKS_CHANGED, getBookmarks, getTaskProgress, setBookmark } from "@/lib/kontur-api";
import { statementToText } from "@/lib/sanitize";
import { readableError } from "@/lib/ui-copy";
import type { TaskRecord } from "@/lib/types";
import type { LearningStatus, TaskBookmark, TaskProgress } from "@/lib/kontur-types";

interface BookmarkRow {
  key: string;
  bookmark: TaskBookmark;
  task: TaskRecord | null;
  progress?: TaskProgress;
  error?: string;
}

function bookmarkKey(bookmark: TaskBookmark): string {
  return bookmark.task_id + ":" + bookmark.task_revision_id;
}

export function BookmarksView() {
  const [rows, setRows] = useState<BookmarkRow[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [mobilePreview, setMobilePreview] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [progressError, setProgressError] = useState<string | null>(null);
  const [removingKey, setRemovingKey] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [requestId, setRequestId] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const currentRequest = requestId;
    setLoading(true);
    setError(null);
    setProgressError(null);
    setActionError(null);

    async function loadBookmarks() {
      try {
        const bookmarks = await getBookmarks();
        const resolved = await Promise.all(bookmarks.map(async (bookmark): Promise<BookmarkRow> => {
          try {
            const task = await getTaskRevision(bookmark.task_id, bookmark.task_revision_id);
            return { key: bookmarkKey(bookmark), bookmark, task };
          } catch {
            return {
              key: bookmarkKey(bookmark),
              bookmark,
              task: null,
              error: "Сохранённая редакция задания сейчас недоступна.",
            };
          }
        }));

        const progressByRevision = new Map<string, TaskProgress>();
        let nextProgressError: string | null = null;
        try {
          const progress = await getTaskProgress(resolved.filter((row) => row.task).map((row) => row.bookmark.task_revision_id));
          progress.forEach((item) => progressByRevision.set(item.task_revision_id, item));
        } catch {
          nextProgressError = "Статусы обучения временно недоступны.";
        }

        const nextRows = resolved.map((row) => ({
          ...row,
          progress: progressByRevision.get(row.bookmark.task_revision_id),
        }));
        if (cancelled || currentRequest !== requestId) return;
        setRows(nextRows);
        setProgressError(nextProgressError);
        setSelectedKey((current) => (
          current && nextRows.some((row) => row.key === current)
            ? current
            : nextRows[0]?.key ?? null
        ));
        setLoading(false);
      } catch (reason: unknown) {
        if (cancelled || currentRequest !== requestId) return;
        setRows([]);
        setSelectedKey(null);
        setMobilePreview(false);
        setError(readableError(reason, "Не удалось загрузить закладки."));
        setLoading(false);
      }
    }

    void loadBookmarks();
    return () => {
      cancelled = true;
    };
  }, [requestId]);

  useEffect(() => {
    const refresh = () => setRequestId((value) => value + 1);
    window.addEventListener(BOOKMARKS_CHANGED, refresh);
    return () => window.removeEventListener(BOOKMARKS_CHANGED, refresh);
  }, []);

  async function removeBookmark(row: BookmarkRow) {
    if (removingKey) return;
    const wasSelected = selectedKey === row.key;
    setRemovingKey(row.key);
    setActionError(null);
    try {
      await setBookmark(row.bookmark.task_id, row.bookmark.task_revision_id, false);
      setRows((current) => {
        const next = current.filter((item) => item.key !== row.key);
        if (wasSelected) setSelectedKey(next[0]?.key ?? null);
        return next;
      });
      if (wasSelected) setMobilePreview(false);
    } catch (reason: unknown) {
      setActionError(readableError(reason, "Не удалось убрать задание из закладок."));
    } finally {
      setRemovingKey(null);
    }
  }

  function selectRow(row: BookmarkRow) {
    setSelectedKey(row.key);
    setMobilePreview(true);
  }

  const selectedRow = rows.find((row) => row.key === selectedKey) ?? null;

  return (
    <main className={styles.page}>
      <header className={styles.heading}>
        <div>
                    <h1>Закладки</h1>
          <p className={styles.lead}>Задания, к которым вы хотите вернуться.</p>
        </div>
        {!loading && !error ? (
          <span className={styles.count}>{rows.length} {pluralize(rows.length, "задание", "задания", "заданий")}</span>
        ) : null}
      </header>

      {error ? (
        <section className={styles.state} role="alert">
          <div className={styles.stateIcon}><Icon name="warning" size={22} /></div>
          <h2>Закладки недоступны</h2>
          <p>{error}</p>
          <button className={styles.primaryButton} onClick={() => setRequestId((value) => value + 1)} type="button">Повторить</button>
        </section>
      ) : null}

      {actionError ? (
        <div className={styles.actionError} role="alert">
          <span>{actionError}</span>
          <button onClick={() => setRequestId((value) => value + 1)} type="button">Повторить</button>
        </div>
      ) : null}
      {loading ? (
        <div className={styles.loading} role="status">
          <span className={styles.spinner} />
          Загружаем закладки…
        </div>
      ) : null}

      {!loading && !error && !rows.length ? (
        <section className={styles.empty}>
          <Icon name="bookmark" size={24} />
          <h2>Пока нет сохранённых заданий</h2>
          <p>Добавляйте задания в каталоге, чтобы быстро открыть их снова.</p>
          <Link className={styles.secondaryButton} href="/tasks">Открыть банк заданий</Link>
        </section>
      ) : null}

      {!loading && !error && rows.length ? (
        <div className={styles.layout} data-mobile-preview={mobilePreview ? "true" : "false"}>
          <aside className={styles.listPane} aria-label="Список закладок">
            <div className={styles.listHeader}>
              <div>
                <strong>Сохранённые задания</strong>
                <span>Порядок сохранения</span>
              </div>
              <span className={styles.listCount}>{rows.length}</span>
            </div>
            {progressError ? <p className={styles.progressNotice} role="status">{progressError}</p> : null}
            <div className={styles.rows} role="list">
              {rows.map((row) => (
                <BookmarkRow
                  key={row.key}
                  row={row}
                  selected={row.key === selectedKey}
                  busy={removingKey === row.key}
                  onSelect={() => selectRow(row)}
                  onRemove={() => void removeBookmark(row)}
                />
              ))}
            </div>
          </aside>

          <section className={styles.previewPane} aria-label="Предпросмотр задания">
            <button className={styles.mobileBack} onClick={() => setMobilePreview(false)} type="button">
              <Icon name="chevron-left" size={16} />
              К списку
            </button>
            {selectedRow ? (
              <BookmarkPreview
                row={selectedRow}
                busy={removingKey === selectedRow.key}
                onRemove={() => void removeBookmark(selectedRow)}
              />
            ) : (
              <div className={styles.previewEmpty}>
                <Icon name="bookmark" size={24} />
                <p>Выберите задание в списке.</p>
              </div>
            )}
          </section>
        </div>
      ) : null}
    </main>
  );
}

function BookmarkRow({
  row,
  selected,
  busy,
  onSelect,
  onRemove,
}: {
  row: BookmarkRow;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onRemove: () => void;
}) {
  const task = row.task;
  const source = task ? sourceCopy(task) : "Сохранённая редакция недоступна.";
  const rowClassName = [styles.row, selected ? styles.rowSelected : "", row.error ? styles.rowUnavailable : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={rowClassName} role="listitem">
      <button
        aria-pressed={selected}
        className={styles.rowSelect}
        onClick={onSelect}
        type="button"
      >
        <span className={styles.rowBookmark} aria-hidden="true"><Icon name="bookmark" size={17} /></span>
        <span className={styles.rowCopy}>
          <strong>{task ? task.title : "Версия недоступна"}</strong>
          <span>{source}</span>
          {row.error ? <span className={styles.rowError}>{row.error}</span> : null}
        </span>
        {row.progress ? <ProgressBadge status={row.progress.status} /> : null}
        {!task ? <span className={styles.statusUnavailable}>Недоступно</span> : null}
      </button>
      <button
        aria-label="Убрать задание из закладок"
        className={styles.rowRemove}
        disabled={busy}
        onClick={onRemove}
        title="Убрать задание из закладок"
        type="button"
      >
        <Icon name="close" size={16} />
      </button>
    </div>
  );
}

function BookmarkPreview({
  row,
  busy,
  onRemove,
}: {
  row: BookmarkRow;
  busy: boolean;
  onRemove: () => void;
}) {
  const task = row.task;
  if (!task) {
    return (
      <div className={styles.previewUnavailable} role="alert">
        <div className={styles.stateIcon}><Icon name="warning" size={22} /></div>
        <p className={styles.previewKicker}>Закладка сохранена</p>
        <h2>Сохранённая версия недоступна</h2>
        <p>{row.error ?? "Сервер не вернул сохранённую редакцию задания."}</p>
        <button className={styles.primaryButton} disabled={busy} onClick={onRemove} type="button">
          Удалить закладку
        </button>
      </div>
    );
  }

  const statement = (task.revision.statement_text ?? statementToText(task.revision.statement_html)) || task.title;
  const progress = row.progress;
  const workspaceHref = progress?.solve_session_id
    ? "/workspace?session=" + encodeURIComponent(progress.solve_session_id)
    : "/workspace?revision=" + encodeURIComponent(task.revision.id);
  const actionLabel = progress?.solve_session_id ? "Продолжить" : "Решать";

  return (
    <article className={styles.previewCard}>
      <div className={styles.previewHeader}>
        <div>
          <p className={styles.previewKicker}>Тип {task.line_number}</p>
          <h2>{task.title}</h2>
        </div>
        <div className={styles.previewHeaderActions}>
          {progress ? <ProgressBadge status={progress.status} /> : <span className={styles.statusUnavailable}>Статус недоступен</span>}
          <button
            aria-label="Убрать задание из закладок"
            className={styles.previewRemove}
            disabled={busy}
            onClick={onRemove}
            title="Убрать задание из закладок"
            type="button"
          >
            <Icon name="bookmark" size={18} />
          </button>
        </div>
      </div>

      <div className={styles.previewMeta}>
        <span>{task.demo || task.revision.source_name === "demo" ? "Демонстрационное задание" : "КЕГЭ"}</span>
        <span>{sourceCopy(task)}</span>
        {task.assets.length ? <span>{task.assets.length} {pluralize(task.assets.length, "файл", "файла", "файлов")}</span> : null}
      </div>

      <p className={styles.previewStatement}>{statement}</p>
      <TaskImages assets={task.assets} canPrepare={false}/>

      <div className={styles.previewFooter}>
        <Link className={styles.solveButton} href={workspaceHref}>
          {actionLabel}
          <Icon name="chevron-right" size={15} />
        </Link>
        <span className={styles.bookmarkedLabel}><Icon name="bookmark" size={15} /> В закладках</span>
      </div>
    </article>
  );
}

function ProgressBadge({ status }: { status: LearningStatus }) {
  const className = status === "correct"
    ? styles.statusCorrect
    : status === "incorrect"
      ? styles.statusIncorrect
      : status === "started"
        ? styles.statusStarted
        : styles.statusNeutral;
  return (
    <span className={styles.progressBadge + " " + className}>
      <Icon name={status === "correct" ? "check" : status === "incorrect" ? "warning" : "circle"} size={13} />
      {statusLabel(status)}
    </span>
  );
}

function statusLabel(value: LearningStatus): string {
  return value === "started" ? "Начато" : value === "incorrect" ? "Неверно" : value === "correct" ? "Решено" : "Не начато";
}

function sourceCopy(task: TaskRecord): string {
  const sourceYear = task.source_year ?? task.revision.source_year;
  if (sourceYear == null) return "Тип " + task.line_number;
  return formatSourceLabel(task.revision.source_label, sourceYear);
}

function formatSourceLabel(label: string | null | undefined, year: number): string {
  if (!label) return "год " + year;
  return new RegExp("(^|\D)" + year + "($|\D)").test(label) ? label : label + " · " + year;
}

function pluralize(value: number, one: string, few: string, many: string): string {
  const mod10 = value % 10;
  const mod100 = value % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}