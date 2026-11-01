"use client";

import Link from "next/link";
import {rememberCatalogSequence} from "@/lib/catalog-sequence";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import {TaskImages} from "@/components/task-images";
import {createPortal} from "react-dom";
import {useKonturShell} from "./kontur-shell";
import {getPreferences} from "@/lib/kontur-preferences";
import styles from "./task-bank.module.css";
import { DEMO_MODE, getTaskCatalog, getTaskCatalogCoverage } from "@/lib/api";
import { getTaskProgress, setBookmark } from "@/lib/kontur-api";
import { statementToText } from "@/lib/sanitize";
import { readableError } from "@/lib/ui-copy";
import type {
  TaskCatalogCoverage,
  TaskCatalogKind,
  TaskCatalogQuery,
  TaskDifficulty,
  TaskRecord,
} from "@/lib/types";
import type { LearningStatus, TaskProgress } from "@/lib/kontur-types";

import {OWNER_SYNC_CHANGED,useOwnerRecord} from "@/lib/owner-sync";
import {SyncConflict} from "./sync-conflict";

const pageSize = 50;
const lineOptions = Array.from({ length: 27 }, (_, index) => index + 1);
const coverageLineOptions = ["Все линии", ...lineOptions.map(String)];
const yearOptions = ["Все годы", "2027", "2026", "2025", "2024"] as const;
const difficultyOptions = ["Все сложности", "easy", "medium", "hard"] as const;
const fileOptions = ["Все файлы", "С файлами", "Без файлов"] as const;
const statusOptions: Array<{ value: "" | LearningStatus; label: string }> = [
  { value: "", label: "Любой статус" },
  { value: "neutral", label: "Не начато" },
  { value: "started", label: "Начато" },
  { value: "incorrect", label: "Неверно" },
  { value: "correct", label: "Решено" },
];
const sourceCatalog: TaskCatalogKind = "all";
const FILTER_STORAGE_KEY = "kontur:ui:task-bank:filters:v1";
const RETURN_STORAGE_KEY = "kontur:ui:task-bank:return:v1";

type DifficultyFilter = (typeof difficultyOptions)[number];
type FileFilter = (typeof fileOptions)[number];
type StatusFilter = "" | LearningStatus;
type CoverageLineFilter = "Все линии" | `${number}`;

interface StoredFilters {
  line?: number;
  query?: string;
  year?: string;
  difficulty?: DifficultyFilter;
  files?: FileFilter;
  learningStatus?: StatusFilter;
  bookmarksOnly?: boolean;
}

interface ReturnState {
  filterKey: string;
  revisionId: string;
  scrollTop: number;
}

export function TaskBankView() {
  const shell=useKonturShell();
  const searchParams = useSearchParams();
  const projectId = searchParams?.get("project") ?? null;
  const [initialFilters] = useState<StoredFilters>(() => readStoredFilters());
  const [line, setLine] = useState(initialFilters.line ?? getPreferences().defaultTaskType);
  const [query, setQuery] = useState(initialFilters.query ?? "");
  const filtersSync=useOwnerRecord({kind:"catalog_filters"},{unsolvedOnly:false,taskType:initialFilters.line??getPreferences().defaultTaskType});
  const [unsolvedOnly,setUnsolvedOnly]=useState(false);
  useEffect(()=>{if(!filtersSync.state.ready)return;
    if(typeof filtersSync.state.value.unsolvedOnly==="boolean")setUnsolvedOnly(filtersSync.state.value.unsolvedOnly);
    if(typeof filtersSync.state.value.taskType==="number")setLine(filtersSync.state.value.taskType);
  },[filtersSync.state.ready,filtersSync.state.value]);

  const [year, setYear] = useState(initialFilters.year ?? "Все годы");
  const [difficulty, setDifficulty] = useState<DifficultyFilter>(initialFilters.difficulty ?? "Все сложности");
  const [files, setFiles] = useState<FileFilter>(initialFilters.files ?? "Все файлы");
  const [learningStatus, setLearningStatus] = useState<StatusFilter>(initialFilters.learningStatus ?? "");
  const [bookmarksOnly, setBookmarksOnly] = useState(initialFilters.bookmarksOnly ?? false);
  const [debouncedQuery, setDebouncedQuery] = useState(initialFilters.query ?? "");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [tasks, setTasks] = useState<TaskRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [progressByRevision, setProgressByRevision] = useState<Map<string, TaskProgress>>(new Map());
  const [bookmarkByRevision, setBookmarkByRevision] = useState<Map<string, boolean>>(new Map());
  const [progressError, setProgressError] = useState<string | null>(null);
  const [busyBookmarks, setBusyBookmarks] = useState<Set<string>>(new Set());
  const [bookmarkError, setBookmarkError] = useState<string | null>(null);
  const catalogRequestRef = useRef(0);
  const progressRequestRef = useRef(0);
  const restoredReturnRef = useRef(false);
  const [coverage, setCoverage] = useState<TaskCatalogCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(!DEMO_MODE);
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const coverageRequestRef = useRef(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const yearFilterRef = useRef<HTMLSelectElement>(null);
  const difficultyFilterRef = useRef<HTMLSelectElement>(null);
  const filesFilterRef = useRef<HTMLSelectElement>(null);
  const statusFilterRef = useRef<HTMLSelectElement>(null);
  const bookmarksFilterRef = useRef<HTMLInputElement>(null);

  function focusFilter(filter: "search" | "year" | "difficulty" | "files" | "status" | "bookmarks") {
    setFiltersOpen(true);
    window.setTimeout(() => {
      const target = filter === "search"
        ? searchInputRef.current
        : filter === "year"
          ? yearFilterRef.current
          : filter === "difficulty"
            ? difficultyFilterRef.current
            : filter === "files"
              ? filesFilterRef.current
              : filter === "status"
                ? statusFilterRef.current
                : bookmarksFilterRef.current;
      target?.focus();
    }, 0);
  }

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 280);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    persistStoredFilters({ line, query, year, difficulty, files, learningStatus, bookmarksOnly });
  }, [bookmarksOnly, difficulty, files, learningStatus, line, query, year]);

  useEffect(() => {
    let cancelled = false;
    const requestId = ++catalogRequestRef.current;
    setLoading(true);
    setLoadingMore(false);
    setError(null);
    setBookmarkError(null);
    setProgressError(null);
    setTasks([]);
    setProgressByRevision(new Map());
    setBookmarkByRevision(new Map());
    setNextCursor(null);
    setTotal(null);
    getTaskCatalog(catalogQuery({ year, line, query: debouncedQuery, files, difficulty, learningStatus, bookmarksOnly }))
      .then((page) => {
        if (!cancelled && requestId === catalogRequestRef.current) {
          setTasks(page.items);
          setNextCursor(page.next_cursor);
          setTotal(page.total);
          setLoading(false);
          void loadProgress(page.items, requestId);
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled && requestId === catalogRequestRef.current) {
          setError(readableError(reason, "Не удалось загрузить задания. Обновите страницу и попробуйте ещё раз."));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bookmarksOnly, debouncedQuery, difficulty, files, learningStatus, line, year]);

  useEffect(() => {
    if (DEMO_MODE) return;
    let cancelled = false;
    const requestId = ++coverageRequestRef.current;
    setCoverageLoading(true);
    setCoverageError(null);
    getTaskCatalogCoverage()
      .then((nextCoverage) => {
        if (!cancelled && requestId === coverageRequestRef.current) {
          setCoverage(nextCoverage);
          setCoverageLoading(false);
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled && requestId === coverageRequestRef.current) {
          setCoverageError(readableError(reason, "Покрытие опубликованной подборки пока недоступно."));
          setCoverageLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (loading || !tasks.length || restoredReturnRef.current) return;
    restoredReturnRef.current = true;
    const saved = readReturnState();
    if (!saved || saved.filterKey !== filterKey({ line, query, year, difficulty, files, learningStatus, bookmarksOnly })) return;
    const scroller = document.querySelector<HTMLElement>(".app-main");
    window.setTimeout(() => {
      const anchor = [...document.querySelectorAll<HTMLElement>("[data-revision-id]")]
        .find((element) => element.dataset.revisionId === saved.revisionId);
      if (anchor) anchor.scrollIntoView({ block: "center" });
      else if (scroller) scroller.scrollTop = saved.scrollTop;
    }, 0);
    forgetReturnState();
  }, [bookmarksOnly, difficulty, files, learningStatus, line, loading, query, tasks, year]);

  useEffect(()=>{
    const refresh=(event:Event)=>{
      const detail=(event as CustomEvent<{topics:string[];reset:boolean}>).detail;
      if(!detail.reset&&!detail.topics.some(topic=>["progress","bookmarks","solve_sessions"].includes(topic)))return;
      if(detail.topics.includes("bookmarks")||detail.reset)setBookmarkByRevision(new Map());
      const requestId=catalogRequestRef.current;
      void (async()=>{for(let offset=0;offset<tasks.length;offset+=100)await loadProgress(tasks.slice(offset,offset+100),requestId);})();
    };
    window.addEventListener(OWNER_SYNC_CHANGED,refresh);
    return()=>window.removeEventListener(OWNER_SYNC_CHANGED,refresh);
  },[tasks]);

  async function loadProgress(items: TaskRecord[], catalogRequestId: number) {
    const requestId = ++progressRequestRef.current;
    if (!items.length) return;
    try {
      const progress = await getTaskProgress(items.map((task) => task.revision.id));
      if (requestId !== progressRequestRef.current || catalogRequestId !== catalogRequestRef.current) return;
      setProgressByRevision((current) => {
        const next = new Map(current);
        for (const item of progress) next.set(item.task_revision_id, item);
        return next;
      });
      setProgressError(null);
    } catch (reason: unknown) {
      if (requestId === progressRequestRef.current && catalogRequestId === catalogRequestRef.current) {
        setProgressError(readableError(reason, "Статусы обучения пока недоступны."));
      }
    }
  }

  async function loadMore() {
    if (!nextCursor || loadingMore || loading) return;
    const cursor = nextCursor;
    setLoadingMore(true);
    setError(null);
    const requestId = ++catalogRequestRef.current;
    try {
      const page = await getTaskCatalog({
        ...catalogQuery({ year, line, query: debouncedQuery, files, difficulty, learningStatus, bookmarksOnly }),
        cursor,
      });
      if (requestId !== catalogRequestRef.current) return;
      setTasks((current) => mergeTasks(current, page.items));
      setNextCursor(page.next_cursor);
      setTotal(page.total);
      void loadProgress(page.items, requestId);
    } catch (reason: unknown) {
      if (requestId === catalogRequestRef.current) setError(readableError(reason, "Следующая страница каталога недоступна."));
    } finally {
      if (requestId === catalogRequestRef.current) setLoadingMore(false);
    }
  }

  function retryCatalog() {
    const requestId = ++catalogRequestRef.current;
    setError(null);
    setLoading(true);
    setLoadingMore(false);
    void getTaskCatalog(catalogQuery({ year, line, query: debouncedQuery, files, difficulty, learningStatus, bookmarksOnly }))
      .then((page) => {
        if (requestId !== catalogRequestRef.current) return;
        setTasks(page.items);
        setProgressByRevision(new Map());
        setBookmarkByRevision(new Map());
        setNextCursor(page.next_cursor);
        setTotal(page.total);
        setLoading(false);
        void loadProgress(page.items, requestId);
      })
      .catch((reason: unknown) => {
        if (requestId !== catalogRequestRef.current) return;
        setError(readableError(reason, "Банк заданий недоступен."));
        setLoading(false);
      });
  }

  function retryCoverage() {
    if (DEMO_MODE) return;
    const requestId = ++coverageRequestRef.current;
    setCoverageError(null);
    setCoverageLoading(true);
    void getTaskCatalogCoverage()
      .then((nextCoverage) => {
        if (requestId !== coverageRequestRef.current) return;
        setCoverage(nextCoverage);
        setCoverageLoading(false);
      })
      .catch((reason: unknown) => {
        if (requestId !== coverageRequestRef.current) return;
        setCoverageError(readableError(reason, "Покрытие опубликованной подборки пока недоступно."));
        setCoverageLoading(false);
      });
  }

  function resetFilters() {
    setUnsolvedOnly(false);filtersSync.edit({unsolvedOnly:false,taskType:line});
    const alreadyDefault = query === ""
      && year === "Все годы"
      && difficulty === "Все сложности"
      && files === "Все файлы"
      && learningStatus === ""
      && !bookmarksOnly;
    if (alreadyDefault) {
      retryCatalog();
      return;
    }
    catalogRequestRef.current += 1;
    setQuery("");
    setDebouncedQuery("");
    setYear("Все годы");
    setDifficulty("Все сложности");
    setFiles("Все файлы");
    setLearningStatus("");
    setBookmarksOnly(false);
    setTasks([]);
    setNextCursor(null);
    setTotal(null);
    setLoadingMore(false);
    setLoading(true);
    setError(null);
  }

  async function toggleBookmark(task: TaskRecord, selected: boolean) {
    setBookmarkError(null);
    setBusyBookmarks((current) => new Set(current).add(task.id));
    try {
      await setBookmark(task.id, task.revision.id, selected);
      if (!selected && bookmarksOnly) {
        retryCatalog();
      } else {
        setBookmarkByRevision((current) => {
          const next = new Map(current);
          next.set(task.revision.id, selected);
          return next;
        });
      }
    } catch (reason: unknown) {
      setBookmarkError(readableError(reason, "Не удалось изменить закладку."));
    } finally {
      setBusyBookmarks((current) => {
        const next = new Set(current);
        next.delete(task.id);
        return next;
      });
    }
  }

  function rememberReturn(task: TaskRecord) {
    const scroller = document.querySelector<HTMLElement>(".app-main");
    const filters = { line, query, year, difficulty, files, learningStatus, bookmarksOnly };
    try {
      persistStoredFilters(filters);
      rememberCatalogSequence({revisionIds:tasks.map(item=>item.revision.id),total,nextCursor,query:catalogQuery({...filters,cursor:null})});
      localStorage.setItem(RETURN_STORAGE_KEY, JSON.stringify({
        filterKey: filterKey(filters),
        revisionId: task.revision.id,
        scrollTop: scroller?.scrollTop ?? window.scrollY,
      } satisfies ReturnState));
    } catch {
      // Storage is optional; navigation remains usable without the return checkpoint.
    }
  }

  if (error && !DEMO_MODE && !tasks.length && !loading) {
    return <BankState title="Банк заданий недоступен" message={error} onRetry={retryCatalog} />;
  }

  return (
    <main className={styles.catalog}>
      <SyncConflict state={filtersSync.state} resolve={filtersSync.resolve}/>
      <header className={styles.catalogHeading}>
        <div>
          <h1>Задания типа {line}</h1>
          <p className={styles.lead}>
            {DEMO_MODE ? "Демонстрационный набор · исходный порядок заданий" : "Задания КЕГЭ в исходном порядке по номеру."}
          </p>
        </div>
        {total!=null&&<span className={styles.headingCount}>{total} {pluralize(total,"задание","задания","заданий")}</span>}
        {shell.toolbarHost?createPortal(<div className={styles.headingControls}>
          <label className={styles.typePicker}>
            <span className="sr-only">Тип задания</span>
            <span>Тип {line}</span>
            <select aria-label="Тип задания" onChange={(event) => {const taskType=Number(event.target.value);setLine(taskType);filtersSync.edit({taskType,unsolvedOnly});}} value={line}>
              {lineOptions.map((option) => <option key={option} value={option}>Тип {option}</option>)}
            </select>
            <Icon name="chevron-down" size={16} />
          </label>
          <button
            aria-expanded={filtersOpen}
            aria-controls="task-bank-filters"
            className={styles.filtersToggle}
            onClick={() => setFiltersOpen((value) => !value)}
            type="button"
          >
            <Icon name="filter" size={17} />
            Фильтры
          </button>
        </div>,shell.toolbarHost):null}
      </header>

      {!DEMO_MODE ? <CoveragePanel coverage={coverage} error={coverageError} loading={coverageLoading} onRetry={retryCoverage} /> : null}

      {filtersOpen ? (
        <section className={styles.filtersPanel} aria-label="Фильтры банка заданий" id="task-bank-filters">
          <label className={styles.searchField}>
            <Icon name="search" size={17} />
            <span className="sr-only">Поиск по условию</span>
            <input
              ref={searchInputRef}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Поиск по условию"
              type="search"
              value={query}
            />
          </label>
          <label className={styles.filterControl}>
            <span className="sr-only">Год источника</span>
            <select aria-label="Год источника" onChange={(event) => setYear(event.target.value)} ref={yearFilterRef} value={year}>
              {yearOptions.map((option) => <option key={option} value={option}>{option === "2024" ? "2024 · резерв" : option}</option>)}
            </select>
            <Icon name="chevron-down" size={14} />
          </label>
          <label className={styles.filterControl}>
            <span className="sr-only">Сложность задания</span>
            <select aria-label="Сложность задания" onChange={(event) => setDifficulty(event.target.value as DifficultyFilter)} ref={difficultyFilterRef} value={difficulty}>
              {difficultyOptions.map((option) => <option key={option} value={option}>{difficultyLabel(option)}</option>)}
            </select>
            <Icon name="chevron-down" size={14} />
          </label>
          <label className={styles.filterControl}>
            <span className="sr-only">Наличие файлов</span>
            <select aria-label="Наличие файлов" onChange={(event) => setFiles(event.target.value as FileFilter)} ref={filesFilterRef} value={files}>
              {fileOptions.map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
            <Icon name="chevron-down" size={14} />
          </label>
          <label className={styles.filterControl}>
            <span className="sr-only">Статус обучения</span>
            <select aria-label="Статус обучения" onChange={(event) => setLearningStatus(event.target.value as StatusFilter)} ref={statusFilterRef} value={learningStatus}>
              {statusOptions.map((option) => <option key={option.value || "all"} value={option.value}>{option.label}</option>)}
            </select>
            <Icon name="chevron-down" size={14} />
          </label>
          <label className={styles.bookmarksFilter}>
            <input checked={bookmarksOnly} onChange={(event) => setBookmarksOnly(event.target.checked)} ref={bookmarksFilterRef} type="checkbox" />
            <span>Только закладки</span>
          </label>
          <label className={styles.bookmarksFilter}>
            <input type="checkbox" checked={unsolvedOnly} onChange={event=>{setUnsolvedOnly(event.target.checked);filtersSync.edit({taskType:line,unsolvedOnly:event.target.checked});}}/>
            <span>Только нерешённые</span>
          </label>
          <button className={styles.filterReset} onClick={resetFilters} type="button">Сбросить</button>
          <span className={styles.filterCount} aria-live="polite">
            {loading ? "Загрузка…" : `Загружено ${tasks.length}${total == null ? "" : ` из ${total}`} ${pluralize(tasks.length, "задание", "задания", "заданий")}`}
          </span>
        </section>
      ) : (
        <div className={styles.collapsedFilters} aria-label="Быстрые фильтры">
          <button className={styles.filterChip} onClick={() => focusFilter("difficulty")} type="button">
            {difficultyLabel(difficulty)}
          </button>
          <button className={styles.filterChip} onClick={() => focusFilter("status")} type="button">
            {learningStatus ? statusLabel(learningStatus) : "Любой статус"}
          </button>
          <button aria-pressed={bookmarksOnly} className={styles.filterChip} onClick={() => focusFilter("bookmarks")} type="button">
            {bookmarksOnly ? "Только закладки" : "Все задания"}
          </button>
        </div>
      )}

      {progressError && tasks.length ? <p className={styles.statusNotice} role="status">{progressError}</p> : null}
      {bookmarkError ? <div className={styles.inlineError} role="alert"><Icon name="warning" size={15} /><span>{bookmarkError}</span></div> : null}

      <section className={styles.taskList} aria-label={`Задания типа ${line}`}>
        {loading ? <div className={styles.loadingRow} role="status"><span className={styles.buttonSpinner} /> Загружаем каталог…</div> : null}
        {!loading && !tasks.length ? (
          <div className={styles.emptyBlock}>
            <Icon name="search" size={21} />
            <h2>Ничего не найдено</h2>
            <p>Измените фильтры каталога и попробуйте ещё раз.</p>
          </div>
        ) : null}
        {error && tasks.length ? <div className={styles.inlineError} role="alert"><Icon name="warning" size={15} /><span>{error}</span><button className={styles.tinyAction} onClick={retryCatalog} type="button">Повторить</button></div> : null}
        {tasks.map((task, index) => unsolvedOnly&&progressByRevision.get(task.revision.id)?.status==="correct"?null:(
          <TaskCard
            key={`${task.id}:${task.revision.id}`}
            bookmarked={bookmarkByRevision.has(task.revision.id) ? bookmarkByRevision.get(task.revision.id) === true : progressByRevision.get(task.revision.id)?.bookmarked === true}
            busy={busyBookmarks.has(task.id)}
            onBookmarkToggle={(selected) => void toggleBookmark(task, selected)}
            onSolve={() => rememberReturn(task)}
            position={index + 1}
            progress={progressByRevision.get(task.revision.id)}
            projectId={projectId}
            task={task}
            total={total}
          />
        ))}
        {nextCursor ? (
          <div className={styles.catalogMore}>
            <button className={styles.secondaryButton} disabled={loadingMore} onClick={() => void loadMore()} type="button">
              {loadingMore ? <><span className={styles.buttonSpinner} />Загружаем…</> : "Показать ещё задания"}
            </button>
            <span className={styles.mutedCopy}>Текущий список: {tasks.length}. Порядок сервера сохранён.</span>
          </div>
        ) : null}
        {!loading && tasks.length > 0 && !nextCursor ? <p className={styles.catalogEnd}>Все доступные задания по текущим фильтрам загружены.</p> : null}
      </section>
    </main>
  );
}

function catalogQuery({
  year,
  line,
  query,
  files,
  difficulty,
  learningStatus,
  bookmarksOnly,
  cursor,
}: {
  year: string;
  line: number;
  query: string;
  files: FileFilter;
  difficulty: DifficultyFilter;
  learningStatus: StatusFilter;
  bookmarksOnly: boolean;
  cursor?: string | null;
}): TaskCatalogQuery {
  return {
    sourceYear: year === "Все годы" ? undefined : Number(year),
    difficulty: difficulty === "Все сложности" ? undefined : difficulty,
    sort: "number",
    catalog: sourceCatalog,
    line,
    query: query || undefined,
    hasFiles: files === "Все файлы" ? undefined : files === "С файлами",
    learningStatus: learningStatus || undefined,
    bookmarksOnly: bookmarksOnly || undefined,
    limit: pageSize,
    cursor,
  };
}

function mergeTasks(current: TaskRecord[], incoming: TaskRecord[]): TaskRecord[] {
  const seen = new Set(current.map((task) => `${task.id}:${task.revision.id}`));
  return [...current, ...incoming.filter((task) => {
    const key = `${task.id}:${task.revision.id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  })];
}

function TaskCard({
  task,
  position,
  total,
  projectId,
  progress,
  bookmarked,
  busy,
  onBookmarkToggle,
  onSolve,
}: {
  task: TaskRecord;
  position: number;
  total: number | null;
  projectId: string | null;
  progress?: TaskProgress;
  bookmarked: boolean;
  busy: boolean;
  onBookmarkToggle: (selected: boolean) => void;
  onSolve: () => void;
}) {
  const isDemo = task.demo || task.revision.source_name === "demo";
  const sourceId = task.external_id ?? task.revision.source_id;
  const sourceYear = task.source_year ?? task.revision.source_year;
  const sourceLabel = task.revision.source_label;
  const sourceCopy = sourceYear != null
    ? formatSourceLabel(sourceLabel, sourceYear)
    : task.exam_year != null
      ? `исторический год ${task.exam_year}`
      : "источник без года";
  const statement = (task.revision.statement_text ?? statementToText(task.revision.statement_html)) || task.title;
  const bookmarkLabel = bookmarked ? "Убрать из закладок" : "Добавить в закладки";
  const sequence = `${task.line_number}:${String(position).padStart(3, "0")}`;
  const canResume=Boolean(progress?.solve_session_id)&&getPreferences().restoreWorkspace;
  const workspaceHref = `/workspace?${canResume ? `session=${encodeURIComponent(progress!.solve_session_id!)}` : `revision=${encodeURIComponent(task.revision.id)}`}&sequence=${encodeURIComponent(sequence)}${projectId ? `&project=${encodeURIComponent(projectId)}` : ""}`;

  return (
    <article
      className={styles.taskCard}
      data-revision-id={task.revision.id}
      data-sequence-position={position}
      data-source-id={sourceId ?? undefined}
      data-test-only={isDemo}
    >
      <div className={styles.cardHeader}>
        <div className={styles.cardMeta}>
          <strong>{task.title}</strong>
          <span>Тип {task.line_number}</span>
          {sourceId ? <span>КЕГЭ № {sourceId}</span> : null}
          <span>Позиция: {position}{total != null ? ` из ${total}` : ""}</span>
          {sourceYear!=null&&<span>{sourceCopy}</span>}
        </div>
        {progress ? <ProgressBadge status={progress.status} /> : null}
      </div>
      <p className={styles.statement}>{statement}</p>
      <TaskImages assets={task.assets} canPrepare={!isDemo} revisionHash={task.revision.content_hash} />
      <div className={styles.cardFooter}>
        <Link className={styles.solveButton} href={workspaceHref} onClick={onSolve}>
          {canResume?"Продолжить":"Решать"}
        </Link>
        {canResume&&<Link className={styles.secondaryButton} href={`/workspace?revision=${encodeURIComponent(task.revision.id)}&fresh=new${projectId?`&project=${encodeURIComponent(projectId)}`:""}`} onClick={onSolve}>Начать заново</Link>}

        <div className={styles.cardFooterMeta}>
          {task.assets.length ? <span>{task.assets.length} {pluralize(task.assets.length, "файл", "файла", "файлов")}</span> : null}
          <button
            aria-label={bookmarkLabel}
            aria-pressed={bookmarked}
            className={styles.bookmarkButton + (bookmarked ? ` ${styles.bookmarkButtonActive}` : "")}
            disabled={busy}
            onClick={() => onBookmarkToggle(!bookmarked)}
            title={bookmarkLabel}
            type="button"
          >
            <Icon name="bookmark" size={18} />
          </button>
        </div>
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
    <span className={`${styles.progressBadge} ${className}`}>
      <Icon name={status === "correct" ? "check" : status === "incorrect" ? "warning" : "circle"} size={14} />
      {statusLabel(status)}
    </span>
  );
}

function CoveragePanel({
  coverage,
  error,
  loading,
  onRetry,
}: {
  coverage: TaskCatalogCoverage | null;
  error: string | null;
  loading: boolean;
  onRetry: () => void;
}) {
  const [line, setLine] = useState<CoverageLineFilter>("Все линии");
  const [difficulty, setDifficulty] = useState<DifficultyFilter>("Все сложности");
  const visibleCells = coverage?.cells.filter((cell) =>
    (line === "Все линии" || String(cell.line_number) === line)
    && (difficulty === "Все сложности" || cell.difficulty === difficulty),
  ) ?? [];

  return (
    <section className={styles.coveragePanel} aria-labelledby="catalog-coverage-title">
      <details>
        <summary className={styles.coverageSummary}>
          <span className={styles.coverageSummaryCopy}>
            <strong id="catalog-coverage-title">Покрытие опубликованной подборки</strong>
            <small>{coverage?.published ? `${coverage.total_selected} заданий · ${coverage.quota_met ? "цель набора выполнена" : "есть недобор"}` : "Сводка по доступным ячейкам"}</small>
          </span>
          <Icon name="chevron-down" size={16} />
        </summary>
        <div className={styles.coverageBody}>
          {loading && !coverage ? <p className={styles.coverageHint} role="status">Загружаем сводку покрытия…</p> : null}
          {error ? <div className={styles.coverageFeedback} role="alert"><Icon name="warning" size={14} /><span>{error}</span><button className={styles.tinyAction} onClick={onRetry} type="button">Повторить</button></div> : null}
          {!loading && !error && coverage && !coverage.published ? <p className={styles.coverageHint}>Опубликованной подборки пока нет. После публикации здесь появятся доступные ячейки и честные недоборы.</p> : null}
          {!loading && !error && coverage?.published ? (
            <>
              <p className={styles.coverageHint}>
                Цель: {coverage.target_per_cell ?? "—"} заданий на номер и уровень. Основные годы: {coverage.primary_years.length ? coverage.primary_years.join(", ") : "не указаны"}; резерв: {coverage.fallback_year ?? "не указан"}.
              </p>
              <div className={styles.coverageFilters} aria-label="Фильтры покрытия">
                <label className={styles.filterControl}>
                  <span className="sr-only">Номер в покрытии</span>
                  <select aria-label="Номер в покрытии" onChange={(event) => setLine(event.target.value as CoverageLineFilter)} value={line}>
                    {coverageLineOptions.map((option) => <option key={option} value={option}>{option === "Все линии" ? "Все номера" : `№ ${option}`}</option>)}
                  </select>
                  <Icon name="chevron-down" size={14} />
                </label>
                <label className={styles.filterControl}>
                  <span className="sr-only">Уровень в покрытии</span>
                  <select aria-label="Уровень в покрытии" onChange={(event) => setDifficulty(event.target.value as DifficultyFilter)} value={difficulty}>
                    {difficultyOptions.map((option) => <option key={option} value={option}>{difficultyLabel(option)}</option>)}
                  </select>
                  <Icon name="chevron-down" size={14} />
                </label>
                <span className={styles.coverageFilterCount}>{visibleCells.length} из {coverage.cells.length} ячеек</span>
              </div>
              {visibleCells.length ? (
                <div className={styles.coverageGrid} role="list" aria-label="Ячейки покрытия">
                  {visibleCells.map((cell) => <CoverageCell cell={cell} fallbackYear={coverage.fallback_year} target={coverage.target_per_cell} key={`${cell.line_number}-${cell.difficulty}`} />)}
                </div>
              ) : <p className={styles.coverageHint}>Для выбранных фильтров ячеек нет.</p>}
            </>
          ) : null}
        </div>
      </details>
    </section>
  );
}

function CoverageCell({
  cell,
  fallbackYear,
  target,
}: {
  cell: TaskCatalogCoverage["cells"][number];
  fallbackYear: number | null;
  target: number | null;
}) {
  return (
    <article className={styles.coverageCell} data-deficit={cell.shortfall > 0 ? "true" : "false"} role="listitem">
      <div className={styles.coverageCellHeading}>
        <strong>№ {cell.line_number} · {difficultyLabel(cell.difficulty)}</strong>
        <span>{cell.total}/{target ?? "—"}</span>
      </div>
      <div className={styles.coverageCellFacts}>
        <span>Основные годы: {cell.selected_primary}</span>
        {cell.selected_fallback > 0 ? <span>Резерв {fallbackYear ?? "года"}: {cell.selected_fallback}</span> : null}
      </div>
      <small>{cell.shortfall > 0 ? `Не хватает ${cell.shortfall}` : "Цель ячейки выполнена"}</small>
    </article>
  );
}

function difficultyLabel(value: DifficultyFilter | TaskDifficulty): string {
  return value === "Все сложности" ? "Любая сложность" : value === "hard" ? "Сложная" : value === "medium" ? "Средняя" : "Лёгкая";
}

function statusLabel(value: LearningStatus): string {
  return value === "started" ? "Начато" : value === "incorrect" ? "Неверно" : value === "correct" ? "Решено" : "Не начато";
}

function pluralize(value: number, one: string, few: string, many: string): string {
  const mod10 = value % 10;
  const mod100 = value % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

function formatSourceLabel(label: string | null | undefined, year: number): string {
  if (!label) return `год ${year}`;
  return new RegExp(`(?:^|\\D)${year}(?:$|\\D)`).test(label) ? label : `${label} · ${year}`;
}

function BankState({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) {
  return (
    <main className={styles.bankState}>
      <div className={styles.stateIcon}><Icon name="warning" size={22} /></div>
      <h1>{title}</h1>
      <p>{message}</p>
      {onRetry ? <button className={styles.primaryButton} onClick={onRetry} type="button">Повторить загрузку</button> : null}
    </main>
  );
}

function readStoredFilters(): StoredFilters {
  if (typeof window === "undefined") return {};
  try {
    const raw = JSON.parse(localStorage.getItem(FILTER_STORAGE_KEY) || "null") as Record<string, unknown> | null;
    if (!raw) return {};
    const line = typeof raw.line === "number" && lineOptions.includes(raw.line) ? raw.line : undefined;
    const query = typeof raw.query === "string" ? raw.query.slice(0, 500) : undefined;
    const year = typeof raw.year === "string" && yearOptions.includes(raw.year as (typeof yearOptions)[number]) ? raw.year : undefined;
    const difficulty = typeof raw.difficulty === "string" && difficultyOptions.includes(raw.difficulty as DifficultyFilter) ? raw.difficulty as DifficultyFilter : undefined;
    const files = typeof raw.files === "string" && fileOptions.includes(raw.files as FileFilter) ? raw.files as FileFilter : undefined;
    const learningStatus = typeof raw.learningStatus === "string" && statusOptions.some((option) => option.value === raw.learningStatus) ? raw.learningStatus as StatusFilter : undefined;
    return { line, query, year, difficulty, files, learningStatus, bookmarksOnly: raw.bookmarksOnly === true };
  } catch {
    return {};
  }
}

function persistStoredFilters(filters: StoredFilters) {
  if (typeof window === "undefined") return;
  try { localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(filters)); } catch { /* Storage is optional. */ }
}

function filterKey(filters: StoredFilters): string {
  return JSON.stringify({
    line: filters.line ?? 14,
    query: filters.query ?? "",
    year: filters.year ?? "Все годы",
    difficulty: filters.difficulty ?? "Все сложности",
    files: filters.files ?? "Все файлы",
    learningStatus: filters.learningStatus ?? "",
    bookmarksOnly: filters.bookmarksOnly === true,
  });
}

function readReturnState(): ReturnState | null {
  if (typeof window === "undefined") return null;
  try {
    const value = JSON.parse(localStorage.getItem(RETURN_STORAGE_KEY) || "null") as Partial<ReturnState> | null;
    if (!value || typeof value.filterKey !== "string" || typeof value.revisionId !== "string" || typeof value.scrollTop !== "number") return null;
    return value as ReturnState;
  } catch {
    return null;
  }
}

function forgetReturnState() {
  if (typeof window === "undefined") return;
  try { localStorage.removeItem(RETURN_STORAGE_KEY); } catch { /* Storage is optional. */ }
}
