"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/icon";
import { getConversations, getDailyPlan, getReviewQueue, rebuildDailyPlan } from "@/lib/api";
import { HISTORY_CHANGED_EVENT } from "@/lib/history-events";
import type { ConversationSummary, DailyPlan, ReviewItem } from "@/lib/types";
import styles from "./overview.module.css";

type SettledData = [
  PromiseSettledResult<DailyPlan>,
  PromiseSettledResult<ReviewItem[]>,
  PromiseSettledResult<ConversationSummary[]>,
];

export function OverviewView() {
  const [plan, setPlan] = useState<DailyPlan | null>(null);
  const [review, setReview] = useState<ReviewItem[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    void Promise.allSettled([getDailyPlan(), getReviewQueue(), getConversations()]).then((results) => {
      if (cancelled) return;
      const [planResult, reviewResult, conversationsResult] = results as SettledData;

      if (planResult.status === "fulfilled") {
        setPlan(planResult.value);
      }
      if (reviewResult.status === "fulfilled") setReview(reviewResult.value);
      if (conversationsResult.status === "fulfilled") {
        setConversations(conversationsResult.value.filter((conversation) => conversation.archived !== true));
      }

      if (results.some((result) => result.status === "rejected")) {
        setError("Не удалось загрузить часть данных. Попробуйте ещё раз или откройте банк заданий.");
      }
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
  }, [loadAttempt]);

  useEffect(() => {
    const handleHistoryChanged = () => setLoadAttempt((current) => current + 1);
    window.addEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
    return () => window.removeEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
  }, []);

  const latestConversation = useMemo(() => {
    return [...conversations].sort((left, right) => timestamp(right.updated_at) - timestamp(left.updated_at))[0] ?? null;
  }, [conversations]);

  async function handleRebuildPlan() {
    setRebuilding(true);
    setError(null);
    try {
      const nextPlan = await rebuildDailyPlan(plan?.date);
      setPlan(nextPlan);
    } catch {
      setError("План пока не удалось обновить. Попробуйте ещё раз.");
    } finally {
      setRebuilding(false);
    }
  }

  const planTitle = readablePlanTitle(plan?.title);
  const planDate = plan?.date && !isMachineLabel(plan.date) ? plan.date : "Сегодня";
  const latestHref = latestConversation ? `/workspace?conversation=${encodeURIComponent(latestConversation.id)}` : null;

  return (
    <main className={styles.page}>
      <header className={styles.heading}>
        <div>
          <p className={styles.kicker}>Подготовка к ЕГЭ по информатике</p>
          <h1>С чего начнём?</h1>
          <p className={styles.lead}>Выберите задачу, продолжите разбор или повторите то, что вызвало трудности.</p>
        </div>
        <Link className={styles.headingAction} href="/tasks">
          <Icon name="plus" size={17} />
          Выбрать задание
        </Link>
      </header>

      {error ? (
        <section className={styles.errorNotice} role="status">
          <div className={styles.errorIcon}><Icon name="warning" size={17} /></div>
          <div>
            <strong>Не всё загрузилось</strong>
            <p>{error}</p>
          </div>
          <div className={styles.errorActions}>
            <button className={styles.quietButton} onClick={() => setLoadAttempt((current) => current + 1)} type="button">
              Повторить загрузку
            </button>
            <Link className={styles.errorLink} href="/tasks">Открыть банк заданий</Link>
          </div>
        </section>
      ) : null}

      <section className={styles.startSection} aria-labelledby="start-title">
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.eyebrow}>Начать</p>
            <h2 id="start-title">Выберите действие</h2>
          </div>
          <span className={styles.sectionHint}>Небольшой шаг уже считается занятием</span>
        </div>
        <div className={styles.actionGrid}>
          <Link className={`${styles.actionCard} ${styles.actionCardFeatured}`} data-testid="choose-task" href="/tasks">
            <span className={styles.actionIcon}><Icon name="book" size={19} /></span>
            <span className={styles.actionCopy}>
              <strong>Выбрать задание</strong>
              <small>Открыть банк заданий КЕГЭ и начать новый разбор</small>
            </span>
            <Icon className={styles.actionArrow} name="chevron-right" size={17} />
          </Link>
          <Link className={styles.actionCard} data-testid="review-errors" href="/errors">
            <span className={styles.actionIcon}><Icon name="refresh" size={19} /></span>
            <span className={styles.actionCopy}>
              <strong>Повторить ошибки</strong>
              <small>Вернуться к темам, которые стоит закрепить</small>
            </span>
            <Icon className={styles.actionArrow} name="chevron-right" size={17} />
          </Link>
        </div>
      </section>

      <div className={styles.contentGrid}>
        <section className={styles.section} aria-labelledby="plan-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Сегодня · {planDate}</p>
              <h2 id="plan-title">{planTitle}</h2>
            </div>
            <div className={styles.sectionActions}>
              <button className={styles.quietButton} disabled={rebuilding} onClick={() => void handleRebuildPlan()} type="button">
                <Icon name="refresh" size={14} />
                {rebuilding ? "Обновляем…" : "Обновить план"}
              </button>
              <Link className={styles.textLink} href="/skills">Открыть повторение <Icon name="chevron-right" size={14} /></Link>
            </div>
          </div>

          {loading && !plan ? <PlanSkeleton /> : null}
          {!loading && plan?.items.length ? (
            <div className={styles.planList}>
              {plan.items.map((item) => {
                const isDone = item.done;
                return (
                  <label className={`${styles.planItem} ${isDone ? styles.planItemDone : ""}`} key={item.id}>
                    <input
                      checked={isDone}
                      disabled
                      aria-label={`${readableLabel(item.label, "Задание")}: ${isDone ? "выполнено" : "ещё не выполнено"}`}
                      type="checkbox"
                    />
                    <span className={styles.fakeCheck}><Icon name="check" size={13} /></span>
                    <span className={styles.planCopy}>
                      <strong>{readableLabel(item.label, "Задание для тренировки")}</strong>
                      <small>{readableLabel(item.detail, "Следующий шаг в подготовке")}</small>
                    </span>
                    <Icon className={styles.itemArrow} name="chevron-right" size={15} />
                  </label>
                );
              })}
            </div>
          ) : null}
          {plan?.items.length ? <p className={styles.completionHint}>Выполнение отмечается автоматически по результатам решений.</p> : null}
          {!loading && !plan?.items.length ? (
            <div className={styles.planEmpty}>
              <div className={styles.emptyIcon}><Icon name="target" size={19} /></div>
              <div>
                <strong>Начните с одной задачи — план появится по вашим результатам</strong>
                <p>После первых решений здесь появятся следующие шаги для спокойной подготовки.</p>
              </div>
              <Link className={styles.smallPrimary} href="/tasks">Выбрать первую задачу</Link>
            </div>
          ) : null}
        </section>

        <section className={styles.section} aria-labelledby="review-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Закрепить</p>
              <h2 id="review-title">Повторение</h2>
            </div>
            <Link aria-label="Открыть все ошибки" className={styles.roundLink} href="/errors"><Icon name="chevron-right" size={16} /></Link>
          </div>
          {review.length ? (
            <div className={styles.reviewList}>
              {review.slice(0, 3).map((item) => (
                <Link className={styles.reviewItem} href="/errors" key={item.id}>
                  <span className={styles.reviewLine}>{item.task_line ?? "—"}</span>
                  <span className={styles.reviewCopy}>
                    <strong>{readableLabel(item.label, "Задание для повторения")}</strong>
                    <small>{readableLabel(item.reason, "Закрепить тему ещё раз")}</small>
                  </span>
                  <span className={styles.reviewDue}>{readableLabel(item.due_label, "Позже")}</span>
                </Link>
              ))}
            </div>
          ) : (
            <div className={styles.reviewEmpty}>
              <p>Пока нет заданий для повторения.</p>
              <span>Ошибки появятся после решений и помогут выбрать следующий шаг.</span>
            </div>
          )}
        </section>
      </div>

      <section className={`${styles.continueSection} ${latestConversation ? styles.continueReady : ""}`} aria-labelledby="continue-title">
        <div className={styles.continueIcon}><Icon name={latestConversation ? "chat" : "spark"} size={20} /></div>
        <div className={styles.continueCopy}>
          <p className={styles.eyebrow}>Продолжить</p>
          <h2 id="continue-title">{latestConversation ? latestConversation.title : "Ваш первый разбор"}</h2>
          <p>{latestConversation ? "Вернитесь к последнему разбору и продолжите с того места, где остановились." : "Выберите задачу из банка, чтобы начать учиться вместе с наставником."}</p>
        </div>
        <Link className={styles.continueAction} href={latestHref ?? "/tasks"}>
          {latestConversation ? "Продолжить разбор" : "Выбрать задание"}
          <Icon name="chevron-right" size={16} />
        </Link>
      </section>
    </main>
  );
}

function PlanSkeleton() {
  return (
    <div aria-label="Загружаем план" className={styles.planSkeleton} role="status">
      <span /><span /><span />
    </div>
  );
}

function timestamp(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function isMachineLabel(value: string | null | undefined): boolean {
  if (!value) return false;
  const normalized = value.trim().toLowerCase();
  return /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(normalized);
}

function readablePlanTitle(value: string | null | undefined): string {
  return value && !isMachineLabel(value) ? value : "План на сегодня";
}

function readableLabel(value: string | null | undefined, fallback: string): string {
  return value && !isMachineLabel(value) ? value : fallback;
}
