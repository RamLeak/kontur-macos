"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/icon";
import { DEMO_MODE, getErrors, getMastery, getReviewQueue, updateError } from "@/lib/api";
import type { ErrorEvent, MasteryState, ReviewItem } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";
import styles from "./learning-view.module.css";

type LearningTab = "skills" | "errors";

export function LearningView({ initialTab = "skills" }: { initialTab?: LearningTab }) {
  const [tab, setTab] = useState<LearningTab>(initialTab);
  const [mastery, setMastery] = useState<MasteryState[]>([]);
  const [errors, setErrors] = useState<ErrorEvent[]>([]);
  const [review, setReview] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionId, setActionId] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedErrorId, setSelectedErrorId] = useState<string | null>(null);
  const [mobilePreview, setMobilePreview] = useState(false);
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [noteError, setNoteError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActionError(null);
    Promise.all([getMastery(), getErrors(), getReviewQueue()])
      .then(([nextMastery, nextErrors, nextReview]) => {
        if (cancelled) return;
        setMastery(nextMastery);
        setErrors(nextErrors);
        setReview(nextReview);
        setSelectedErrorId((current) => (
          current && nextErrors.some((item) => item.id === current)
            ? current
            : nextErrors[0]?.id ?? null
        ));
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(readableError(reason, "Не удалось загрузить результаты. Обновите страницу и попробуйте снова."));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  const unconfirmedErrors = useMemo(() => errors.filter((item) => item.status === "proposed"), [errors]);
  const stableSkills = useMemo(() => mastery.filter((item) => item.status === "stable").length, [mastery]);
  const selectedError = errors.find((item) => item.id === selectedErrorId) ?? errors[0] ?? null;

  function selectError(item: ErrorEvent) {
    setSelectedErrorId(item.id);
    setMobilePreview(true);
    setEditingNoteId(null);
    setNoteError(null);
  }

  async function confirmError(item: ErrorEvent) {
    setActionId(item.id);
    setActionError(null);
    try {
      const updated = await updateError(item.id, { status: "confirmed" });
      setErrors((current) => current.map((value) => value.id === updated.id ? { ...value, status: updated.status ?? "confirmed" } : value));
    } catch (reason: unknown) {
      setActionError(readableError(reason, "Ошибка не подтверждена."));
    } finally {
      setActionId(null);
    }
  }

  function beginNoteEdit(item: ErrorEvent) {
    setEditingNoteId(item.id);
    setNoteDraft(item.user_note ?? "");
    setNoteError(null);
  }

  function cancelNoteEdit() {
    setEditingNoteId(null);
    setNoteError(null);
  }

  async function saveNote(item: ErrorEvent) {
    setActionId(item.id);
    setNoteError(null);
    try {
      const updated = await updateError(item.id, {
        status: item.status ?? "proposed",
        prevention_note: noteDraft,
      });
      setErrors((current) => current.map((value) => value.id === item.id
        ? { ...value, user_note: updated.user_note ?? noteDraft, note: value.note }
        : value));
      setEditingNoteId(null);
    } catch (reason: unknown) {
      setNoteError(readableError(reason, "Не удалось сохранить заметку."));
    } finally {
      setActionId(null);
    }
  }

  if (error) {
    return (
      <LearningState
        title="Раздел обучения недоступен"
        message={error}
        onRetry={() => setReloadToken((value) => value + 1)}
      />
    );
  }

  return (
    <main className={styles.page}>
      <header className={styles.heading}>
        <div>
          <span className={styles.eyebrow}>Наблюдаемые результаты</span>
          <h1>{tab === "skills" ? "Карта навыков" : "Журнал ошибок"}</h1>
          <p className={styles.lead}>Разберите ошибки и запишите, что поможет в следующей попытке.</p>
        </div>
        <div className={styles.tabs} role="tablist" aria-label="Раздел обучения">
          <button aria-selected={tab === "skills"} className={tab === "skills" ? styles.tabActive : styles.tab} onClick={() => setTab("skills")} role="tab" type="button">Навыки</button>
          <button aria-selected={tab === "errors"} className={tab === "errors" ? styles.tabActive : styles.tab} onClick={() => setTab("errors")} role="tab" type="button">Ошибки</button>
        </div>
      </header>

      {actionError ? (
        <div className={styles.actionError} role="alert">
          <span>{actionError}</span>
          <button onClick={() => setReloadToken((value) => value + 1)} type="button">Повторить</button>
        </div>
      ) : null}

      <section className={styles.metricStrip}>
        <Metric label="Навыков в карте" value={loading ? "—" : String(mastery.length)} />
        <Metric label="Стабильных" value={loading ? "—" : String(stableSkills)} />
        <Metric label="Неподтверждённых" value={loading ? "—" : String(unconfirmedErrors.length)} tone={unconfirmedErrors.length ? "warning" : ""} />
        <Metric label="На повторение" value={loading ? "—" : String(review.length)} />
      </section>

      {tab === "skills" ? (
        <div className={styles.learningGrid}>
          <section className={styles.panel}>
            <div className={styles.sectionHeading}>
              <div><span className={styles.eyebrow}>Состояние</span><h2>Навыки информатики</h2></div>
              <span className={styles.muted}>По результатам решений</span>
            </div>
            {loading ? <LoadingLine /> : <div className={styles.skillList}>{mastery.map((item) => <SkillRow item={item} key={item.id} />)}</div>}
          </section>
          <ReviewPanel items={review} />
        </div>
      ) : (
        <div className={styles.notebookShell}>
          <div className={styles.notebookLayout} data-mobile-preview={mobilePreview ? "true" : "false"}>
            <section className={styles.errorListPane} aria-label="Журнал ошибок">
              <div className={styles.sectionHeading}>
                <div><span className={styles.eyebrow}>Журнал</span><h2>Ошибки</h2></div>
                <span className={styles.muted}>{errors.length} записей</span>
              </div>
              {loading ? <LoadingLine /> : errors.length ? (
                <div className={styles.errorList} role="list">
                  {errors.map((item) => (
                    <ErrorListRow
                      item={item}
                      key={item.id}
                      selected={item.id === selectedError?.id}
                      actionId={actionId}
                      onSelect={() => selectError(item)}
                      onConfirm={() => void confirmError(item)}
                    />
                  ))}
                </div>
              ) : (
                <p className={styles.emptyCopy}>Здесь пока нет записей. Сохранённые события появятся после разбора попытки.</p>
              )}
            </section>

            <section className={styles.errorPreviewPane} aria-label="Предпросмотр ошибки">
              <button className={styles.mobileBack} onClick={() => setMobilePreview(false)} type="button">
                <Icon name="chevron-left" size={16} />
                К списку ошибок
              </button>
              {loading ? <LoadingLine /> : selectedError ? (
                <ErrorPreview
                  item={selectedError}
                  actionId={actionId}
                  editingNoteId={editingNoteId}
                  noteDraft={noteDraft}
                  noteError={noteError}
                  onBeginNoteEdit={() => beginNoteEdit(selectedError)}
                  onCancelNoteEdit={cancelNoteEdit}
                  onNoteDraftChange={setNoteDraft}
                  onSaveNote={() => void saveNote(selectedError)}
                />
              ) : (
                <div className={styles.previewEmpty}><Icon name="info" size={22} /><p>Выберите событие в журнале.</p></div>
              )}
            </section>
          </div>
          <ReviewPanel items={review} />
        </div>
      )}
    </main>
  );
}

function SkillRow({ item }: { item: MasteryState }) {
  const status = statusMeta(item.status);
  const rate = Math.max(0, Math.min(item.independent_success_rate ?? 0, 100));
  return (
    <div className={styles.skillRow}>
      <span className={styles.skillStatus + " " + styles["skillStatus" + status.tone]}><Icon name={status.icon} size={15} /></span>
      <div className={styles.skillCopy}>
        <strong>{item.name}</strong>
        <small>{status.label}</small>
        <small>{item.reason === "no append-only evidence recorded" ? "Сохраните попытку по этой теме, чтобы появились данные." : item.reason}</small>
      </div>
      <div className={styles.skillProgress}><div className={styles.progressTrack}><span style={{ width: String(rate) + "%" }} /></div><small>{item.independent_success_rate != null ? String(item.independent_success_rate) + "%" : "—"}</small></div>
      <span className={styles.evidenceCount}>{item.evidence_count} записей</span>
    </div>
  );
}

function ErrorListRow({
  item,
  selected,
  actionId,
  onSelect,
  onConfirm,
}: {
  item: ErrorEvent;
  selected: boolean;
  actionId: string | null;
  onSelect: () => void;
  onConfirm: () => void;
}) {
  const status = errorStatusMeta(item.status);
  return (
    <div className={styles.errorRow} role="listitem">
      <button aria-pressed={selected} className={selected ? styles.errorSelectActive : styles.errorSelect} onClick={onSelect} type="button">
        <span className={styles.errorStatus + " " + styles["errorStatus" + status.value]}><Icon name={status.icon} size={15} /></span>
        <span className={styles.errorCopy}>
          <strong>{item.task_line != null && item.task_line > 0 ? "№ " + item.task_line : "Задание"}</strong>
          <small>{(item.type || "Тип не указан") + " · " + item.title}</small>
        </span>
        <span className={styles.errorStateLabel + " " + styles["errorState" + status.value]}>{status.label}</span>
      </button>
      {status.value === "proposed" ? (
        <button className={styles.confirmButton} disabled={actionId === item.id} onClick={onConfirm} type="button">
          {actionId === item.id ? "Сохраняем…" : "Подтвердить"}
        </button>
      ) : null}
    </div>
  );
}

function ErrorPreview({
  item,
  actionId,
  editingNoteId,
  noteDraft,
  noteError,
  onBeginNoteEdit,
  onCancelNoteEdit,
  onNoteDraftChange,
  onSaveNote,
}: {
  item: ErrorEvent;
  actionId: string | null;
  editingNoteId: string | null;
  noteDraft: string;
  noteError: string | null;
  onBeginNoteEdit: () => void;
  onCancelNoteEdit: () => void;
  onNoteDraftChange: (value: string) => void;
  onSaveNote: () => void;
}) {
  const status = errorStatusMeta(item.status);
  const revisionId = typeof item.task_revision_id === "string" ? item.task_revision_id.trim() : "";
  const correctionHref = revisionId
    ? "/workspace?revision=" + encodeURIComponent(revisionId) + "&fresh=correction"
    : null;
  const noteEditing = editingNoteId === item.id;
  return (
    <article className={styles.errorPreview}>
      <header className={styles.previewHeader}>
        <div>
          <span className={styles.eyebrow}>Событие ошибки</span>
          <h2>{item.task_line != null && item.task_line > 0 ? "№ " + item.task_line : "Задание"}</h2>
          <p className={styles.previewTitle}>{item.title}</p>
        </div>
        <span className={styles.errorStateLabel + " " + styles["errorState" + status.value]}>{status.label}</span>
      </header>

      <div className={styles.previewMeta}>
        <span>Тип · {item.type || "не указан"}</span>
        <span>{eventDate(item.created_at)}</span>
        <span>{linkedEvidenceLabel(item)}</span>
      </div>

      <section className={styles.historyBlock}>
        <div className={styles.blockHeading}><h3>История события</h3><Icon name="info" size={16} /></div>
        <p>{item.note || "Историческое описание не передано."}</p>
        <small>Эта запись отражает прошлую оценку и не меняется вместе с учебной заметкой.</small>
      </section>

      <section className={styles.noteBlock}>
        <div className={styles.blockHeading}><h3>Моя заметка</h3><span className={styles.muted}>Только для планирования</span></div>
        {noteEditing ? (
          <>
            <textarea aria-label="Заметка об ошибке" className={styles.noteInput} onChange={(event) => onNoteDraftChange(event.target.value)} value={noteDraft} />
            {noteError ? <p className={styles.noteError} role="alert">{noteError}</p> : null}
            <div className={styles.noteActions}>
              <button className={styles.secondaryButton} onClick={onCancelNoteEdit} type="button">Отмена</button>
              <button className={styles.primaryButton} disabled={actionId === item.id} onClick={onSaveNote} type="button">{actionId === item.id ? "Сохраняем…" : "Сохранить заметку"}</button>
            </div>
          </>
        ) : (
          <>
            <p className={item.user_note ? styles.noteText : styles.notePlaceholder}>{item.user_note || "Добавьте короткий план, как распознать и исправить этот тип ошибки."}</p>
            <button className={styles.noteEditButton} onClick={onBeginNoteEdit} type="button">Изменить заметку</button>
          </>
        )}
      </section>

      <section className={styles.correctionBlock}>
        <div>
          <h3>Следующее исправление</h3>
          <p>{correctionHref ? "Откройте ту же сохранённую редакцию и начните отдельную correction-сессию." : "Для события не передана сохранённая редакция."}</p>
        </div>
        {correctionHref ? (
          <Link className={styles.primaryButton} href={correctionHref}>Исправить <Icon name="chevron-right" size={15} /></Link>
        ) : (
          <button className={styles.disabledAction} disabled title="Нет сохранённой редакции для новой сессии" type="button">Исправить</button>
        )}
      </section>
    </article>
  );
}

function ReviewPanel({ items }: { items: ReviewItem[] }) {
  return (
    <section className={styles.reviewPanel}>
      <div className={styles.sectionHeading}>
        <div><span className={styles.eyebrow}>Следующий шаг</span><h2>Очередь повторения</h2></div>
        <Icon name="refresh" size={18} />
      </div>
      {items.length ? (
        <div className={styles.reviewList}>
          {items.map((item) => (
            <div className={styles.reviewItem} key={item.id}>
              <span className={styles.reviewLine}>{item.task_line ?? "—"}</span>
              <span className={styles.reviewCopy}><strong>{item.label}</strong><small>{item.reason}</small></span>
              <span className={styles.reviewDue}>{item.due_label}</span>
              <Link className={styles.reviewAction} href={reviewHref(item)} onClick={() => rememberReviewLine(item)}>Перейти к повторению</Link>
            </div>
          ))}
        </div>
      ) : <p className={styles.emptyCopy}>Очередь пуста.</p>}
    </section>
  );
}

function reviewHref(item: ReviewItem): string {
  return item.task_line != null && item.task_line > 0
    ? "/tasks?line=" + encodeURIComponent(String(item.task_line))
    : "/tasks";
}

function rememberReviewLine(item: ReviewItem) {
  if (typeof window === "undefined" || item.task_line == null || item.task_line <= 0) return;
  try {
    const key = "kontur:ui:task-bank:filters:v1";
    const stored = JSON.parse(localStorage.getItem(key) || "{}") as Record<string, unknown>;
    localStorage.setItem(key, JSON.stringify({ ...stored, line: item.task_line }));
  } catch {
    // Filter persistence is optional; the link still opens the bank.
  }
}

function linkedEvidenceLabel(item: ErrorEvent): string {
  if (item.solve_session_id) return "Привязано к рабочей сессии";
  if (item.attempt_id) return "Привязано к попытке";
  return "Источник события не указан";
}

function Metric({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return <div className={styles.metricItem + " " + (tone ? styles.metricWarning : "")}><span>{label}</span><strong>{value}</strong></div>;
}

function LoadingLine() {
  return <div className={styles.loadingLine} role="status"><span className={styles.spinner} /> Загружаем результаты…</div>;
}

function statusMeta(status: MasteryState["status"]): { icon: "check" | "target" | "warning" | "info"; label: string; tone: string } {
  if (status === "stable") return { icon: "check", label: "Стабильный", tone: "Stable" };
  if (status === "needs_attention") return { icon: "warning", label: "Нужно внимание", tone: "Attention" };
  if (status === "insufficient_evidence") return { icon: "info", label: "Пока мало данных", tone: "Insufficient" };
  return { icon: "target", label: "В развитии", tone: "Learning" };
}

function errorStatusMeta(status: ErrorEvent["status"]): { value: NonNullable<ErrorEvent["status"]>; label: string; icon: "warning" | "info" } {
  if (status === "confirmed") return { value: "confirmed", label: "Классификация подтверждена", icon: "info" };
  if (status === "rejected") return { value: "rejected", label: "Отклонена", icon: "info" };
  return { value: "proposed", label: "Предложена", icon: "warning" };
}

function eventDate(value: string | null | undefined): string {
  if (!value) return "Дата события не указана";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "Дата события не указана";
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" }).format(new Date(timestamp));
}

function LearningState({ title, message, onRetry }: { title: string; message: string; onRetry: () => void }) {
  return (
    <main className={styles.statePage}>
      <div className={styles.stateIcon}><Icon name="warning" size={22} /></div>
      <h1>{title}</h1>
      <p>{message}</p>
      <p className={styles.stateHelp}>Сохранённые решения и попытки не изменены.</p>
      <button className={styles.primaryButton} onClick={onRetry} type="button">Повторить</button>
    </main>
  );
}