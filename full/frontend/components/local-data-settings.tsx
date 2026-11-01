"use client";
import { useRef, useState } from "react";
import { localNative, runLocalAction } from "@/lib/local-native";
import { waitForLocalMutations } from "@/lib/local-fetch";
import { reloadLocalClientStateAfterImport } from "@/lib/local-client-state";
import { lockLocalInteraction } from "@/lib/local-interaction-lock";
import { importSelection, importResult, exportResult, type FullImportSelection } from "@/lib/local-transfer";
import { flushOwnerWork } from "@/lib/owner-sync";
import styles from "./settings.module.css";

const labels: Record<string, string> = { tasks: "Задания", task_revisions: "Редакции заданий", attempts: "Попытки", assets: "Вложения", documents: "Работы", conversations: "Чаты", messages: "Сообщения", solve_sessions: "Решения", code_documents: "Документы кода", board_documents: "Доски", exam_sessions: "Экзамены", bookmarks: "Закладки", sync_records: "Черновики и настройки", learning_events: "История обучения" };
function Counts({ values }: { values: Record<string, number> }) { return <div className={styles.infoGrid}>{Object.entries(values).filter(([key]) => Object.hasOwn(labels, key)).map(([key, value]) => <div className={styles.infoItem} key={key}><small>{labels[key]}</small><strong>{value}</strong></div>)}</div>; }

export function LocalDataSettings() {
  const [preview, setPreview] = useState<FullImportSelection | null>(null);
  const [busy, setBusy] = useState(false); const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null); const [notice, setNotice] = useState<string | null>(null);
  const [resultCounts, setResultCounts] = useState<Record<string, number> | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  async function action(work: () => Promise<void>) {
    if (busyRef.current) return; busyRef.current = true; setBusy(true); setError(null); setNotice(null);
    const release = lockLocalInteraction();
    try { await runLocalAction(async () => {
      await waitForLocalMutations();
      const flushed = await flushOwnerWork();
      if (!flushed.serverConfirmed) throw new Error("Сначала сохраните текущую работу и устраните конфликты черновиков.");
      await work();
    }); } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось перенести данные."); }
    finally { release(); busyRef.current = false; setBusy(false); }
  }
  function choose() { void action(async () => { setPreview(importSelection(await localNative<unknown>("local_import_preview"))); setResultCounts(null); setWarnings([]); }); }
  function confirm() {
    if (!preview) return; const token = preview.token;
    void action(async () => {
      const result = importResult(await localNative<unknown>("local_import_confirm", { token }));
      setPreview(null); setResultCounts(result.imported); setWarnings(result.warnings);
      setNotice(`Данные импортированы. Совпадающих записей: ${result.duplicateRecords}.`);
      await reloadLocalClientStateAfterImport();
      window.location.reload();
    });
  }
  function backup() { void action(async () => { const result = exportResult(await localNative<unknown>("local_export_bundle")); if (result) { setResultCounts(result.counts); setNotice("Резервная копия сохранена."); } }); }
  return <section className={styles.panel} aria-labelledby="local-data-title">
    <div className={styles.panelHeading}><div><span className="eyebrow">Локальная рабочая область</span><h2 id="local-data-title">Данные</h2><p>Добавляйте задания и историю из файла .kontur или сохраняйте полную резервную копию.</p></div></div>
    <div className={styles.sectionActions}><button type="button" className="primary-button" disabled={busy} onClick={choose}>{busy ? "Подготавливаем…" : "Добавить файл .kontur"}</button><button type="button" className="outline-button" disabled={busy} onClick={backup}>Сохранить резервную копию</button></div>
    {preview && <section aria-label="Проверка импорта"><h3>Файл проверен</h3><p>Конфликтов: {preview.preview.conflictCount}.</p><Counts values={preview.preview.counts} /><p>Подтвердите добавление данных. Перед изменением хранилища создаётся резервная копия.</p><div className={styles.sectionActions}><button type="button" className="outline-button" disabled={busy} onClick={() => setPreview(null)}>Отмена</button><button type="button" className="primary-button" disabled={busy} onClick={confirm}>Импортировать</button></div>{preview.preview.warnings.length > 0 && <ul>{preview.preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}</section>}
    {notice && <p className={styles.inlineNotice} role="status">{notice}</p>}{error && <p className={styles.inlineError} role="alert">{error}</p>}
    {resultCounts && <Counts values={resultCounts} />}{warnings.length > 0 && <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </section>;
}
