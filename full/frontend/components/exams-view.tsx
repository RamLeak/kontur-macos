"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "@/components/icon";
import { Modal } from "@/components/modal";
import { CodeEditor } from "@/components/code-editor";
import { DeviceFileRow } from "@/components/device-file-row";
import { InfiniteBoard } from "@/components/infinite-board";
import { TaskSpreadsheetFiles, isSpreadsheetName } from "@/components/task-spreadsheet-files";
import { TaskImages } from "@/components/task-images";
import { flushOpenDrafts, registerDraftFlush, registerDraftSaveState, acceptDraftHandoff } from "@/lib/draft-lifecycle";
import { assessLocalDraft, clearLocalDraft, readLocalDraft, saveLocalDraft, isLocalDraftDurable } from "@/lib/local-drafts";
import { resolveExamCodeAck } from "@/lib/exam-draft-ack";
import { useKonturShell } from "@/components/kontur-shell";
import { useShortcuts } from "@/lib/kontur-shortcuts";
import {
  DEMO_MODE, getCapabilities, instantiateOfficialExamTemplate,
} from "@/lib/api";
import {
  getActiveExam, getExamBlueprints, getExamReport, getSavedExams, getSavedExam, getExamWorkspace,
  cancelExamStudyAi, clearExamStudyAiDraft, getExamStudyAi, listExamCodeRuns, markExamAssetsReady, pauseExam, prepareExam, readExamStudyAiDraft, resumeExam, saveExamAnswer, saveExamStudyAiDraft, saveExamWorkspaceBoard, saveExamWorkspaceCode, startExamStudyAi,
  startDifficultyExam, startExam, startPreparedExam, submitExam,
  type ExamCodeRun, type ExamMode, type ExamPrepareDifficulty, type ExamPrepareKind, type ExamPrepareInput, type ExamRecord, type ExamStudyAiRun, type ExamWorkspace,
} from "@/lib/exam-workspace-api";
import { createExamTimerAnchor, examTimerSnapshotKey, remainingExamSeconds, type ExamTimerSnapshot } from "@/lib/exam-timer";
import { ApiError } from "@/lib/types";
import { deviceCacheLimitBytes, loadDeviceAsset, loadDeviceTexts, MAX_FILE_BYTES, type DeviceFile } from "@/lib/device-files";
import { readDevicePythonRuns, useDevicePython } from "@/lib/use-device-python";
import {statementToText} from "@/lib/sanitize";
import {getPreferences} from "@/lib/kontur-preferences";
import {readExamUiState,saveExamUiState,readStudyCheckpoint,saveStudyCheckpoint,studyRequestKey,clearStudyRequestKey} from "@/lib/exam-ui-state";
import type { AssetRef, BoardDocument, CodeRun, ExamBlueprint, ExamDifficulty, ExamReport } from "@/lib/types";
import styles from "./exams-view.module.css";

const OFFICIAL_2027_TEMPLATE_ID = "fipi-informatics-2027-project-2026-08-21";
const DIFFICULTIES: Array<{ value: ExamDifficulty; label: string; description: string; accent: string }> = [
  { value: "basic", label: "Базовый уровень", description: "Типовые алгоритмы и уверенность в основных номерах.", accent: styles.basic },
  { value: "medium", label: "Средний уровень", description: "Закрепление материала и поиск тем для практики.", accent: styles.medium },
  { value: "hard", label: "Сложный уровень", description: "Задачи с несколькими шагами рассуждения.", accent: styles.hard },
];
const PREPARE_KINDS: Array<{ value: ExamPrepareKind; label: string; description: string }> = [
  { value: "full", label: "Полный", description: "Практический профиль из 27 позиций." },
  { value: "thematic", label: "Тематический", description: "Выберите линию и число заданий." },
  { value: "custom", label: "Пользовательский", description: "Соберите собственный набор линий." },
  { value: "errors", label: "По ошибкам", description: "Подтверждённые ошибки владельца." },
];

const MAX_PREPARED_ASSETS = 128;
const MAX_PREPARED_ASSET_BYTES = 80_000_000;
const PREPARED_ASSET_WORKERS = 4;
const PREPARED_ASSET_ATTEMPTS = 2;

type PreparedAsset = { asset: AssetRef; revisionHash: string; lineNumber: number | null; prepareTextFiles: boolean };

function selectedPreparedAssets(exam: ExamRecord): PreparedAsset[] {
  const selected: PreparedAsset[] = [];
  const seen = new Set<string>();
  for (const item of exam.items ?? []) {
    const revisionHash = item.task_revision_id;
    for (const asset of item.assets ?? []) {
      const prepareTextFiles = item.line_number === 9 && isSpreadsheetName(asset.name);
      const key = `${asset.id}\u001f${revisionHash}\u001f${prepareTextFiles ? "type9-tsv-v3" : "original"}`;
      if (seen.has(key)) continue;
      seen.add(key);
      selected.push({ asset, revisionHash, lineNumber: item.line_number, prepareTextFiles });
    }
  }
  return selected;
}

async function preloadPreparedExamAssets(exam: ExamRecord, signal: AbortSignal): Promise<void> {
  const selected = selectedPreparedAssets(exam);
  if (selected.length > MAX_PREPARED_ASSETS) {
    throw new Error("В выбранном варианте слишком много файлов для подготовки устройства. Вариант не запущен.");
  }
  const reservations = new Map<string, { sourceBytes: number; prepareTextFiles: boolean }>();
  for (const { asset, revisionHash, prepareTextFiles } of selected) {
    const declaredBytes = asset.size_bytes;
    if (declaredBytes != null && (!Number.isFinite(declaredBytes) || declaredBytes < 0 || declaredBytes > MAX_FILE_BYTES)) {
      throw new Error("Файлы выбранного варианта превышают размер подготовки устройства. Вариант не запущен.");
    }
    const sourceUpperBound = declaredBytes ?? 0;
    const key = `${revisionHash}\u001f${asset.id}`;
    const previous = reservations.get(key);
    reservations.set(key, {
      sourceBytes: Math.max(previous?.sourceBytes ?? 0, sourceUpperBound),
      prepareTextFiles: Boolean(previous?.prepareTextFiles || prepareTextFiles),
    });
  }
  const knownBytes = [...reservations.values()].reduce((total, { sourceBytes }) => total + sourceBytes, 0);
  const deviceBytesLimit = Math.min(MAX_PREPARED_ASSET_BYTES, deviceCacheLimitBytes());
  if (knownBytes > MAX_PREPARED_ASSET_BYTES) {
    throw new Error("Файлы выбранного варианта превышают доступный объём подготовки устройства. Вариант не запущен.");
  }
  if (knownBytes > deviceBytesLimit) {
    throw new Error("Файлы выбранного варианта не помещаются в кэш устройства. Увеличьте лимит файлов в Настройках и повторите подготовку.");
  }
  let loadedBytes = 0;
  const countedFiles = new Set<string>();
  const countLoadedFile = (key: string, bytes: Uint8Array) => {
    if (countedFiles.has(key)) return;
    countedFiles.add(key);
    loadedBytes += bytes.byteLength;
    if (loadedBytes > deviceBytesLimit) {
      throw new Error("Файлы выбранного варианта превышают доступный объём подготовки устройства. Вариант не запущен.");
    }
  };
  let cursor = 0;
  const worker = async () => {
    while (cursor < selected.length) {
      const index = cursor;
      cursor += 1;
      const { asset, revisionHash, lineNumber, prepareTextFiles } = selected[index];
      let lastError: unknown = null;
      for (let attempt = 0; attempt < PREPARED_ASSET_ATTEMPTS; attempt += 1) {
        signal.throwIfAborted();
        try {
          const original = await loadDeviceAsset(asset, revisionHash, signal);
          countLoadedFile(`${revisionHash}\u001f${asset.id}\u001f${original.sha256}`, original.bytes);
          if (prepareTextFiles && lineNumber === 9) {
            const textFiles = await loadDeviceTexts(asset, revisionHash, signal);
            for (const file of textFiles) {
              countLoadedFile(`${revisionHash}\u001f${asset.id}\u001f${file.converterVersion ?? ""}\u001f${file.name}\u001f${file.sha256}`, file.bytes);
            }
          }
          lastError = null;
          break;
        } catch (reason: unknown) {
          lastError = reason;
          if (signal.aborted) throw reason;
        }
      }
      if (lastError) throw lastError;
    }
  };
  const workerCount = selected.some(({ asset }) => asset.size_bytes == null)
    ? 1
    : Math.min(PREPARED_ASSET_WORKERS, selected.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

function isAbortLike(reason: unknown): boolean {
  return typeof DOMException !== "undefined" && reason instanceof DOMException && reason.name === "AbortError";
}

export function ExamsView() {
  const shell=useKonturShell();
  const [exams, setExams] = useState<ExamRecord[]>([]);
  const [saved, setSaved] = useState<ExamRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [examMode, setExamMode] = useState<Exclude<ExamMode, "legacy">>("strict");
  const [prepareKind, setPrepareKind] = useState<ExamPrepareKind>("full");
  const [prepareLine, setPrepareLine] = useState("1");
  const [prepareCount, setPrepareCount] = useState("1");
  const [customLines, setCustomLines] = useState([{line:4,count:1},{line:14,count:2},{line:24,count:1}]);
  const [prepareDifficulty, setPrepareDifficulty] = useState<ExamPrepareDifficulty>("any");
  const [prepareDuration, setPrepareDuration] = useState("235");
  const [excludeSolved, setExcludeSolved] = useState(false);
  const [excludeRecent, setExcludeRecent] = useState(false);
  const [sourceYear, setSourceYear] = useState("");
  const [includeCorrectedErrors, setIncludeCorrectedErrors] = useState(false);
  const [prepared, setPrepared] = useState<ExamRecord | null>(null);
  const prepareRequestRef = useRef<{fingerprint:string;key:string}|null>(null);
  const [templateBusy, setTemplateBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ExamReport | null>(null);
  const [reportBusy, setReportBusy] = useState<string | null>(null);
  const [assetFetchAvailable, setAssetFetchAvailable] = useState(!DEMO_MODE);
  const [initialLoadError, setInitialLoadError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const preparedAssetsAbortRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    preparedAssetsAbortRef.current?.abort();
    preparedAssetsAbortRef.current = null;
  }, []);

  useEffect(() => {
    let cancelled = false;
    setInitialLoadError(null);
    Promise.all([getExamBlueprints(), getActiveExam()]).then(async ([items, active]) => {
      const [history, capabilities] = await Promise.all([getSavedExams(), getCapabilities()]);
      if (cancelled) return;
      const records = mergeActiveExam(items, active);
      const savedRecords = mergeSavedExams(history, active);
      const ready = savedRecords.find((item) => item.lifecycle === "ready" && item.kind);
      const restoredReady = ready?.exam_session_id ? await getSavedExam(ready.exam_session_id) : null;
      if (cancelled) return;
      setExams(records);
      setSaved(savedRecords);
      setPrepared(restoredReady);
      setAssetFetchAvailable(!DEMO_MODE && capabilities.asset_fetch.available);
      setLoading(false);
    }).catch((reason: unknown) => {
      if (!cancelled) {
        const message = reason instanceof Error ? reason.message : "Экзамены недоступны";
        setInitialLoadError(message);
        setError(message);
        setLoading(false);
      }
    });
    return () => { cancelled = true; };
  }, [loadAttempt]);

  function updateExam(next: ExamRecord) {
    const active: ExamRecord = { ...next, status: next.status === "paused" ? "paused" : "active", lifecycle: next.lifecycle ?? "active" };
    setSaved((current) => mergeSavedExams(current, active));
    setExams((current) => current.map((item) => item.id === (active.blueprint_id ?? active.id) ? { ...item, ...active, id: item.id } : item));
  }

  async function prepareVariant() {
    const line = Number(prepareLine);
    const count = Number(prepareCount);
    const duration = Number(prepareDuration);
    if (!Number.isInteger(duration) || duration < 1 || duration > 1440) {
      setError("Длительность должна быть от 1 до 1440 минут.");
      return;
    }
    if ((prepareKind === "thematic" || prepareKind === "errors") && (!Number.isInteger(line) || line < 1 || line > 27 || !Number.isInteger(count) || count < 1 || count > 100)) {
      setError("Линия должна быть от 1 до 27, число заданий — от 1 до 100.");
      return;
    }
    if (prepareKind === "custom" && (!customLines.length || new Set(customLines.map(row=>row.line)).size !== customLines.length || customLines.some(row=>!Number.isInteger(row.line)||row.line<1||row.line>27||!Number.isInteger(row.count)||row.count<1||row.count>100))) {
      setError("Выберите разные типы 1–27 и количество от 1 до 100 для каждого."); return;
    }
    const input: ExamPrepareInput = {
      kind: prepareKind,
      mode: examMode,
      required_lines: prepareKind === "full" ? Object.fromEntries(Array.from({ length: 27 }, (_, index) => [String(index + 1), 1])) : prepareKind === "custom" ? Object.fromEntries(customLines.map(row=>[String(row.line),row.count])) : { [String(line)]: count },
      difficulty: prepareDifficulty,
      duration_minutes: duration,
      exclude_solved: excludeSolved,
      exclude_recent: excludeRecent,
      source_year: sourceYear.trim() ? Number(sourceYear) : null,
      include_corrected_errors: prepareKind === "errors" && includeCorrectedErrors,
    };
    if (input.source_year !== null && (!Number.isInteger(input.source_year) || input.source_year < 2000 || input.source_year > 2100)) {
      setError("Год источника должен быть от 2000 до 2100.");
      return;
    }
    setBusyId("prepare");
    setError(null);
    setReport(null);
    try {
      const fingerprint=JSON.stringify(input);
      if(prepareRequestRef.current?.fingerprint!==fingerprint)prepareRequestRef.current={fingerprint,key:"exam-prepare:"+crypto.randomUUID()};
      const next = await prepareExam(input,prepareRequestRef.current.key);
      prepareRequestRef.current=null;
      setPrepared(next);
      setSaved((current) => mergeSavedExams(current, next));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Не удалось подготовить вариант");
    } finally {
      setBusyId(null);
    }
  }

  async function startPrepared() {
    if (!prepared?.exam_session_id || preparedAssetsAbortRef.current) return;
    const examSessionId = prepared.exam_session_id;
    const controller = new AbortController();
    preparedAssetsAbortRef.current = controller;
    setBusyId(`start:${examSessionId}`);
    setError(null);
    try {
      await preloadPreparedExamAssets(prepared, controller.signal);
      const verified = await markExamAssetsReady(examSessionId, `ui-assets-ready:${examSessionId}`);
      setPrepared(verified);
      setSaved((current) => mergeSavedExams(current, verified));
      if (!verified.critical_files_ready || (verified.missing_critical_files?.length ?? 0) > 0) {
        throw new Error("Не все выбранные материалы готовы на устройстве. Вариант сохранён — повторите подготовку после проверки файлов.");
      }
      controller.signal.throwIfAborted();
      const next = await startPreparedExam(examSessionId, `ui-start:${examSessionId}`);
      setPrepared(null);
      updateExam(next);
    } catch (reason: unknown) {
      if (!isAbortLike(reason)) setError(reason instanceof Error ? reason.message : "Не удалось начать подготовленный вариант");
    } finally {
      if (preparedAssetsAbortRef.current === controller) {
        preparedAssetsAbortRef.current = null;
        setBusyId(null);
      }
    }
  }

  async function beginDifficulty(difficulty: ExamDifficulty) {
    setBusyId(`difficulty:${difficulty}`); setError(null); setReport(null);
    try { updateExam(await startDifficultyExam(difficulty, examMode)); }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Не удалось собрать вариант"); }
    finally { setBusyId(null); }
  }

  async function beginTemplate(id: string) {
    setBusyId(id); setError(null); setReport(null);
    try { updateExam(await startExam(id, examMode)); }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Не удалось начать вариант"); }
    finally { setBusyId(null); }
  }

  async function addOfficialTemplate() {
    if (templateBusy) return;
    setTemplateBusy(true); setError(null);
    try {
      const result = await instantiateOfficialExamTemplate();
      const blueprint = asExamRecord(result.blueprint);
      setExams((current) => current.some((item) => item.id === blueprint.id) ? current.map((item) => item.id === blueprint.id ? { ...item, ...blueprint } : item) : [blueprint, ...current]);
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Не удалось добавить шаблон ЕГЭ-2027"); }
    finally { setTemplateBusy(false); }
  }

  function handleReport(next: ExamReport) {
    setReport(next);
    setSaved((current) => current.map((item) => item.exam_session_id === next.exam_session_id ? { ...item, status: "completed", lifecycle: "completed" } : item));
    setExams((current) => current.map((item) => item.exam_session_id === next.exam_session_id ? { ...item, status: "completed", lifecycle: "completed" } : item));
  }

  async function openReport(exam: ExamRecord) {
    const active = saved.find(isInProgress) ?? null;
    if (active || !exam.exam_session_id || reportBusy || report?.exam_session_id === exam.exam_session_id) return;
    setReportBusy(exam.exam_session_id); setError(null);
    try { setReport(await getExamReport(exam.exam_session_id)); }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Не удалось открыть разбор"); }
    finally { setReportBusy(null); }
  }

  function retryInitialLoad() {
    setLoading(true);
    setError(null);
    setInitialLoadError(null);
    setLoadAttempt((attempt) => attempt + 1);
  }

  const active = saved.find(isInProgress) ?? null;
  const templates = exams.filter((item) => !item.exam_session_id && item.status === "ready");
  useEffect(()=>{shell.setWorkspaceActive(Boolean(active));return()=>shell.setWorkspaceActive(false);},[Boolean(active),shell.setWorkspaceActive]);
  const reportExamId = report ? saved.find((item) => item.exam_session_id === report.exam_session_id)?.id ?? null : null;
  if(active)return <main className={styles.workspacePage}><h1 className="sr-only">{active.name}</h1><ExamRunner assetFetchAvailable={assetFetchAvailable} exam={active} report={report} onExamUpdated={updateExam} onReport={handleReport}/></main>;
  if (initialLoadError && !DEMO_MODE && !exams.length && !saved.length) return <ExamState title="Экзамены недоступны" message={initialLoadError} onRetry={retryInitialLoad} />;

  return <main className={`page-content exams-page ${styles.page}`}>
    <header className={`page-heading ${styles.hero}`}><div><span className="eyebrow">ТРЕНИРОВОЧНЫЕ ВАРИАНТЫ</span><h1>Варианты</h1><p className="page-lead">Выберите задания и режим тренировки.</p></div><span className="exam-integrity"><Icon name="lock" size={15} />Без прокторинга</span></header>
    {DEMO_MODE ? <div className="demo-inline-note" role="note"><Icon name="spark" size={15} />TEST_ONLY preview: подготовка вариантов требует основной рабочей области.</div> : null}
    {error ? <div className="inline-error" role="alert"><Icon name="warning" size={15} />{error}</div> : null}
    {report && !active ? <ReportPanel examId={reportExamId} report={report} /> : null}
    <section className={active ? styles.variantSectionHidden : styles.variantSection} aria-labelledby="variant-heading">
      <div className={styles.sectionHeading}><div><span className="eyebrow">НОВЫЙ ВАРИАНТ</span><h2 id="variant-heading">Соберите пробник</h2><p>Сначала посмотрите состав. Таймер запустится, когда вы нажмёте «Начать».</p></div><span className={styles.sourceNote}><Icon name="shield" size={14} />Материалы проверяются перед началом</span></div>
      <div className={styles.modePicker} role="radiogroup" aria-label="Режим варианта">
        <span className={styles.modeLabel}>Режим</span>
        <button aria-checked={examMode === "strict"} className={`${styles.modeButton} ${examMode === "strict" ? styles.modeButtonActive : ""}`} onClick={() => setExamMode("strict")} onKeyDown={(event) => { if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); setExamMode("study"); } }} role="radio" type="button"><strong>Строгий</strong><small>Как на экзамене: без паузы и подсказок.</small></button>
        <button aria-checked={examMode === "study"} className={`${styles.modeButton} ${examMode === "study" ? styles.modeButtonActive : ""}`} onClick={() => setExamMode("study")} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); setExamMode("strict"); } }} role="radio" type="button"><strong>Учебный</strong><small>Можно поставить на паузу и вернуться позже.</small></button>
      </div>
      <div className={styles.prepareKindGrid} role="radiogroup" aria-label="Тип пробника">{PREPARE_KINDS.map((item, index) => <button aria-checked={prepareKind === item.value} className={`${styles.prepareKindCard} ${prepareKind === item.value ? styles.prepareKindCardActive : ""}`} key={item.value} onClick={() => setPrepareKind(item.value)} onKeyDown={(event) => { if (event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); setPrepareKind(PREPARE_KINDS[(index + 1) % PREPARE_KINDS.length].value); } if (event.key === "ArrowLeft" || event.key === "ArrowUp") { event.preventDefault(); setPrepareKind(PREPARE_KINDS[(index + PREPARE_KINDS.length - 1) % PREPARE_KINDS.length].value); } }} role="radio" type="button"><strong>{item.label}</strong><span>{item.description}</span></button>)}</div>
      {prepareKind === "custom" && <section className={styles.customComposition} aria-label="Состав пользовательского варианта"><h3>Типы заданий</h3>{customLines.map((row,index)=><div className={styles.customRow} key={index}><label>Тип<select aria-label={`Тип в строке ${index+1}`} value={row.line} onChange={event=>setCustomLines(rows=>rows.map((item,i)=>i===index?{...item,line:Number(event.target.value)}:item))}>{Array.from({length:27},(_,i)=><option disabled={customLines.some((other,j)=>j!==index&&other.line===i+1)} key={i} value={i+1}>{i+1}</option>)}</select></label><label>Количество<input aria-label={`Количество в строке ${index+1}`} type="number" min={1} max={100} value={row.count} onChange={event=>setCustomLines(rows=>rows.map((item,i)=>i===index?{...item,count:Number(event.target.value)}:item))}/></label><button className="icon-button" aria-label={`Убрать тип ${row.line}`} disabled={customLines.length===1} onClick={()=>setCustomLines(rows=>rows.filter((_,i)=>i!==index))} type="button"><Icon name="close" size={18}/></button></div>)}<button className="outline-button" disabled={customLines.length>=27} onClick={()=>setCustomLines(rows=>[...rows,{line:Array.from({length:27},(_,i)=>i+1).find(line=>!rows.some(row=>row.line===line))!,count:1}])} type="button">Добавить тип</button></section>}
      <div className={styles.prepareParams}>
        {prepareKind === "thematic" || prepareKind === "errors" ? <label>Тип задания<input inputMode="numeric" min={1} max={27} onChange={(event) => setPrepareLine(event.target.value)} type="number" value={prepareLine} /></label> : null}
        {prepareKind === "thematic" || prepareKind === "errors" ? <label>Количество заданий<input inputMode="numeric" min={1} max={100} onChange={(event) => setPrepareCount(event.target.value)} type="number" value={prepareCount} /></label> : null}
        <label>Сложность<select onChange={(event) => setPrepareDifficulty(event.target.value as ExamPrepareDifficulty)} value={prepareDifficulty}><option value="any">Любая</option><option value="easy">Базовая</option><option value="medium">Средняя</option><option value="hard">Сложная</option></select></label>
        <label>Длительность, минут<input inputMode="numeric" min={1} max={1440} onChange={(event) => setPrepareDuration(event.target.value)} type="number" value={prepareDuration} /></label>
        <label>Год источника<input inputMode="numeric" min={2000} max={2100} onChange={(event) => setSourceYear(event.target.value)} placeholder="Любой" type="number" value={sourceYear} /></label>
        <label className={styles.prepareCheck}><input checked={excludeSolved} onChange={(event) => setExcludeSolved(event.target.checked)} type="checkbox" />Исключить решённые</label>
        <label className={styles.prepareCheck}><input checked={excludeRecent} onChange={(event) => setExcludeRecent(event.target.checked)} type="checkbox" />Исключить последние 30 дней</label>
        {prepareKind === "errors" ? <label className={styles.prepareCheck}><input checked={includeCorrectedErrors} onChange={(event) => setIncludeCorrectedErrors(event.target.checked)} type="checkbox" />Включить исправленные ошибки</label> : null}
        <button className="primary-button" disabled={Boolean(active) || busyId !== null || DEMO_MODE} onClick={() => void prepareVariant()} type="button">{busyId === "prepare" ? <><span className="button-spinner" />Подготавливаем…</> : "Подготовить вариант"}</button>
      </div>
    </section>
    {prepared && !active ? <PreparedExamPreview busy={busyId === `start:${prepared.exam_session_id}`} exam={prepared} onStart={() => void startPrepared()} /> : null}
    <section className={styles.historySection} aria-labelledby="history-heading"><div className={styles.sectionHeadingCompact}><div><span className="eyebrow">ИСТОРИЯ</span><h2 id="history-heading">Сохранённые варианты</h2></div><span className="muted-copy">Черновики сохраняются автоматически; разбор открывается после сдачи</span></div>{loading ? <div className="loading-row"><span className="button-spinner" />Загружаем варианты…</div> : null}{!loading && !saved.length ? <div className="empty-block"><Icon name="exam" size={20} /><h2>История пока пуста</h2><p>Подготовьте первый вариант выше.</p></div> : null}<div className={styles.historyList}>{saved.map((exam) => <ExamHistoryCard blocked={Boolean(active)} exam={exam} busy={reportBusy === exam.exam_session_id} onOpenReport={openReport} key={exam.exam_session_id ?? exam.id} />)}</div></section>
    {templates.length || (!DEMO_MODE && !exams.some((item) => item.template_id === OFFICIAL_2027_TEMPLATE_ID)) ? <details className={styles.advancedTemplates}><summary>Дополнительные зафиксированные шаблоны</summary>{!DEMO_MODE && !exams.some((item) => item.template_id === OFFICIAL_2027_TEMPLATE_ID) ? <button className="outline-button" disabled={templateBusy} onClick={() => void addOfficialTemplate()} type="button">{templateBusy ? "Создаём…" : "Добавить шаблон ЕГЭ-2027"}</button> : null}{templates.map((exam) => <ExamCard exam={exam} busy={busyId === exam.id} blocked={Boolean(active)} onStart={beginTemplate} key={exam.id} />)}</details> : null}
  </main>;
}

function PreparedExamPreview({ exam, busy, onStart }: { exam: ExamRecord; busy: boolean; onStart: () => void }) {
  const items = exam.items ?? [];
  const missing = exam.missing_critical_files ?? [];
  const startDisabled = busy || !items.length;
  const materialsState = exam.critical_files_ready && missing.length === 0 ? "Материалы готовы" : "Проверим материалы перед стартом";
  return <section className={styles.preparePreview} aria-labelledby="prepared-preview-heading"><div className={styles.preparePreviewHeader}><div><span className="eyebrow">ПРЕДПРОСМОТР ГОТОВ</span><h2 id="prepared-preview-heading">{exam.name}</h2><p>{exam.mode === "strict" ? "Строгий режим" : "Учебный режим"} · задания выбраны, таймер ещё не запущен.</p></div><span className={styles.previewState}>{startDisabled ? "Нет заданий" : materialsState}</span></div><div className={styles.previewFacts}><Fact label="Позиций" value={String(items.length)} /><Fact label="Время" value={exam.duration_minutes != null ? `${exam.duration_minutes} мин` : "—"} /><Fact label="Тип" value={prepareKindLabel(exam.kind)} /></div><div className={styles.previewItems}>{items.map((item) => <div className={styles.previewItem} key={item.id}><span>{item.position + 1}</span><div><strong>{item.task_type ?? (item.line_number != null ? `Линия ${item.line_number}` : "Задание")}</strong><small>{item.source_id ? `КЕГЭ № ${item.source_id} · ` : ""}{item.assets?.length ? item.assets.map((asset) => asset.name).join(", ") : "Без файлов"}</small></div></div>)}</div>{missing.length ? <div className={styles.previewMissing} role="alert"><strong>Материалы будут проверены заново перед стартом</strong><ul>{missing.map((file) => <li key={file.asset_id}>{file.filename}: {fileReasonLabel(file.reason)}</li>)}</ul><span>Состав варианта не заменяется автоматически.</span></div> : null}<button className="primary-button" disabled={startDisabled} onClick={onStart} type="button">{busy ? <><span className="button-spinner" />Готовим файлы…</> : "Подготовить и начать"}</button></section>;
}

function prepareKindLabel(value: ExamPrepareKind | undefined): string { return PREPARE_KINDS.find((item) => item.value === value)?.label ?? "Вариант"; }
function fileReasonLabel(value: string): string { return value === "not_ready" ? "файл ещё не подготовлен" : value === "hash_changed" || value === "source_hash_changed" ? "изменился контрольный хеш" : value === "rights_not_authorized" ? "права не подтверждены" : "файл недоступен"; }
function asExamRecord(exam: ExamBlueprint): ExamRecord { return { ...exam, mode: "legacy", sealed: true, lifecycle: "ready" }; }
function isInProgress(exam: ExamRecord): boolean { return exam.status === "active" || exam.status === "paused"; }
// Revision IDs remain server-owned fields and stay out of the learner-facing setup cards.
function mergeActiveExam(items: ExamBlueprint[], active: ExamRecord | null): ExamRecord[] { const records = items.map(asExamRecord); if (!active) return records; const id = active.blueprint_id ?? active.id; const index = records.findIndex((item) => item.id === id); const status = active.status === "paused" ? "paused" : "active"; return index >= 0 ? records.map((item, itemIndex) => itemIndex === index ? { ...item, ...active, id: item.id, blueprint_id: id, exam_session_id: active.exam_session_id ?? active.id, status } : item) : [{ ...active, id, blueprint_id: id, exam_session_id: active.exam_session_id ?? active.id, status }, ...records]; }
function mergeSavedExams(items: ExamRecord[], active: ExamRecord | null | ExamRecord[]): ExamRecord[] { const additions = Array.isArray(active) ? active : active ? [active] : []; const byKey = new Map<string, ExamRecord>(); for (const item of [...items, ...additions]) byKey.set(item.exam_session_id ?? item.id, { ...byKey.get(item.exam_session_id ?? item.id), ...item }); return [...byKey.values()].sort((left, right) => (Date.parse(right.started_at ?? "") || 0) - (Date.parse(left.started_at ?? "") || 0)); }
function ExamRunner({ assetFetchAvailable, exam, report, onExamUpdated, onReport }: { assetFetchAvailable: boolean; exam: ExamRecord; report: ExamReport | null; onExamUpdated: (exam: ExamRecord) => void; onReport: (report: ExamReport) => void }) {
  const shell = useKonturShell();
  const sectionRef = useRef<HTMLElement>(null);
  const [positionsOpen,setPositionsOpen]=useState(false);
  const [positionsPoint,setPositionsPoint]=useState({left:8,top:68});
  const positionsButtonRef=useRef<HTMLButtonElement|null>(null);
  const positionsMenuRef=useRef<HTMLElement|null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>(() => exam.answers ?? {});
  const [assetsByItem, setAssetsByItem] = useState<Record<string, AssetRef[]>>(() => Object.fromEntries((exam.items ?? []).map((item) => [item.id, item.assets ?? []])));
  const [workspaces, setWorkspaces] = useState<Record<string, ExamWorkspace>>({});
  const [codeByItem, setCodeByItem] = useState<Record<string, string>>({});
  const [surfaceByItem, setSurfaceByItem] = useState<Record<string, "code" | "board">>({});
  const [flaggedByItem, setFlaggedByItem] = useState<Record<string, boolean>>({});
  const [currentIndex, setCurrentIndex] = useState(0);
  const [savingItem, setSavingItem] = useState<string | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirmFinish, setConfirmFinish] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [runnerError, setRunnerError] = useState<string | null>(null);
  const [codeRunsByItem, setCodeRunsByItem] = useState<Record<string, ExamCodeRun[]>>({});
  const [localCodeConflicts, setLocalCodeConflicts] = useState<Record<string, string>>({});
  const localCodeConflictsRef = useRef<Record<string, string>>({});
  const [codeConflictReady,setCodeConflictReady]=useState<Record<string,boolean>>({});
  const codeConflictReadyRef=useRef<Record<string,boolean>>({});
  const [codeRunBusy, setCodeRunBusy] = useState(false);
  const [studyAiQuestion, setStudyAiQuestion] = useState("");
  const [studyAiRun, setStudyAiRun] = useState<ExamStudyAiRun | null>(null);
  const [sentStudyQuestion,setSentStudyQuestion]=useState("");
  const [studyAiBusy, setStudyAiBusy] = useState(false);
  const [studyAiError, setStudyAiError] = useState<string | null>(null);
  const [serverExpired, setServerExpired] = useState(false);
  const answersRef = useRef<Record<string, string>>(exam.answers ?? {});
  const codeRef = useRef<Record<string, string>>({});
  const workspacesRef = useRef<Record<string, ExamWorkspace>>({});
  const initializedExamRef = useRef<string | null>(null);
  const deadlineExpiredRef = useRef(false);
  const dirtyItems = useRef(new Set<string>());
  const saveTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const pendingSaves = useRef(new Map<string, { content: string; operation: Promise<void> }>());
  const dirtyCodeItems = useRef(new Set<string>());
  const codeSaveTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const pendingCodeSaves = useRef(new Map<string, { content: string; operation: Promise<ExamWorkspace> }>());
  const deadlineFinalizedRef = useRef(false);
  const flushDirtyAnswersRef = useRef<() => Promise<void>>(async () => undefined);
  const studyAiPollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const codeRunRequestRef = useRef(0);
  const codeRunLifecycleRef = useRef({
    examKey: "",
    itemId: null as string | null,
    taskRevisionId: "",
    paused: false,
    pausing: false,
    resuming: false,
    submitting: false,
    confirmFinish: false,
    serverExpired: false,
  });
  const sessionId = exam.exam_session_id ?? "";
  const examKey = `${exam.id}:${sessionId}`;
  const items = exam.items ?? [];
  const currentItem = items[currentIndex] ?? null;
  const currentItemIdRef=useRef<string|null>(null);currentItemIdRef.current=currentItem?.id??null;
  const uiInitializedRef=useRef<string|null>(null);
  const currentWorkspace = currentItem ? workspaces[currentItem.id] ?? null : null;
  const paused = exam.status === "paused";
  const canPause = exam.mode === "legacy" || exam.mode === "study";
  const isStudy = exam.mode === "study";
  const boardFirst = new Set([4, 7, 11, 12]);
  const studyAiScope = currentItem ? examDraftScope(exam.id, currentItem.id) : "";

  useEffect(() => {
    invalidateCodeRun();
  }, [examKey, currentItem?.id]);

  useEffect(()=>{if(uiInitializedRef.current===examKey&&currentItem)saveExamUiState(sessionId,{itemId:currentItem.id,flags:flaggedByItem,surfaces:surfaceByItem});},[examKey,sessionId,currentItem?.id,flaggedByItem,surfaceByItem]);

  const examDraftKey=()=>JSON.stringify([Object.entries(answersRef.current).sort(),Object.entries({...codeRef.current,...localCodeConflictsRef.current}).sort()]);
  useEffect(() => registerDraftFlush(() => flushDirtyAnswersRef.current(),"exam:"+sessionId,examDraftKey), [sessionId]);
  useEffect(()=>registerDraftSaveState(()=>{
    const answerIds=[...dirtyItems.current],codeIds=[...dirtyCodeItems.current];
    const conflict=Object.keys(localCodeConflictsRef.current).length>0;
    return {dirty:Boolean(answerIds.length||codeIds.length||conflict),saving:Boolean(pendingSaves.current.size||pendingCodeSaves.current.size),
      conflict,offline:typeof navigator!=="undefined"&&!navigator.onLine,
      localRecoverable:answerIds.every(id=>isLocalDraftDurable(examDraftScope(exam.id,id),"answer")&&readLocalDraft<string>(examDraftScope(exam.id,id),"answer")?.content===answersRef.current[id])
        &&codeIds.every(id=>isLocalDraftDurable(examDraftScope(exam.id,id),"code")&&readLocalDraft<string>(examDraftScope(exam.id,id),"code")?.content===codeRef.current[id])
        &&Object.entries(localCodeConflictsRef.current).every(([id,content])=>isLocalDraftDurable(examDraftScope(exam.id,id),"code")&&readLocalDraft<string>(examDraftScope(exam.id,id),"code")?.content===content)};
  },"exam:"+sessionId,examDraftKey),[sessionId,exam.id]);


  useEffect(() => {
    if (initializedExamRef.current === examKey) return;
    initializedExamRef.current = examKey;
    deadlineExpiredRef.current = false;
    deadlineFinalizedRef.current = false;
    setServerExpired(false);
    const restoredUi=readExamUiState(sessionId,items.map(item=>item.id));
    uiInitializedRef.current=examKey;
    setCurrentIndex(Math.max(0,items.findIndex(item=>item.id===restoredUi.itemId)));
    setConfirmFinish(false);
    dirtyItems.current.clear();
    dirtyCodeItems.current.clear();
    workspacesRef.current = {};
    codeRef.current = {};
    setWorkspaces({});
    setCodeByItem({});
    setSurfaceByItem(restoredUi.surfaces);
    setFlaggedByItem(restoredUi.flags);
    setCodeRunsByItem({});
    codeConflictReadyRef.current={};setCodeConflictReady({});
    localCodeConflictsRef.current = {};
    setLocalCodeConflicts({});
    for (const timer of saveTimers.current.values()) clearTimeout(timer);
    for (const timer of codeSaveTimers.current.values()) clearTimeout(timer);
    saveTimers.current.clear();
    codeSaveTimers.current.clear();
    pendingSaves.current.clear();
    pendingCodeSaves.current.clear();
    const serverAnswers = exam.answers ?? {};
    const restored = { ...serverAnswers };
    const pendingAnswerItems: string[] = [];
    for (const item of exam.items ?? []) {
      const scope = examDraftScope(exam.id, item.id);
      const draft = readLocalDraft<string>(scope, "answer");
      if (!draft || typeof draft.content !== "string") continue;
      if (draft.content === (serverAnswers[item.id] ?? "")) {
        clearLocalDraft(scope, "answer");
        continue;
      }
      restored[item.id] = draft.content;
      pendingAnswerItems.push(item.id);
    }
    answersRef.current = restored;
    setAnswers(restored);
    setAssetsByItem(Object.fromEntries((exam.items ?? []).map((item) => [item.id, item.assets ?? []])));
    for (const itemId of pendingAnswerItems) {
      dirtyItems.current.add(itemId);
      if (exam.status === "active") scheduleSave(itemId);
    }
  }, [exam, examKey]);

  useEffect(() => {
    if (!sessionId || !currentItem) return undefined;
    const itemId = currentItem.id;
    let cancelled = false;
    setWorkspaceLoading(true);
    setWorkspaceError(null);
    void getExamWorkspace(sessionId, itemId).then((next) => {
      if (cancelled) return;
      workspacesRef.current = { ...workspacesRef.current, [itemId]: next };
      const draftScope = examDraftScope(exam.id, itemId);
      const localDraft = readLocalDraft<string>(draftScope, "code");
      const assessment = assessLocalDraft(localDraft, next.code.version, next.code.content);
      if (assessment.status === "confirmed") clearLocalDraft(draftScope, "code");
      const restoredCode = assessment.status === "restore" && exam.status === "active" && !next.read_only
        && typeof assessment.content === "string" ? assessment.content : next.code.content;
      if (assessment.status === "conflict") {
        if (typeof assessment.content === "string") {
          setCodeConflict(itemId, assessment.content);
        }
        setRunnerError("Найдена локальная версия кода новее серверного черновика. Она сохранена отдельно; выберите её вручную после сверки.");
      }
      codeRef.current = { ...codeRef.current, [itemId]: restoredCode };
      setWorkspaces((current) => ({ ...current, [itemId]: next }));
      setCodeByItem((current) => ({ ...current, [itemId]: restoredCode }));
      if (restoredCode !== next.code.content && assessment.status === "restore") {
        dirtyCodeItems.current.add(itemId);
        scheduleCodeSave(itemId);
      }
      setWorkspaceLoading(false);
    }).catch((reason: unknown) => {
      if (cancelled) return;
      setWorkspaceLoading(false);
      setWorkspaceError(reason instanceof Error ? reason.message : "Рабочее место задания недоступно");
    });
    // Browser history is read-only local evidence. It is intentionally loaded
    // independently from the sealed server run history and carries the saved
    // browser code version when one is available.
    const localRuns = readDevicePythonRuns(browserExamRunScope(exam.id, sessionId, itemId)).map((run) => toLocalExamCodeRun(run, sessionId, itemId, currentItem?.task_revision_id ?? "", 0, []));
    if (localRuns.length) setCodeRunsByItem((current) => ({ ...current, [itemId]: mergeExamCodeRuns(localRuns, current[itemId] ?? []) }));
    void listExamCodeRuns(sessionId, itemId).then((runs) => {
      if (!cancelled) setCodeRunsByItem((current) => ({ ...current, [itemId]: mergeExamCodeRuns(current[itemId] ?? [], runs) }));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [currentItem?.id, examKey, sessionId, exam.status]);

  useEffect(() => {
    let cancelled=false;
    setStudyAiRun(null);setSentStudyQuestion("");
    setStudyAiError(null);
    const checkpoint=currentItem?readStudyCheckpoint(examDraftScope(exam.id,currentItem.id)):null;
    if(isStudy&&currentItem&&checkpoint)void getExamStudyAi(sessionId,currentItem.id,checkpoint.runId).then(run=>{if(!cancelled&&currentItemIdRef.current===run.item_id){setStudyAiRun(run);setSentStudyQuestion(checkpoint.question);}}).catch(()=>undefined);
    setStudyAiQuestion(currentItem ? readExamStudyAiDraft(examDraftScope(exam.id, currentItem.id)) : "");
    if (studyAiPollTimer.current) {
      clearTimeout(studyAiPollTimer.current);
      studyAiPollTimer.current = null;
    }
    return()=>{cancelled=true;};
  }, [currentItem?.id, exam.id]);

  useEffect(() => {
    const handleVisibility = () => { if (document.visibilityState === "hidden") void flushDirtyAnswersRef.current().catch(() => undefined); };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibility);
      for (const timer of saveTimers.current.values()) clearTimeout(timer);
      for (const timer of codeSaveTimers.current.values()) clearTimeout(timer);
      saveTimers.current.clear();
      codeSaveTimers.current.clear();
      void flushDirtyAnswersRef.current().catch(() => undefined);
    };
  }, [sessionId]);

  function acceptWorkspace(next: ExamWorkspace, acknowledgedCode?: string, previousServerCode?: string): boolean {
    const itemId = next.item_id;
    const localCode = codeRef.current[itemId];
    const serverCodeBeforeRequest = previousServerCode ?? workspacesRef.current[itemId]?.code.content;
    const codeAck = resolveExamCodeAck(
      localCode,
      acknowledgedCode ?? serverCodeBeforeRequest ?? next.code.content,
      next.code.content,
    );
    workspacesRef.current = { ...workspacesRef.current, [itemId]: next };
    setWorkspaces((current) => ({ ...current, [itemId]: next }));
    if (codeAck.applied) {
      codeRef.current = { ...codeRef.current, [itemId]: next.code.content };
      setCodeByItem((current) => ({ ...current, [itemId]: next.code.content }));
    } else if (typeof codeAck.content === "string") {
      // Rebase the newer local draft on the acknowledged server version.
      saveLocalDraft(examDraftScope(exam.id, itemId), "code", next.code.version, codeAck.content);
    }
    return codeAck.applied;
  }

  async function save(itemId: string) {
    const timer = saveTimers.current.get(itemId);
    if (timer) { clearTimeout(timer); saveTimers.current.delete(itemId); }
    if (deadlineExpiredRef.current) { dirtyItems.current.delete(itemId); return; }
    const content = answersRef.current[itemId] ?? "";
    const pending = pendingSaves.current.get(itemId);
    if (pending) {
      await pending.operation;
      if (!dirtyItems.current.has(itemId)) return;
      if (pending.content === (answersRef.current[itemId] ?? "")) { dirtyItems.current.delete(itemId); return; }
      return save(itemId);
    }
    if (!dirtyItems.current.has(itemId)) return;
    dirtyItems.current.delete(itemId);
    setSavingItem(itemId); setRunnerError(null);
    const operation = saveExamAnswer(sessionId, itemId, content);
    pendingSaves.current.set(itemId, { content, operation });
    try {
      await operation;
      const latestContent = answersRef.current[itemId] ?? "";
      if (latestContent === content) {
        clearLocalDraft(examDraftScope(exam.id, itemId), "answer");
      } else {
        saveLocalDraft(examDraftScope(exam.id, itemId), "answer", 0, latestContent);
        dirtyItems.current.add(itemId);
      }
    } catch (reason: unknown) {
      if (isExamTimeExpired(reason)) { markDeadlineExpired(); dirtyItems.current.delete(itemId); }
      else { dirtyItems.current.add(itemId); setRunnerError(reason instanceof Error ? reason.message : "Ответ не сохранён"); }
      throw reason;
    } finally {
      if (pendingSaves.current.get(itemId)?.operation === operation) pendingSaves.current.delete(itemId);
      setSavingItem((current) => current === itemId ? null : current);
    }
  }

  function scheduleSave(itemId: string) {
    if (deadlineExpiredRef.current) return;
    dirtyItems.current.add(itemId);
    const previous = saveTimers.current.get(itemId);
    if (previous) clearTimeout(previous);
    const timer = setTimeout(() => { saveTimers.current.delete(itemId); void save(itemId).catch(() => undefined); }, 700);
    saveTimers.current.set(itemId, timer);
  }

  function setCodeConflict(itemId: string, content: string | null, comparisonReady=true) {
    invalidateCodeRun();
    const next = { ...localCodeConflictsRef.current };
    if (content === null) delete next[itemId]; else next[itemId] = content;
    localCodeConflictsRef.current = next;
    setLocalCodeConflicts(next);
    const ready={...codeConflictReadyRef.current};
    if(content===null)delete ready[itemId];else ready[itemId]=comparisonReady;
    codeConflictReadyRef.current=ready;setCodeConflictReady(ready);
  }

  async function saveCode(itemId: string, contentOverride?: string) {
    if (localCodeConflictsRef.current[itemId] !== undefined) throw new Error("Сначала выберите локальный или сохранённый черновик кода.");
    const timer = codeSaveTimers.current.get(itemId);
    if (timer) { clearTimeout(timer); codeSaveTimers.current.delete(itemId); }
    if (deadlineExpiredRef.current) { dirtyCodeItems.current.delete(itemId); return; }
    const workspace = workspacesRef.current[itemId];
    if (!workspace || workspace.read_only) return;
    const content = contentOverride ?? codeRef.current[itemId] ?? "";
    const pending = pendingCodeSaves.current.get(itemId);
    if (pending) {
      await pending.operation;
      if (!dirtyCodeItems.current.has(itemId)) return;
      if (pending.content === (codeRef.current[itemId] ?? "")) { dirtyCodeItems.current.delete(itemId); return; }
      return saveCode(itemId);
    }
    if (!dirtyCodeItems.current.has(itemId) && contentOverride === undefined) return;
    dirtyCodeItems.current.delete(itemId);
    setRunnerError(null);
    const operation = saveExamWorkspaceCode(sessionId, itemId, content, workspace.code.version);
    pendingCodeSaves.current.set(itemId, { content, operation });
    try {
      const next = await operation;
      const applied = acceptWorkspace(next, content);
      if (applied) {
        clearLocalDraft(examDraftScope(exam.id, itemId), "code");
      } else {
        dirtyCodeItems.current.add(itemId);
        scheduleCodeSave(itemId);
      }
    } catch (reason: unknown) {
      if (isExamTimeExpired(reason)) { markDeadlineExpired(); dirtyCodeItems.current.delete(itemId); }
      else if (reason instanceof ApiError && reason.code === "VERSION_CONFLICT") {
        const ownCode = codeRef.current[itemId] ?? content;
        setCodeConflict(itemId, ownCode, false);
        dirtyCodeItems.current.delete(itemId);
        try {
          const fresh = await getExamWorkspace(sessionId, itemId);
          workspacesRef.current = { ...workspacesRef.current, [itemId]: fresh };
          codeRef.current = { ...codeRef.current, [itemId]: fresh.code.content };
          setWorkspaces(current => ({ ...current, [itemId]: fresh }));
          setCodeByItem(current => ({ ...current, [itemId]: fresh.code.content }));
          setCodeConflict(itemId, ownCode, true);
        } catch { /* Keep the local copy and conflict gate until a reload can compare versions. */ }
        setRunnerError("Код изменён в другой вкладке. Ваш черновик сохранён отдельно — выберите версию.");
      } else { dirtyCodeItems.current.add(itemId); setRunnerError(reason instanceof Error ? reason.message : "Код не сохранён"); }
      throw reason;
    } finally {
      if (pendingCodeSaves.current.get(itemId)?.operation === operation) pendingCodeSaves.current.delete(itemId);
    }
  }

  function scheduleCodeSave(itemId: string) {
    if (deadlineExpiredRef.current) return;
    dirtyCodeItems.current.add(itemId);
    const previous = codeSaveTimers.current.get(itemId);
    if (previous) clearTimeout(previous);
    const timer = setTimeout(() => { codeSaveTimers.current.delete(itemId); void saveCode(itemId).catch(() => undefined); }, 700);
    codeSaveTimers.current.set(itemId, timer);
  }

  async function refreshConflict(itemId:string) {
    const local=localCodeConflictsRef.current[itemId];
    if(local===undefined||paused||pausing||submitting||confirmFinish||examExpired)return;
    setCodeConflict(itemId,local,false);setRunnerError(null);
    try {
      const fresh=await getExamWorkspace(sessionId,itemId);
      workspacesRef.current={...workspacesRef.current,[itemId]:fresh};
      codeRef.current={...codeRef.current,[itemId]:fresh.code.content};
      setWorkspaces(current=>({...current,[itemId]:fresh}));
      setCodeByItem(current=>({...current,[itemId]:fresh.code.content}));
      setCodeConflict(itemId,local,true);
    } catch {setRunnerError("Не удалось загрузить сохранённую версию. Ваш локальный код остаётся в отдельном черновике.");}
  }

  async function runCurrentCode() {
    if (codeRunBusy || paused || pausing || resuming || examExpired || submitting || confirmFinish || !currentItem || !currentWorkspace || currentWorkspace.read_only || localCodeConflictsRef.current[currentItem.id] !== undefined) return;
    const itemId = currentItem.id;
    const taskRevisionId = currentItem.task_revision_id;
    const content = codeRef.current[itemId] ?? currentWorkspace.code.content;
    if (!content.trim()) return;
    const requestId = ++codeRunRequestRef.current;
    setCodeRunBusy(true);
    setRunnerError(null);
    try {
      if (!canCommitCodeRun(requestId, itemId, taskRevisionId)) return;
      // A browser run must reference the server version that acknowledges the
      // exact source being executed. Saving first also turns a stale local
      // draft/version conflict into an actionable error before execution.
      await saveCode(itemId, content);
      if (!canCommitCodeRun(requestId, itemId, taskRevisionId)) return;
      const workspace = workspacesRef.current[itemId];
      const savedContent = codeRef.current[itemId] ?? workspace?.code.content ?? "";
      if (!workspace || workspace.item_id !== itemId || workspace.read_only || savedContent !== content || workspace.code.content !== content) {
        throw new Error("Код изменился при сохранении. Повторите запуск после проверки черновика.");
      }
      const savedVersion = workspace.code.version;
      const run = await devicePython.run(savedContent, savedVersion);
      if (!canCommitCodeRun(requestId, itemId, taskRevisionId)) return;
      const localRun = toLocalExamCodeRun(run, sessionId, itemId, taskRevisionId, savedVersion, devicePython.files);
      setCodeRunsByItem((current) => ({
        ...current,
        [itemId]: [localRun, ...(current[itemId] ?? []).filter((item) => item.id !== localRun.id)],
      }));
    } catch (reason: unknown) {
      if (canCommitCodeRun(requestId, itemId, taskRevisionId)) {
        setRunnerError(reason instanceof Error ? reason.message : "Код не выполнен");
      }
    } finally {
      if (codeRunRequestRef.current === requestId) setCodeRunBusy(false);
    }
  }

  async function persistBoard(document: BoardDocument): Promise<BoardDocument> {
    const itemId = currentItem?.id;
    const workspace = itemId ? workspacesRef.current[itemId] : null;
    if (!itemId || !workspace) throw new ApiError("WORKSPACE_NOT_READY","Рабочее место ещё не загружено.",409,{});
    if (deadlineExpiredRef.current) throw new ApiError("EXAM_TIME_EXPIRED","Время вышло. Локальный черновик сохранён отдельно.",409,{});
    if (workspace.read_only) throw new ApiError("EXAM_NOT_ACTIVE","Редактирование приостановлено. Локальный черновик сохранён отдельно.",409,{});
    try {
      const next = await saveExamWorkspaceBoard(sessionId, itemId, document);
      const applied = acceptWorkspace(next, undefined, workspace.code.content);
      if (!applied) {
        dirtyCodeItems.current.add(itemId);
        scheduleCodeSave(itemId);
      }
      return boardFromWorkspace(next);
    } catch (reason: unknown) {
      if (isExamTimeExpired(reason)) markDeadlineExpired();
      setRunnerError(reason instanceof Error ? reason.message : "Доска не сохранена");
      throw reason;
    }
  }


  const timerSnapshot: ExamTimerSnapshot = {
    status: exam.status,
    serverNow: exam.server_now ?? null,
    remainingSeconds: exam.remaining_seconds ?? null,
    deadlineAt: exam.deadline_at ?? null,
  };
  const timerSnapshotKey = examTimerSnapshotKey(timerSnapshot);
  const [clockMs, setClockMs] = useState(() => monotonicNow());
  const timerAnchorRef = useRef(createExamTimerAnchor(timerSnapshot, timerSnapshotKey, Date.now(), monotonicNow()));
  if (timerAnchorRef.current.key !== timerSnapshotKey) {
    timerAnchorRef.current = createExamTimerAnchor(timerSnapshot, timerSnapshotKey, Date.now(), monotonicNow());
  }
  useEffect(() => {
    setClockMs(monotonicNow());
    setServerExpired(false);
  }, [timerSnapshotKey]);
  useEffect(() => {
    if (paused || timerAnchorRef.current.monotonicDeadlineMs == null || serverExpired) return;
    const tick = () => setClockMs(monotonicNow());
    tick();
    const interval = window.setInterval(tick, 1000);
    return () => window.clearInterval(interval);
  }, [paused, serverExpired, timerSnapshotKey]);
  const remainingSeconds = remainingExamSeconds(timerSnapshot, timerAnchorRef.current, clockMs);
  const examExpired = serverExpired || remainingSeconds === 0;
  codeRunLifecycleRef.current = {
    examKey,
    itemId: currentItem?.id ?? null,
    taskRevisionId: currentItem?.task_revision_id ?? "",
    paused,
    pausing,
    resuming,
    submitting,
    confirmFinish,
    serverExpired: serverExpired || deadlineExpiredRef.current || examExpired,
  };

  function canCommitCodeRun(requestId: number, itemId: string, taskRevisionId: string): boolean {
    const lifecycle = codeRunLifecycleRef.current;
    return requestId === codeRunRequestRef.current
      && lifecycle.examKey === examKey
      && lifecycle.itemId === itemId
      && lifecycle.taskRevisionId === taskRevisionId
      && !lifecycle.paused
      && !lifecycle.pausing
      && !lifecycle.resuming
      && !lifecycle.submitting
      && !lifecycle.confirmFinish
      && !lifecycle.serverExpired
      && !deadlineExpiredRef.current
      && currentItemIdRef.current === itemId
      && localCodeConflictsRef.current[itemId] === undefined;
  }

  function clearSaveTimers() {
    for (const timer of saveTimers.current.values()) clearTimeout(timer);
    for (const timer of codeSaveTimers.current.values()) clearTimeout(timer);
    saveTimers.current.clear();
    codeSaveTimers.current.clear();
  }
  function invalidateCodeRun() {
    codeRunRequestRef.current += 1;
    setCodeRunBusy(false);
  }
  function markDeadlineExpired() { deadlineExpiredRef.current = true; invalidateCodeRun(); clearSaveTimers(); dirtyItems.current.clear(); dirtyCodeItems.current.clear(); setServerExpired(true); }
  useEffect(() => { if (remainingSeconds === 0 && !paused) markDeadlineExpired(); }, [remainingSeconds, paused]);

  async function flushDirtyAnswers() {
    if (!sessionId) return;
    if(!deadlineExpiredRef.current && Object.keys(localCodeConflictsRef.current).length)throw new Error("Сначала сравните и выберите черновик кода. Обе версии сохранены отдельно.");
    const answerResults = deadlineExpiredRef.current ? [] : await Promise.allSettled([...dirtyItems.current].map((itemId) => save(itemId)));
    const codeResults = deadlineExpiredRef.current ? [] : await Promise.allSettled([...dirtyCodeItems.current].map((itemId) => saveCode(itemId)));
    const pendingAnswerResults = await Promise.allSettled([
      ...[...pendingSaves.current.values()].map(({ operation }) => operation),
    ]);
    const pendingCodeResults = await Promise.allSettled([
      ...[...pendingCodeSaves.current.values()].map(({ operation }) => operation),
    ]);
    const pendingResults = [...pendingAnswerResults, ...pendingCodeResults];
    const results = [...answerResults, ...codeResults, ...pendingResults];
    if (results.some((result) => result.status === "rejected" && isExamTimeExpired(result.reason))) markDeadlineExpired();
    const failure = results.find((result) => result.status === "rejected" && !isExamTimeExpired(result.reason));
    if (failure?.status === "rejected") throw failure.reason;
  }
  flushDirtyAnswersRef.current = flushDirtyAnswers;

  async function askStudyAi() {
    if (!isStudy || studyAiBusy || paused || pausing || resuming || examExpired || submitting || confirmFinish || !currentItem || !currentWorkspace || localCodeConflictsRef.current[currentItem.id] !== undefined) return;
    const message = studyAiQuestion.trim();
    if (!message) return;
    const itemId = currentItem.id;
    const scope=examDraftScope(exam.id,itemId);
    setStudyAiBusy(true);
    setStudyAiError(null);
    shell.showPanel("assistant");
    try {
      await flushDirtyAnswers();
      await flushOpenDrafts();
      const latest = await getExamWorkspace(sessionId, itemId);
      acceptWorkspace(latest, undefined, currentWorkspace.code.content);
      const preferences=getPreferences();
      const model:Parameters<typeof startExamStudyAi>[2]["model"]=preferences.model==="luna"?"gpt-5.6-luna":preferences.model==="terra"?"gpt-5.6-terra":preferences.model==="sol"?"gpt-5.6-sol":"auto";
      const request={message,workspace_version:latest.code.version,model,effort:preferences.effort,tutor_mode:preferences.tutorMode==="compare"||preferences.tutorMode==="full_solution"?"review" as const:preferences.tutorMode};
      const requestKey=studyRequestKey(scope,JSON.stringify([request,latest.board.version]));
      const run = await startExamStudyAi(sessionId, itemId, request, requestKey);
      saveStudyCheckpoint(scope,{runId:run.id,question:message});clearStudyRequestKey(scope);
      if (currentItemIdRef.current !== itemId) return;
      setSentStudyQuestion(message);
      if(!["unavailable","failed","cancelled","timed_out"].includes(run.status)){clearExamStudyAiDraft(scope);setStudyAiQuestion("");}
      setStudyAiRun(run);
    } catch (reason: unknown) {
      if(currentItemIdRef.current!==itemId)return;
      if (reason instanceof ApiError && reason.code === "VERSION_CONFLICT") {
        // Preserve the question draft so the user can refresh the server
        // workspace and retry without losing the request.
        saveExamStudyAiDraft(examDraftScope(exam.id, itemId), message);
        setStudyAiError("Код изменился на сервере. Вопрос сохранён — обновите черновик и повторите.");
      } else if (reason instanceof ApiError && (reason.code === "EXAM_SEALED" || reason.code === "EXAM_NOT_ACTIVE" || reason.code === "EXAM_TIME_EXPIRED")) {
        setStudyAiError("Помощник недоступен в текущем состоянии варианта.");
      } else if (reason instanceof ApiError && ["AI_UNAVAILABLE", "CODEX_UNAVAILABLE", "AGENT_PROTOCOL_ERROR"].includes(reason.code)) {
        setStudyAiError("Помощник сейчас недоступен. Сохранённые черновики варианта не изменены.");
      } else {
        setStudyAiError(reason instanceof Error ? reason.message : "Помощник временно недоступен.");
      }
    } finally {
      setStudyAiBusy(false);
    }
  }

  async function cancelStudyAi() {
    if (paused || pausing || resuming || submitting || confirmFinish || examExpired || !studyAiRun || !currentItem || !sessionId || !["queued", "running", "cancelling"].includes(studyAiRun.status)) return;
    try {
      const next = await cancelExamStudyAi(sessionId, currentItem.id, studyAiRun.id);
      if(currentItemIdRef.current===next.item_id)setStudyAiRun(next);
    } catch (reason: unknown) {
      setStudyAiError(reason instanceof Error ? reason.message : "Не удалось остановить запрос помощника.");
    }
  }

  useEffect(() => {
    if (!studyAiRun || !currentItem || !sessionId || ["succeeded", "failed", "timed_out", "cancelled", "unavailable"].includes(studyAiRun.status)) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await getExamStudyAi(sessionId, currentItem.id, studyAiRun.id);
        if (cancelled || currentItemIdRef.current !== next.item_id) return;
        setStudyAiRun(next);
        if (!["succeeded", "failed", "timed_out", "cancelled", "unavailable"].includes(next.status)) {
          studyAiPollTimer.current = setTimeout(() => void poll(), 1000);
        }
      } catch (reason: unknown) {
        if (!cancelled) setStudyAiError(reason instanceof Error ? reason.message : "Не удалось обновить ответ помощника.");
      }
    };
    studyAiPollTimer.current = setTimeout(() => void poll(), 500);
    return () => {
      cancelled = true;
      if (studyAiPollTimer.current) clearTimeout(studyAiPollTimer.current);
      studyAiPollTimer.current = null;
    };
  }, [currentItem?.id, sessionId, studyAiRun?.id, studyAiRun?.status]);

  async function moveTo(nextIndex: number) {
    if (confirmFinish || nextIndex < 0 || nextIndex >= items.length || nextIndex === currentIndex) return;
    invalidateCodeRun();
    try {
      if (items[currentIndex]) {
        await save(items[currentIndex].id);
        await saveCode(items[currentIndex].id);
      }
      setCurrentIndex(nextIndex);
    } catch { /* save() exposes the actionable error in the runner. */ }
  }

  async function saveAndPause() {
    if (!canPause || pausing || paused || resuming || submitting || confirmFinish || examExpired || !sessionId) return;
    invalidateCodeRun();
    setPausing(true); setRunnerError(null);
    try { await flushDirtyAnswers(); await flushOpenDrafts(); onExamUpdated(await pauseExam(sessionId)); }
    catch (reason: unknown) { setRunnerError(reason instanceof Error ? reason.message : "Не удалось сохранить вариант"); }
    finally { setPausing(false); }
  }

  async function continueExam() {
    if (resuming || !paused || !sessionId) return;
    setResuming(true); setRunnerError(null);
    try {
      onExamUpdated(await resumeExam(sessionId));
      for (const itemId of dirtyItems.current) scheduleSave(itemId);
      for (const itemId of dirtyCodeItems.current) scheduleCodeSave(itemId);
    }
    catch (reason: unknown) { setRunnerError(reason instanceof Error ? reason.message : "Не удалось продолжить вариант"); }
    finally { setResuming(false); }
  }

  async function submit() {
    if (paused || pausing || resuming || submitting) return;
    invalidateCodeRun();
    setSubmitting(true); setRunnerError(null); setConfirmFinish(false);
    try { await flushDirtyAnswers(); await flushOpenDrafts(deadlineExpiredRef.current?{pendingOnly:true}:undefined); onReport(await submitExam(sessionId)); }
    catch (reason: unknown) { setRunnerError(reason instanceof Error ? reason.message : "Не удалось отправить вариант"); }
    finally { setSubmitting(false); }
  }

  async function finalizeAtDeadline() {
    if (paused || !sessionId) return;
    invalidateCodeRun();
    setSubmitting(true); setRunnerError(null);
    try {
      // Dirty local drafts are deliberately not forced past the server
      // deadline; pending requests are still awaited before finalization.
      await flushDirtyAnswers();
      try {await flushOpenDrafts({pendingOnly:true});}catch(reason){if(!isExamTimeExpired(reason))throw reason;}
      onReport(await submitExam(sessionId));
    } catch (reason: unknown) {
      setRunnerError(reason instanceof Error ? reason.message : "Не удалось автоматически завершить вариант");
    } finally {
      setSubmitting(false);
    }
  }

  useEffect(() => {
    if (remainingSeconds !== 0 || paused || pausing || resuming || submitting || !sessionId || deadlineFinalizedRef.current) return;
    deadlineFinalizedRef.current = true;
    markDeadlineExpired();
    void finalizeAtDeadline();
  }, [remainingSeconds, paused, pausing, resuming, submitting, sessionId]);

  const currentAssets = currentItem ? assetsByItem[currentItem.id] ?? currentItem.assets ?? [] : [];
  const deviceRevisionHash = currentItem?.task_revision_id ?? "";
  const deviceRunScope = currentItem ? browserExamRunScope(exam.id, sessionId, currentItem.id) : `exam:${examKey}:empty`;
  const devicePython = useDevicePython({
    assets: currentAssets,
    lineNumber: currentItem?.line_number,
    revisionHash: deviceRevisionHash,
    scope: deviceRunScope,
    enabled: Boolean(currentItem && !paused && !pausing && !resuming && !submitting && !confirmFinish && !serverExpired),
  });
  const currentSurface = currentItem ? surfaceByItem[currentItem.id] ?? (boardFirst.has(currentItem.line_number ?? -1) ? "board" : "code") : "code";
  const currentCode = currentItem && currentWorkspace ? codeByItem[currentItem.id] ?? currentWorkspace.code.content : "";
  const workspaceReadOnly = Boolean(report) || submitting || paused || pausing || resuming || examExpired || currentWorkspace?.read_only === true;
  const boardReadOnly=Boolean(report)||paused||examExpired||currentWorkspace?.read_only===true;
  const filledCount = items.filter((item) => Boolean((answers[item.id] ?? "").trim())).length;

  useShortcuts(currentSurface === "code" ? "editor" : "exam", {
    "surface.toggle": () => {
      if (!confirmFinish && !workspaceReadOnly && currentItem) {
        setSurfaceByItem((current) => ({ ...current, [currentItem.id]: currentSurface === "code" ? "board" : "code" }));
      }
    },
    "code.run": currentSurface === "code" && currentCode.trim() && !workspaceReadOnly && !confirmFinish ? () => void runCurrentCode() : undefined,
    "assistant.toggle": isStudy && !confirmFinish ? () => {
      if (shell.panelVisible && shell.panelMode === "assistant") shell.hidePanel();
      else shell.showPanel("assistant");
    } : undefined,
    "task.previous": !workspaceReadOnly && !confirmFinish && !pausing ? () => void moveTo(currentIndex - 1) : undefined,
    "task.next": !workspaceReadOnly && !confirmFinish && !pausing ? () => void moveTo(currentIndex + 1) : undefined,
  });

  useEffect(()=>{
    if(!positionsOpen)return;
    const close=(event:Event)=>{if(!positionsMenuRef.current?.contains(event.target as Node)&&!positionsButtonRef.current?.contains(event.target as Node))setPositionsOpen(false);};
    const key=(event:KeyboardEvent)=>{if(event.key==="Escape"){event.preventDefault();setPositionsOpen(false);positionsButtonRef.current?.focus();}};
    document.addEventListener("pointerdown",close);document.addEventListener("keydown",key,true);
    return()=>{document.removeEventListener("pointerdown",close);document.removeEventListener("keydown",key,true);};
  },[positionsOpen]);
  if (!sessionId) return null;
  return <>{shell.toolbarHost?createPortal(<div className="exam-workspace-toolbar">
    <a className="outline-button exam-catalog-link" href="/tasks">Каталог</a>
    <button ref={positionsButtonRef} className="outline-button" aria-expanded={positionsOpen} aria-controls="exam-positions-menu" disabled={confirmFinish||submitting||paused||pausing} onClick={event=>{const rect=event.currentTarget.getBoundingClientRect();setPositionsPoint({left:Math.max(8,Math.min(rect.left,window.innerWidth-276)),top:rect.bottom+8});setPositionsOpen(value=>!value);}} type="button">Задания · {currentIndex+1}/{items.length}</button>
    <span className="exam-toolbar-spacer"/>
    <div className={examExpired?"exam-timer is-expired":"exam-timer"} role="timer" aria-live="off"><strong>{formatCountdown(examExpired?0:remainingSeconds)}</strong></div>
    {isStudy&&<button className="outline-button exam-ai-toggle" disabled={confirmFinish} onClick={()=>shell.panelVisible&&shell.panelMode==="assistant"?shell.hidePanel():shell.showPanel("assistant")} type="button">Помощник</button>}
  </div>,shell.toolbarHost):null}<section ref={sectionRef} className={`${styles.runner} exam-runner open-section`} aria-labelledby="exam-runner-heading">
    <div className={styles.runnerHeader}><div><span className="eyebrow">{paused ? "СОХРАНЁННЫЙ ЧЕРНОВИК" : isStudy ? "УЧЕБНЫЙ ВАРИАНТ" : "ЗАПЕЧАТАННЫЙ ВАРИАНТ"}</span><h2 id="exam-runner-heading">{paused ? "Вариант на паузе" : isStudy ? "Решение учебного варианта" : "Решение варианта"}</h2></div><div className={styles.runnerHeaderActions}>{isStudy ? <button className="outline-button" disabled={confirmFinish} onClick={() => shell.showPanel("assistant")} type="button">Помощник</button> : null}<div className={examExpired ? "exam-timer is-expired" : "exam-timer"} role="timer" aria-live="off"><span>{examExpired ? "Время вышло" : "Осталось"}</span><strong>{formatCountdown(examExpired ? 0 : remainingSeconds)}</strong>{exam.deadline_at ? <small>до {formatDeadline(exam.deadline_at)}</small> : null}</div></div></div>
    <p className={styles.sealedNote}><Icon name={isStudy ? "info" : "lock"} size={13} />{isStudy ? "Учебный режим сохраняет отдельные черновики кода и доски по каждому заданию; подсказки и внешний контекст не добавляются автоматически." : "Пока вариант не сдан, помощник, образцы кода и ответы недоступны — это режим самостоятельной тренировки."}</p>
    {runnerError ? <div className="inline-error" role="alert"><Icon name="warning" size={14} />{runnerError}</div> : null}
    {devicePython.fileError ? <div className="inline-error" role="alert"><Icon name="warning" size={14} />{devicePython.fileError}<button className="tiny-action" onClick={devicePython.retryFiles} type="button">Повторить</button></div> : null}
    {workspaceError ? <div className="inline-error" role="alert"><Icon name="warning" size={14} />{workspaceError}</div> : null}
    {examExpired ? <div className="exam-expired-note" role="status"><Icon name="lock" size={14} />Время вышло. Новые ответы не принимаются; отправьте уже сохранённые ответы.</div> : null}
    <div className={styles.runnerLayout}>
      <nav ref={positionsMenuRef} hidden={!positionsOpen} id="exam-positions-menu" style={positionsPoint} className={styles.questionNav} aria-label="Номера заданий"><strong>Задания</strong><div className={styles.questionGrid}>{items.map((item, index) => { const hasAnswer = Boolean((answers[item.id] ?? "").trim()); const hasDraft = !hasAnswer && (Boolean((codeByItem[item.id] ?? workspaces[item.id]?.code.content ?? "").trim()) || hasBoardDraft(workspaces[item.id])); const flagged = Boolean(flaggedByItem[item.id]); const sourcePosition = item.position + 1; return <button disabled={paused || pausing || submitting || confirmFinish} data-source-position={sourcePosition} aria-current={index === currentIndex ? "step" : undefined} aria-label={`${hasAnswer ? "Ответ введён, " : hasDraft ? "Есть черновик, " : ""}${flagged ? "Отмечено, " : ""}задание ${index + 1}`} className={`${styles.questionButton} ${index === currentIndex ? styles.selected : ""} ${hasAnswer ? styles.filled : ""} ${hasDraft ? styles.draft : ""} ${flagged ? styles.flagged : ""}`} onClick={()=>{setPositionsOpen(false);void moveTo(index);}} type="button" key={item.id}>{index + 1}</button>; })}</div><span className={styles.navLegend}><i className={styles.legendFilled} />Ответ введён</span><span className={styles.navLegend}><i className={styles.legendDraft} />Есть черновик</span><span className={styles.navLegend}><i className={styles.legendFlagged} />Отмечено</span></nav>
      <div className={styles.questionPane}>
        {currentItem ? <><div className={styles.questionHeading}><span className={styles.positionBadge}>Позиция {currentIndex + 1} из {items.length}</span>{currentItem.source_id ? <strong>КЕГЭ № {currentItem.source_id}</strong> : null}{currentItem.difficulty ? <span className={styles.difficultyBadge}>{difficultyLabel(currentItem.difficulty)}</span> : null}<span className="muted-inline">{currentItem.line_number != null ? `Линия ${currentItem.line_number}` : "Номер КЕГЭ"}</span><button aria-pressed={Boolean(flaggedByItem[currentItem.id])} className={styles.flagButton} disabled={confirmFinish || paused || submitting || examExpired} onClick={() => setFlaggedByItem((current) => ({ ...current, [currentItem.id]: !current[currentItem.id] }))} type="button">{flaggedByItem[currentItem.id] ? "Снять отметку" : "Отметить"}</button></div><h3>{(DEMO_MODE ? statementToText(currentItem.statement) : currentItem.statement)}</h3><TaskImages assets={currentAssets} canPrepare={assetFetchAvailable} onAssetPrepared={(prepared) => setAssetsByItem((current) => ({ ...current, [currentItem.id]: (current[currentItem.id] ?? []).map((asset) => asset.id === prepared.id ? { ...asset, ...prepared } : asset) }))} />{currentAssets.some((asset) => !isImageAsset(asset)) ? <div className={styles.fileList}>{currentAssets.filter((asset) => !isImageAsset(asset)).map((asset) => <span className={styles.deviceFileItem} key={asset.id}><DeviceFileRow disabled={workspaceReadOnly || confirmFinish} loading={devicePython.filesLoading} name={asset.name} sizeBytes={asset.size_bytes} onDownload={() => devicePython.download(asset)} />{currentItem.line_number === 9 && isSpreadsheetName(asset.name) ? <TaskSpreadsheetFiles asset={asset} disabled={workspaceReadOnly || confirmFinish} revisionHash={deviceRevisionHash} /> : null}</span>)}</div> : null}<label className={styles.answerField}><span>Ваш ответ</span><textarea aria-label={`Ответ на задачу ${currentIndex + 1}`} disabled={Boolean(report) || submitting || examExpired || paused || pausing} onBlur={() => void save(currentItem.id).catch(() => undefined)} onChange={(event) => { const next = { ...answersRef.current, [currentItem.id]: event.target.value }; answersRef.current = next; setAnswers(next); saveLocalDraft(examDraftScope(exam.id, currentItem.id), "answer", 0, event.target.value); scheduleSave(currentItem.id); }} placeholder="Введите ответ…" value={answers[currentItem.id] ?? ""} /></label><div className={styles.answerStatus} role="status">{paused ? "Вариант сохранён. Нажмите «Продолжить», чтобы редактировать." : null}</div></> : <p className="muted-copy">Позиции варианта появятся после загрузки варианта. Помощник остаётся отключён.</p>}
      </div>
      <ExamWorkspacePane
        boardReadOnly={boardReadOnly}
        conflictReady={Boolean(currentItem&&codeConflictReady[currentItem.id])}
        code={currentCode}
        codeRunBusy={codeRunBusy}
        confirmFinish={confirmFinish}
        currentSurface={currentSurface}
        deviceFileError={devicePython.fileError}
        deviceFilesLoading={devicePython.filesLoading}
        deviceRunning={devicePython.running}
        draftScope={currentItem ? examDraftScope(exam.id, currentItem.id) : ""}
        itemId={currentItem?.id ?? ""}
        localConflict={currentItem ? localCodeConflicts[currentItem.id] ?? null : null}
        onBlur={() => currentItem ? void saveCode(currentItem.id).catch(() => undefined) : undefined}
        onCodeChange={(value) => {
          if (!currentItem || !currentWorkspace || workspaceReadOnly || confirmFinish || localCodeConflictsRef.current[currentItem.id] !== undefined) return;
          const next = { ...codeRef.current, [currentItem.id]: value };
          codeRef.current = next;
          setCodeByItem(next);
          saveLocalDraft(examDraftScope(exam.id, currentItem.id), "code", currentWorkspace.code.version, value);
          scheduleCodeSave(currentItem.id);
        }}
        onDiscardLocal={() => {
          if (!currentItem || !currentWorkspace || workspaceReadOnly || !codeConflictReadyRef.current[currentItem.id]) return;
          acceptDraftHandoff("exam:"+sessionId,examDraftKey());
          clearLocalDraft(examDraftScope(exam.id, currentItem.id), "code");
          dirtyCodeItems.current.delete(currentItem.id);
          setCodeConflict(currentItem.id, null);
          codeRef.current = { ...codeRef.current, [currentItem.id]: currentWorkspace.code.content };
          setCodeByItem(current => ({ ...current, [currentItem.id]: currentWorkspace.code.content }));
          setRunnerError(null);
        }}
        onRefreshConflict={()=>{if(currentItem)void refreshConflict(currentItem.id);}}
        onRun={() => void runCurrentCode()}
        onStop={devicePython.stop}
        onRestoreLocal={() => {
          if (!currentItem || !currentWorkspace || workspaceReadOnly || !codeConflictReadyRef.current[currentItem.id]) return;
          const localCode = localCodeConflicts[currentItem.id];
          if (localCode == null) return;
          codeRef.current = { ...codeRef.current, [currentItem.id]: localCode };
          setCodeByItem((current) => ({ ...current, [currentItem.id]: localCode }));
          saveLocalDraft(examDraftScope(exam.id, currentItem.id), "code", currentWorkspace.code.version, localCode);
          setCodeConflict(currentItem.id, null);
          dirtyCodeItems.current.add(currentItem.id);
          scheduleCodeSave(currentItem.id);
        }}
        onSelectSurface={(surface) => currentItem && setSurfaceByItem((current) => ({ ...current, [currentItem.id]: surface }))}
        persistBoard={persistBoard}
        runs={currentItem ? codeRunsByItem[currentItem.id] ?? [] : []}
        sessionId={sessionId}
        workspace={currentWorkspace}
        workspaceLoading={workspaceLoading}
        workspaceReadOnly={workspaceReadOnly}
      />
    </div>
    <Modal open={confirmFinish} onClose={() => { if (!submitting) setConfirmFinish(false); }} title="Завершить вариант?">
      <p>Введено ответов: {filledCount} из {items.length}. После подтверждения черновики станут доступны только в истории.</p>
      <div className="kontur-modal-actions">
        <button className="outline-button" disabled={submitting} onClick={() => setConfirmFinish(false)} type="button">Вернуться</button>
        <button className="primary-button" disabled={submitting} onClick={() => void submit()} type="button">Подтвердить сдачу</button>
      </div>
    </Modal>
    <div className={styles.runnerFooter}><button className="outline-button" disabled={currentIndex === 0 || !items.length || paused || submitting || pausing || confirmFinish} onClick={() => void moveTo(currentIndex - 1)} type="button">Назад</button><button className="outline-button" disabled={currentIndex >= items.length - 1 || !items.length || paused || submitting || pausing || confirmFinish} onClick={() => void moveTo(currentIndex + 1)} type="button">Дальше</button><button className="outline-button" disabled={currentIndex >= items.length - 1 || !items.length || paused || submitting || pausing || confirmFinish} onClick={() => void moveTo(items.length - 1)} type="button">Последняя</button><span className={styles.footerSpacer} />{paused ? <button className="primary-button" disabled={resuming || confirmFinish} onClick={() => void continueExam()} type="button">{resuming ? <><span className="button-spinner" />Продолжаем…</> : "Продолжить вариант"}</button> : canPause ? <button className="outline-button" disabled={pausing || submitting || confirmFinish} onClick={() => void saveAndPause()} type="button">{pausing ? <><span className="button-spinner" />Сохраняем…</> : "Пауза"}</button> : <span className={styles.noPauseNote}><Icon name="lock" size={12} />Строгий режим: пауза закрыта</span>}<button className="primary-button" disabled={submitting || paused || pausing || resuming || !items.length} onClick={() => { invalidateCodeRun(); setConfirmFinish(true); }} type="button">{submitting ? <><span className="button-spinner" />Отправляем…</> : "Сдать вариант"}</button></div>
  </section>
  {isStudy && shell.panelHost ? createPortal(<StudyAiPanel
    busy={studyAiBusy}
    error={studyAiError}
    expired={examExpired}
    message={studyAiQuestion}
    sentQuestion={sentStudyQuestion}
    taskLabel={currentItem?.source_id ? `КЕГЭ № ${currentItem.source_id}` : "Позиция "+(currentIndex+1)}
    onCancel={() => void cancelStudyAi()}
    onChange={(value) => { setStudyAiQuestion(value); saveExamStudyAiDraft(studyAiScope, value); }}
    onSubmit={() => void askStudyAi()}
    paused={paused || pausing || resuming || submitting || confirmFinish}
    run={studyAiRun}
    workspaceReady={Boolean(currentWorkspace) && (!currentItem || localCodeConflicts[currentItem.id] === undefined)}
  />, shell.panelHost) : null}
  </>;
}

function StudyAiPanel({ busy, error, expired, message,sentQuestion,taskLabel, onCancel, onChange, onSubmit, paused, run, workspaceReady }: {
  busy: boolean;
  error: string | null;
  expired: boolean;
  message: string;
  sentQuestion:string;
  taskLabel:string;
  onCancel: () => void;
  onChange: (value: string) => void;
  onSubmit: () => void;
  paused: boolean;
  run: ExamStudyAiRun | null;
  workspaceReady: boolean;
}) {
  const shell=useKonturShell();
  const active = run && ["queued", "running", "cancelling"].includes(run.status);
  const terminalText = run?.status === "unavailable" ? "Помощник сейчас недоступен." : run?.status === "failed" || run?.status === "timed_out" ? "Ответ не получен. Попробуйте ещё раз позже." : run?.status === "cancelled" ? "Запрос остановлен." : null;
  return <aside className={styles.studyAiPanel} aria-label="Помощник учебного варианта">
    <div className={styles.studyAiHeader}><div><h2>Помощник</h2></div><button className="icon-button" aria-label="К чатам и проектам" onClick={()=>shell.showPanel("navigation")} type="button"><Icon name="folder" size={18}/></button><button className="icon-button" aria-label="Закрыть помощника" onClick={shell.hidePanel} type="button"><Icon name="close" size={18}/></button>{active ? <button className="outline-button" disabled={paused||expired||busy} onClick={onCancel} type="button">Остановить</button> : null}</div>
    <p className={styles.studyAiNote}>Задание {taskLabel} · код и доска учитываются при отправке.</p>
    {sentQuestion&&<div className={styles.studyAiQuestion}><strong>Вы · контекст этого вопроса</strong><p>{sentQuestion}</p></div>}
    <label className={styles.studyAiField}><span>Вопрос по текущему заданию</span><textarea aria-label="Вопрос по текущему заданию" disabled={Boolean(active) || busy || paused || expired} maxLength={20_000} onChange={(event) => onChange(event.target.value)} placeholder="Например: подскажи, с чего начать проверку алгоритма…" value={message} /></label>
    <button className="primary-button" disabled={busy || Boolean(active) || paused || expired || !workspaceReady || !message.trim()} onClick={onSubmit} type="button">{busy ? <><span className="button-spinner" />Отправляем…</> : "Спросить ИИ"}</button>
    {paused || expired ? <p className={styles.studyAiState}>Помощник доступен только пока учебный вариант активен.</p> : null}
    {error ? <p className="inline-error" role="alert">{error}</p> : null}
    {active ? <p className={styles.studyAiState} role="status">Ответ готовится…</p> : null}
    {run?.content ? <div className={styles.studyAiOutput}><strong>Ответ помощника</strong><p>{run.content}</p></div> : null}
    {terminalText ? <p className={styles.studyAiState}>{terminalText}</p> : null}
  </aside>;
}

function ExamWorkspacePane({
  boardReadOnly,
  conflictReady,
  code,
  codeRunBusy,
  confirmFinish,
  currentSurface,
  deviceFileError,
  deviceFilesLoading,
  deviceRunning,
  draftScope,
  itemId,
  localConflict,
  onBlur,
  onCodeChange,
  onDiscardLocal,
  onRefreshConflict,
  onRun,
  onStop,
  onRestoreLocal,
  onSelectSurface,
  persistBoard,
  runs,
  sessionId,
  workspace,
  workspaceLoading,
  workspaceReadOnly,
}: {
  boardReadOnly: boolean;
  conflictReady:boolean;
  code: string;
  codeRunBusy: boolean;
  confirmFinish: boolean;
  currentSurface: "code" | "board";
  deviceFileError: string | null;
  deviceFilesLoading: boolean;
  deviceRunning: boolean;
  draftScope: string;
  itemId: string;
  localConflict: string | null;
  onBlur: () => void;
  onCodeChange: (value: string) => void;
  onDiscardLocal: () => void;
  onRefreshConflict:()=>void;
  onRun: () => void;
  onStop: () => void;
  onRestoreLocal: () => void;
  onSelectSurface: (surface: "code" | "board") => void;
  persistBoard: (document: BoardDocument) => Promise<BoardDocument>;
  runs: ExamCodeRun[];
  sessionId: string;
  workspace: ExamWorkspace | null;
  workspaceLoading: boolean;
  workspaceReadOnly: boolean;
}) {
  return <div className={styles.solutionPane} aria-label="Рабочее место задания">
    <div className={styles.solutionHeader}>
      <span className={styles.solutionTitle}>Решение</span>
      <div className={styles.solutionTabs} role="tablist" aria-label="Инструмент решения">
        <button aria-selected={currentSurface === "code"} className={currentSurface === "code" ? styles.solutionTabActive : styles.solutionTab} disabled={confirmFinish || workspaceReadOnly} onClick={() => onSelectSurface("code")} role="tab" type="button">Код</button>
        <button aria-selected={currentSurface === "board"} className={currentSurface === "board" ? styles.solutionTabActive : styles.solutionTab} disabled={confirmFinish || workspaceReadOnly} onClick={() => onSelectSurface("board")} role="tab" type="button">Доска</button>
      </div>
    </div>
    {workspaceLoading ? <div className={styles.workspaceLoading} role="status"><span className="button-spinner" />Загружаем рабочее место…</div> : workspace ? currentSurface === "board" ? <InfiniteBoard active={!workspaceReadOnly && !confirmFinish} disabled={boardReadOnly} draftScope={draftScope} initialBoard={boardFromWorkspace(workspace)} key={`${sessionId}:${itemId}`} persist={persistBoard} sessionId={`exam:${sessionId}:${itemId}`} /> : <>
      {localConflict !== null ? <div className={styles.workspaceConflict} role="alert"><span>Локальный черновик отличается от сохранённой версии.</span><button className="tiny-action" disabled={workspaceReadOnly || confirmFinish || !conflictReady} onClick={onRestoreLocal} type="button">Восстановить локальный</button><button className="tiny-action" disabled={workspaceReadOnly || confirmFinish || !conflictReady} onClick={onDiscardLocal} type="button">Оставить сохранённый</button>{!conflictReady&&<button className="tiny-action" disabled={workspaceReadOnly || confirmFinish} onClick={onRefreshConflict} type="button">Повторить загрузку</button>}</div> : null}
      <div className={styles.codeRunToolbar}>
        <span className={styles.workspaceNote}>Выполнение вашей программы</span>
        <div className={styles.codeRunActions}>
          <button className="outline-button" disabled={Boolean(deviceFileError) || deviceFilesLoading || deviceRunning || codeRunBusy || workspaceReadOnly || confirmFinish || localConflict !== null || !code.trim()} title={deviceFileError ?? "Запустить вашу программу в браузере"} onClick={onRun} type="button">{codeRunBusy || deviceRunning ? <><span className="button-spinner" />Выполняем…</> : deviceFilesLoading ? "Готовим файлы…" : "Запустить код"}</button>
          {deviceRunning ? <button className="outline-button" onClick={onStop} type="button">Остановить</button> : null}
        </div>
      </div>
      <CodeEditor draftKey={`exam:${sessionId}:${itemId}`} onBlur={onBlur} onChange={onCodeChange} readOnly={workspaceReadOnly || confirmFinish || localConflict !== null} value={code} />
      <ExamCodeRunOutput runs={runs} stale={Boolean(runs[0])&&(runs[0].workspace_version!==workspace.code.version||code!==workspace.code.content)}/>
    </> : <div className={styles.workspaceLoading}>Рабочее место загружается…</div>}
    {workspaceReadOnly ? <span className={styles.workspaceReadOnly} role="status">Редактирование приостановлено.</span> : null}
  </div>;
}

function ExamCodeRunOutput({ runs,stale=false }: { runs: ExamCodeRun[];stale?:boolean }) {
  const latest = runs[0];
  if (!latest) return <div className="output-panel"><div className="output-header">Вывод</div><p className="output-empty">Здесь появится вывод вашей программы.</p></div>;
  const local = isBrowserExamRun(latest);
  const previous = runs.slice(1);
  const localHistory = previous.filter(isBrowserExamRun);
  const serverHistory = previous.filter((run) => !isBrowserExamRun(run));
  return <div className={styles.codeRunOutput}>
    <div className={styles.codeRunHeading}><strong>{stale ? "Вывод · предыдущий запуск" : local ? "Вывод · в браузере" : codeRunStatusLabel(latest.status)}</strong><span>{latest.completed_at ? formatRunTime(latest.completed_at) : "Выполняется"}</span></div>
    {local ? <p className={styles.workspaceNote}>Вывод программы не является проверкой ответа.</p> : null}
    {latest.stdout ? <pre aria-label="Вывод программы">{latest.stdout}</pre> : null}
    {latest.stderr ? <pre className={styles.codeRunError} aria-label="Сообщение проверки">{latest.stderr}</pre> : null}
    {!latest.stdout && !latest.stderr ? <p className={styles.workspaceNote}>{local ? "Программа не вывела текст." : "Сервер не вернул текстовый вывод."}</p> : null}
    {localHistory.length ? <details className={styles.codeRunHistory}><summary>Локальная история browser Python <span>{localHistory.length}</span></summary><ol>{localHistory.map((run) => <li key={run.id}>{codeRunStatusLabel(run.status)} · {run.completed_at ? formatRunTime(run.completed_at) : "без времени"}</li>)}</ol></details> : null}
    {serverHistory.length ? <details className={styles.codeRunHistory}><summary>История серверных проверок <span>{serverHistory.length}</span></summary><ol>{serverHistory.map((run) => <li key={run.id}>{codeRunStatusLabel(run.status)} · {run.completed_at ? formatRunTime(run.completed_at) : "без времени"}</li>)}</ol></details> : null}
  </div>;
}

function boardFromWorkspace(workspace: ExamWorkspace): BoardDocument {
  return boardFromSnapshot(
    workspace.board.document_id ?? `exam-board-${workspace.exam_id}-${workspace.item_id}`,
    workspace.board.version,
    workspace.board.snapshot,
    workspace.board.updated_at,
  );
}

function boardFromSnapshot(id: string, version: number, snapshot: Record<string, unknown>, updatedAt: string | null = null): BoardDocument {
  const viewport = isRecord(snapshot.viewport) ? snapshot.viewport : {};
  const images = Array.isArray(snapshot.images) ? snapshot.images : undefined;
  return {
    id,
    version,
    strokes: Array.isArray(snapshot.strokes) ? snapshot.strokes as BoardDocument["strokes"] : [],
    texts: Array.isArray(snapshot.texts) ? snapshot.texts as BoardDocument["texts"] : [],
    ...(images ? { images: images as BoardDocument["images"] } : {}),
    viewport: { x: numberOrNull(viewport.x) ?? 0, y: numberOrNull(viewport.y) ?? 0, zoom: numberOrNull(viewport.zoom) ?? 1 },
    updated_at: updatedAt ?? new Date(0).toISOString(),
  };
}

function hasBoardDraft(workspace: ExamWorkspace | undefined): boolean {
  if (!workspace) return false;
  const snapshot = workspace.board.snapshot;
  return (Array.isArray(snapshot.strokes) && snapshot.strokes.length > 0) || (Array.isArray(snapshot.texts) && snapshot.texts.length > 0) || (Array.isArray(snapshot.images) && snapshot.images.length > 0);
}
function ReportPanel({ examId, report }: { examId: string | null; report: ExamReport }) {
  const data = readReport(report);
  useEffect(() => {
    let nextFrame=0;
    const frame=requestAnimationFrame(()=>{nextFrame=requestAnimationFrame(()=>{const heading=document.getElementById("exam-report-heading");heading?.focus({preventScroll:true});heading?.scrollIntoView({block:"start",behavior:window.matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"});});});
    return()=>{cancelAnimationFrame(frame);cancelAnimationFrame(nextFrame);};
  }, [report.exam_session_id]);
  return <section className={styles.report} aria-labelledby="exam-report-heading">
    <div className={styles.reportHeading}><div><span className="eyebrow">РАЗБОР ПОСЛЕ СДАЧИ</span><h2 id="exam-report-heading" tabIndex={-1}>Результат варианта</h2></div></div>
    <p className={styles.practiceNote}><Icon name="info" size={14} />{data.mode === "study" ? "Учебный режим: " : data.mode === "strict" ? "Строгий режим: " : ""}{data.practiceOnly ? "это тренировочный результат для анализа подготовки, а не прогноз балла настоящего ЕГЭ." : "результат доступен только для анализа подготовки; прогноз балла не рассчитывается."}</p>
    <div className={styles.scoreGrid}><Score label="Первичный результат" value={data.primary != null && data.maxPrimary != null ? `${data.primary} / ${data.maxPrimary}` : "—"} /><Score label="Тестовый балл" value={data.testScore != null ? `${data.testScore} / 100` : "—"} /><Score label="Правильные ответы" value={data.correct != null && data.total != null ? `${data.correct} / ${data.total}` : "—"} /><Score label="Время" value={data.elapsed != null ? formatElapsed(data.elapsed) : "—"} /><Score label={data.year != null ? `Шкала · ориентир ${data.year}` : "Шкала"} value={data.year != null ? String(data.year) : "Не применяется"} /></div>
    {data.note ? <p className={styles.scoreScaleNote}>{data.note}</p> : null}
    <details className={styles.reportDetails} open><summary>Разбор по заданиям <span>{data.items.length}</span></summary><div className={styles.reportItems}>{data.items.map((item, index) => <div className={styles.reportItem} key={item.id ?? `${index}`}><span className={styles.reportItemNumber}>{item.position != null ? item.position + 1 : index + 1}</span><div><strong>{reportStatusLabel(item.status)}</strong><small>{item.sourceId ? `КЕГЭ № ${item.sourceId} · ` : ""}{item.difficulty ? difficultyLabel(item.difficulty) : "Уровень не указан"}{item.line != null ? ` · линия ${item.line}` : ""}</small></div><div className={styles.reportItemAnswers}><div><span>Ваш ответ</span><strong>{formatReportAnswer(item.answer)}</strong></div><div><span>Верный ответ</span><strong>{formatReportAnswer(item.expectedAnswer)}</strong></div><div className={styles.reportPoints}><span>Баллы · заработано / максимум</span><strong>{formatReportPoints(item.points, item.maxPoints)}</strong></div></div></div>)}</div></details>
    <ReportWorkspaceReview examId={examId} items={data.workspaces} sessionId={report.exam_session_id} />
    <p className={styles.nextVariant}>Можно начать следующий вариант.</p>
  </section>;
}

function Score({ label, value }: { label: string; value: string }) { return <div className={styles.score}><span>{label}</span><strong>{value}</strong></div>; }
type ReportItemView = { id?: string; sourceId?: string; position?: number; line?: number | null; difficulty?: ExamDifficulty | null; status?: string; answer?: unknown; expectedAnswer?: unknown; points?: number | null; maxPoints?: number | null };
type ReportCodeRunView = { id: string; status: string; stdout: string; stderr: string; completedAt: string | null };
type ReportWorkspaceView = { itemId: string; label: string; code: string; codeVersion: number; board: Record<string, unknown>; boardVersion: number; codeRuns: ReportCodeRunView[] };
function readReport(report: ExamReport) {
  const raw = report.report;
  const scale = isRecord(raw.score_scale) ? raw.score_scale : {};
  const reportItems = Array.isArray(raw.items) ? raw.items.filter(isRecord).map((item): Record<string, unknown> => ({ ...item, difficulty: item.difficulty === "easy" ? "basic" : item.difficulty })) : [];
  const workspaceRecord = isRecord(raw.workspaces) ? raw.workspaces : {};
  const mode = raw.mode === "strict" || raw.mode === "study" || raw.mode === "legacy" ? raw.mode : "legacy";
  const workspaces = Object.entries(workspaceRecord).flatMap(([itemId, value]): ReportWorkspaceView[] => {
    if (!isRecord(value)) return [];
    const sourceItem=reportItems.find(item=>item.item_id===itemId);
    return [{
      itemId,
      label: typeof sourceItem?.source_id === "string" ? `КЕГЭ № ${sourceItem.source_id}` : sourceItem && typeof sourceItem.position === "number" ? `Позиция ${sourceItem.position+1}` : "Снимок задания",
      code: typeof value.code === "string" ? value.code : "",
      codeVersion: numberOrNull(value.code_version) ?? 0,
      board: isRecord(value.board) ? value.board : {},
      boardVersion: numberOrNull(value.board_version) ?? 0,
      codeRuns: Array.isArray(value.code_runs) ? value.code_runs.filter(isRecord).map((run): ReportCodeRunView => ({
        id: typeof run.id === "string" ? run.id : `${itemId}-run`,
        status: typeof run.status === "string" ? run.status : "unavailable",
        stdout: typeof run.stdout === "string" ? run.stdout : "",
        stderr: typeof run.stderr === "string" ? run.stderr : "",
        completedAt: typeof run.completed_at === "string" ? run.completed_at : null,
      })) : [],
    }];
  });
  return {
    practiceOnly: raw.practice_only === true,
    mode,
    primary: numberOrNull(raw.primary_score), maxPrimary: numberOrNull(raw.max_primary_score),
    testScore: numberOrNull(raw.test_score), correct: numberOrNull(raw.correct_count), total: numberOrNull(raw.total_count),
    elapsed: numberOrNull(raw.elapsed_seconds), year: numberOrNull(scale.year),
    note: typeof scale.note === "string" ? scale.note : null,
    workspaces,
    items: reportItems.map((item): ReportItemView => ({
      id: typeof item.item_id === "string" ? item.item_id : undefined,
      sourceId:typeof item.source_id === "string" ? item.source_id : undefined,
      position: numberOrNull(item.position) ?? undefined, line: numberOrNull(item.line_number),
      difficulty: isDifficulty(item.difficulty) ? item.difficulty : null,
      status: typeof item.status === "string" ? item.status : undefined,
      answer: item.submitted_answer, expectedAnswer: item.expected_answer,
      points: numberOrNull(item.points), maxPoints: numberOrNull(item.max_points),
    })),
  };
}

function ReportWorkspaceReview({ examId, items, sessionId }: { examId: string | null; items: ReportWorkspaceView[]; sessionId: string }) {
  const [surfaceByItem, setSurfaceByItem] = useState<Record<string, "code" | "board">>({});
  const [localRunsByItem, setLocalRunsByItem] = useState<Record<string, ReportCodeRunView[]>>({});
  const itemIds = items.map((item) => item.itemId).join("\u001f");
  useEffect(() => {
    if (!examId) {
      setLocalRunsByItem({});
      return;
    }
    setLocalRunsByItem(Object.fromEntries(items.map((item) => [
      item.itemId,
      readDevicePythonRuns(browserExamRunScope(examId, sessionId, item.itemId)).map(toReportCodeRunView),
    ])));
  }, [examId, itemIds, sessionId]);
  if (!items.length) return null;
  return <details className={styles.reportWorkspace}><summary>История рабочих мест <span>снимок на момент сдачи</span></summary><div className={styles.reportWorkspaceList}>{items.map((item) => { const surface = surfaceByItem[item.itemId] ?? "code"; return <article className={styles.reportWorkspaceItem} key={item.itemId}><div className={styles.reportWorkspaceHeading}><strong>{item.label}</strong><span>Сохранённый снимок</span><div className={styles.solutionTabs} role="tablist" aria-label={`История рабочего места ${item.itemId}`}><button aria-selected={surface === "code"} className={surface === "code" ? styles.solutionTabActive : styles.solutionTab} onClick={() => setSurfaceByItem((current) => ({ ...current, [item.itemId]: "code" }))} role="tab" type="button">Код</button><button aria-selected={surface === "board"} className={surface === "board" ? styles.solutionTabActive : styles.solutionTab} onClick={() => setSurfaceByItem((current) => ({ ...current, [item.itemId]: "board" }))} role="tab" type="button">Доска</button></div></div>{surface === "code" ? <><CodeEditor draftKey={`report:${item.itemId}:${item.codeVersion}`} onChange={() => undefined} readOnly value={item.code} /><ReportCodeRunHistory runs={item.codeRuns} /><ReportLocalCodeRunHistory runs={localRunsByItem[item.itemId] ?? []} /></> : <InfiniteBoard active={false} disabled initialBoard={boardFromSnapshot(`report-board-${item.itemId}`, item.boardVersion, item.board)} sessionId={`report:${item.itemId}`} />}</article>; })}</div></details>;
}
function ReportCodeRunHistory({ runs }: { runs: ReportCodeRunView[] }) {
  if (!runs.length) return null;
  return <details className={styles.codeRunHistory}><summary>Проверки кода <span>{runs.length}</span></summary><div>{runs.map((run) => <article className={styles.reportCodeRun} key={run.id}><strong>{codeRunStatusLabel(run.status as ExamCodeRun["status"])}</strong>{run.stdout ? <pre>{run.stdout}</pre> : null}{run.stderr ? <pre className={styles.codeRunError}>{run.stderr}</pre> : null}</article>)}</div></details>;
}
function ReportLocalCodeRunHistory({ runs }: { runs: ReportCodeRunView[] }) {
  if (!runs.length) return null;
  return <details className={styles.codeRunHistory}><summary>Локальные запуски в браузере <span>{runs.length}</span></summary><p className={styles.workspaceNote}>Вывод программы не является проверкой ответа.</p><div>{runs.map((run) => <article className={styles.reportCodeRun} key={run.id}><strong>{codeRunStatusLabel(run.status as ExamCodeRun["status"])}</strong>{run.stdout ? <pre>{run.stdout}</pre> : null}{run.stderr ? <pre className={styles.codeRunError}>{run.stderr}</pre> : null}</article>)}</div></details>;
}
function toReportCodeRunView(run: CodeRun): ReportCodeRunView {
  const terminal = run.status === "completed" || run.status === "failed" || run.status === "timed_out" || run.status === "unavailable" || run.status === "succeeded";
  return { id: run.id, status: run.status, stdout: run.stdout, stderr: run.stderr, completedAt: terminal ? run.created_at : null };
}
function isRecord(value: unknown): value is Record<string, unknown> { return Boolean(value) && typeof value === "object" && !Array.isArray(value); }
function numberOrNull(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function isDifficulty(value: unknown): value is ExamDifficulty { return value === "basic" || value === "medium" || value === "hard"; }
function difficultyLabel(value: ExamDifficulty): string { return DIFFICULTIES.find((item) => item.value === value)?.label ?? value; }
function reportStatusLabel(value: string | undefined): string { return value === "correct" ? "Верно" : value === "partial" ? "Частично" : value === "unanswered" ? "Без ответа" : value === "incorrect" ? "Неверно" : "Статус не указан"; }
function formatReportAnswer(value: unknown): string { if (typeof value === "string") return value.trim() ? value.replace(/\\r\\n|\\n|\\r/g, "\n") : "Нет ответа"; if (typeof value === "number" || typeof value === "boolean") return String(value); return "Не указано"; }
function formatReportPoints(points: number | null | undefined, maxPoints: number | null | undefined): string { return points != null && maxPoints != null ? `${points} / ${maxPoints}` : "—"; }
function scoringLabel(value: string | null | undefined): string { return value === "score-v1" ? "Старый формат подсчёта: 1 балл за задание" : value === "ege-practice-2027-v1" ? "Практический формат ЕГЭ-2027" : value ?? "Формат подсчёта не указан"; }
function isImageAsset(asset: AssetRef): boolean { return ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(asset.media_type.trim().toLowerCase()) || /\.(png|jpe?g|gif|webp)$/i.test(asset.name); }
function isBrowserExamRun(run: ExamCodeRun): boolean { return isRecord(run.resource) && run.resource.provenance === "browser"; }
function toLocalExamCodeRun(run: CodeRun, examId: string, itemId: string, revisionId: string, workspaceVersion: number, files: DeviceFile[]): ExamCodeRun {
  const terminal = run.status === "completed" || run.status === "failed" || run.status === "timed_out" || run.status === "unavailable" || run.status === "succeeded";
  return {
    id: run.id,
    exam_id: examId,
    item_id: itemId,
    task_revision_id: revisionId,
    workspace_version: run.code_version ?? workspaceVersion,
    code_hash: "",
    status: run.status,
    executor_status: "browser-local",
    stdout: run.stdout,
    stderr: run.stderr,
    exit_code: run.exit_code,
    timed_out: run.status === "timed_out",
    duration_ms: run.duration_ms,
    resource: { provenance: "browser", persistence: "localStorage", storage: "local", grading: false, runner: "pyodide" },
    input_manifest: { kind: "device-cache", scope: "local", files: files.map((file) => file.name) },
    created_at: run.created_at,
    completed_at: terminal ? run.created_at : null,
  };
}
function mergeExamCodeRuns(...groups: ExamCodeRun[][]): ExamCodeRun[] {
  const byId = new Map<string, ExamCodeRun>();
  for (const group of groups) for (const run of group) if (!byId.has(run.id)) byId.set(run.id, run);
  return [...byId.values()].sort((left, right) => (Date.parse(right.completed_at ?? right.created_at) || 0) - (Date.parse(left.completed_at ?? left.created_at) || 0));
}
function isExamTimeExpired(reason: unknown): boolean { return reason instanceof ApiError && reason.code === "EXAM_TIME_EXPIRED"; }
function formatCountdown(seconds: number | null): string { if (seconds == null) return "—"; const h = Math.floor(seconds / 3600); const m = Math.floor((seconds % 3600) / 60); const s = seconds % 60; return [h, m, s].map((value) => String(value).padStart(2, "0")).join(":"); }
function formatElapsed(seconds: number): string { return formatCountdown(Math.max(0, Math.round(seconds))); }
function formatDeadline(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }
function monotonicNow(): number { return typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now(); }
function examDraftScope(examId: string, itemId: string): string { return `exam:${examId}:item:${itemId}`; }
function browserExamRunScope(examId: string, sessionId: string, itemId: string): string { return `exam:${examId}:${sessionId}:item:${itemId}:browser`; }
function codeRunStatusLabel(status: ExamCodeRun["status"]): string { return status === "succeeded" || status === "completed" ? "Выполнено" : status === "failed" ? "Завершено с ошибкой" : status === "timed_out" ? "Время проверки вышло" : status === "unavailable" ? "Недоступно" : "Проверяем"; }
function formatRunTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }

function ExamHistoryCard({ blocked, exam, busy, onOpenReport }: { blocked: boolean; exam: ExamRecord; busy: boolean; onOpenReport: (exam: ExamRecord) => void }) {
  const paused = exam.status === "paused"; const completed = exam.status === "completed"; const ready = exam.lifecycle === "ready";
  return <article className={`${styles.historyCard} ${paused ? styles.historyPaused : ""}`}><div><span className="exam-year">{exam.year ? `ЕГЭ · ${exam.year}` : "Тренировочный вариант"}</span><h3>{exam.name}</h3><p>{exam.mode === "study" ? "Учебный режим · " : exam.mode === "strict" ? "Строгий режим · " : ""}{exam.difficulty ? `${difficultyLabel(exam.difficulty)} · ` : ""}{ready ? "Вариант подготовлен" : paused ? "Черновик сохранён" : completed ? "Вариант завершён" : "Вариант выполняется"}</p></div><div className={styles.historyCardActions}>{ready ? <span className={styles.pausedLabel}>Предпросмотр выше</span> : null}{paused ? <span className={styles.pausedLabel}>Пауза</span> : null}{completed ? <button className="outline-button" disabled={busy || blocked} onClick={() => onOpenReport(exam)} type="button">{busy ? "Открываем…" : "Открыть разбор"}</button> : null}</div></article>;
}

function ExamCard({ exam, busy, blocked, onStart }: { exam: ExamRecord; busy: boolean; blocked: boolean; onStart: (id: string) => void }) { return <article className="exam-card"><div className="exam-card-heading"><div><span className="exam-year">ЕГЭ · {exam.year ?? "год не указан"}</span><h2>{exam.name}</h2></div><span className={`exam-state ${exam.status}`}>{exam.status === "completed" ? "Завершён" : "Готов"}</span></div><div className="exam-facts"><Fact label="Время" value={exam.duration_minutes != null ? `${exam.duration_minutes} мин` : "Назначается при сборке"} /><Fact label="Позиций" value={exam.revision_count != null ? String(exam.revision_count) : "Назначается при сборке"} /><Fact label="Подсчёт" value={scoringLabel(exam.scoring_version)} /></div><div className="exam-card-footer"><span className="muted-copy">Состав и порядок фиксируются сервером при запуске.</span><button className="primary-button" disabled={busy || blocked} onClick={() => onStart(exam.id)} type="button">{busy ? "Запуск…" : "Начать вариант"}</button></div></article>; }
function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) { return <div className="exam-fact"><span>{label}</span><strong className={mono ? "mono" : ""}>{value}</strong></div>; }
function ExamState({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) { return <main className="page-state"><div className="state-icon state-icon-error"><Icon name="warning" size={22} /></div><h1>{title}</h1><p>{message}</p><p className="state-help">Backend error отображается как есть; frontend не подменяет его demo-данными.</p>{onRetry ? <button className="outline-button" onClick={onRetry} type="button">Повторить загрузку</button> : null}</main>; }
