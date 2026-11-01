"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { olderAttempts, progressLabels } from "@/lib/catalog";
import { native, safeError } from "@/lib/native";
import { readStatementBlocks } from "@/lib/statement-blocks";
import { StatementContent } from "./statement-content";
import type { AssetMetadata, Attempt, Document, MarkAttempt, ProgressSummary, Task, Verdict } from "@/lib/native-types";

function LocalAsset({ asset }: { asset: AssetMetadata }) {
  const [url, setUrl] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const image = ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(asset.mime) && asset.sizeBytes <= 8 * 1024 * 1024;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; if (urlRef.current) URL.revokeObjectURL(urlRef.current); }; }, []);
  async function show() {
    setBusy(true); setError(null);
    try {
      const data = await native.asset(asset.id);
      if (!mounted.current) return;
      if (data.metadata.id !== asset.id || data.metadata.sha256 !== asset.sha256 || !image) throw new Error("Invalid asset");
      const next = URL.createObjectURL(new Blob([new Uint8Array(data.bytes)], { type: asset.mime }));
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = next; setUrl(next);
    } catch (reason) { if (mounted.current) setError(safeError(reason, "Не удалось открыть вложение.").message); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function save() { setBusy(true); setError(null); try { await native.saveAsset(asset.id); } catch (reason) { setError(safeError(reason).message); } finally { if (mounted.current) setBusy(false); } }
  return <div className="local-asset"><span>{asset.name} <small>· {Math.ceil(asset.sizeBytes / 1024)} КБ</small></span><div>{image && !url && <button type="button" className="outline-button" disabled={busy} onClick={() => void show()}>{busy ? "Открываем…" : "Показать изображение"}</button>}<button type="button" className="outline-button" disabled={busy} onClick={() => void save()}>Сохранить файл</button></div>{url && <img src={url} alt={asset.name} />}{error && <p role="alert" className="panel-error">{error}</p>}</div>;
}

export function LocalStatement({ document, task, attempts, assets, progress, readOnly, onStatement, onMark }: {
  document: Document | null; task: Task | undefined; attempts: Attempt[]; assets: AssetMetadata[];
  progress?: ProgressSummary; readOnly: boolean; onStatement: (text: string) => void;
  onMark: (input: MarkAttempt) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [verdict, setVerdict] = useState<Verdict>("unverified");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<MarkAttempt | null>(null);
  const linked = !!document?.taskSourceId && !!document.taskRevision;
  const currentAttempts = useMemo(() => document ? attempts.filter(item => item.taskSourceId === document.taskSourceId && item.taskRevision === document.taskRevision) : [], [attempts, document?.taskSourceId, document?.taskRevision]);
  const oldCount = useMemo(() => task ? olderAttempts(task, attempts) : 0, [task, attempts]);
  const unknownAttempts = useMemo(() => document ? attempts.filter(item => item.taskSourceId === document.taskSourceId && item.taskRevision === null) : [], [attempts, document?.taskSourceId]);
  const blocks = useMemo(() => readStatementBlocks(task?.blocks ?? null), [task?.blocks]);
  async function mark() {
    if (!document?.taskSourceId || !document.taskRevision || busy) return;
    setBusy(true); setError(null);
    const payload = { taskSourceId: document.taskSourceId, taskRevision: document.taskRevision, verdict, code: document.code, answer };
    const previous = pending.current;
    const same = previous && previous.taskSourceId === payload.taskSourceId && previous.taskRevision === payload.taskRevision && previous.verdict === verdict && previous.code === payload.code && previous.answer === answer;
    const input = same ? previous : { ...payload, id: crypto.randomUUID() };
    pending.current = input;
    try { await onMark(input); pending.current = null; setAnswer(""); }
    catch (reason) { setError(safeError(reason).message); }
    finally { setBusy(false); }
  }

  return <section className="task-column local-condition" aria-label="Условие">
    <header className="workspace-pane-header"><strong>Условие</strong><span className="workspace-task-meta">{document?.isOwnWork !== false ? "Своя работа" : `Тип ${document.typeNumber ?? "—"}`}</span></header>
    <div className="task-scroll">
      <article className="task-document"><h1>{document?.title ?? "Своя работа"}</h1>
        {!linked ? <label className="local-own-statement"><span className="local-muted">Собственное условие или заметки</span><textarea aria-label="Собственное условие" value={document?.statement ?? ""} onChange={event => onStatement(event.target.value)} readOnly={readOnly} placeholder="Запишите условие своей задачи…" /></label> : <StatementContent source={document?.statement || "Условие в этом документе отсутствует."} />}
        {task && <div className="local-provenance"><span>Источник: {task.sourceId}</span><span title={task.revision}>Ревизия: {task.revision.slice(0, 14)}</span></div>}
        {blocks.answerTable && <p className="local-answer-format">Формат ответа: таблица {blocks.answerTable.rows} × {blocks.answerTable.cols}.</p>}
        {blocks.matrix && <div className="local-statement-table"><table><caption className="sr-only">Таблица задания</caption>{blocks.matrix.headers.length > 0 && <thead><tr>{blocks.matrix.headers.map((header, index) => <th key={index}>{header}</th>)}</tr></thead>}<tbody>{blocks.matrix.rows.map((row, index) => <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div>}
        {blocks.latex && <details className="local-formula-source"><summary>Условие с формулами · исходный текст</summary><pre>{blocks.latex}</pre></details>}
        {blocks.subtasks.map((child, index) => <section key={child.sourceId ?? index} className="local-subtask"><h2>Часть {index + 1}{child.typeNumber ? ` · Тип ${child.typeNumber}` : ""}</h2><StatementContent source={child.statement} />{child.assetIds.length > 0 && <p className="local-muted">Файлы: {child.assetIds.map(id => assets.find(asset => asset.id === id)?.name ?? id).join(", ")}</p>}</section>)}
        {blocks.unsupported && <p className="local-muted" role="status">В условии есть данные неподдерживаемого формата. Они сохранены и доступны при экспорте.</p>}
        {assets.map(asset => <LocalAsset key={asset.id} asset={asset} />)}
      </article>
      {linked && <div className="local-attempt"><h2>Мой результат</h2><p className={"local-progress is-" + (progress?.status ?? "not_started")}>{task ? progressLabels[progress?.status ?? "not_started"] : "Архивная ревизия · история ниже"}</p>{oldCount > 0 && <p className="local-muted">В других ревизиях: {oldCount} попыток. Они не определяют статус текущего условия.</p>}
        <label className="local-field"><span>Ответ или заметка</span><input value={answer} maxLength={100000} onChange={event => setAnswer(event.target.value)} readOnly={busy || readOnly} /></label>
        <label className="local-field"><span>Моя оценка попытки</span><select value={verdict} onChange={event => setVerdict(event.target.value as Verdict)} disabled={busy || readOnly}><option value="unverified">Без проверки</option><option value="correct">Верно</option><option value="wrong">Неверно</option><option value="error">Ошибка</option></select></label>
        <button type="button" className="outline-button" disabled={busy || readOnly} onClick={() => void mark()}>{busy ? "Сохраняем…" : "Записать попытку"}</button>
        {error && <p role="alert" className="panel-error">{error}</p>}
        <details><summary>История · {currentAttempts.length}</summary><ol className="local-attempt-list">{[...currentAttempts].sort((a, b) => b.createdAt - a.createdAt || b.id.localeCompare(a.id)).map(item => <li key={item.id}><time dateTime={new Date(item.createdAt).toISOString()}>{new Date(item.createdAt).toLocaleString("ru-RU")}</time><strong>{item.verdict === "correct" ? "Верно" : item.verdict === "wrong" ? "Неверно" : item.verdict === "error" ? "Ошибка" : "Без проверки"}</strong><span>{item.source === "imported" ? "Импорт" : "Своя оценка"}</span>{item.answer && <p>{item.answer}</p>}</li>)}</ol></details>
        {unknownAttempts.length > 0 && <details><summary>Импортировано: версия неизвестна · {unknownAttempts.length}</summary><p className="local-muted">Эти импортированные попытки не меняют прогресс текущей ревизии.</p><ol className="local-attempt-list">{unknownAttempts.map(item => <li key={item.id}><time>{new Date(item.createdAt).toLocaleString("ru-RU")}</time><span>{item.verdict === "correct" ? "Верно" : item.verdict === "wrong" ? "Неверно" : item.verdict === "error" ? "Ошибка" : "Без проверки"}</span>{item.answer && <p>{item.answer}</p>}</li>)}</ol></details>}
      </div>}
    </div>
  </section>;
}
