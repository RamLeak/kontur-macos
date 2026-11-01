"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { getConversations, updateConversation } from "@/lib/api";
import {
  HISTORY_CHANGED_EVENT,
  notifyHistoryChanged,
  sortConversationsByUpdated,
} from "@/lib/history-events";
import type { ConversationSummary } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";
import styles from "./archived-chats.module.css";

type ArchivedConversation = ConversationSummary & {
  archived?: boolean;
  pinned?: boolean;
};

export function ArchivedChats() {
  const [conversations, setConversations] = useState<ArchivedConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRequestRef = useRef(0);
  const mountedRef = useRef(false);

  const loadArchived = useCallback(async () => {
    if (!mountedRef.current) return;
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    try {
      const allConversations = await getConversations() as ArchivedConversation[];
      if (!mountedRef.current || requestId !== loadRequestRef.current) return;
      setConversations(sortConversationsByUpdated(allConversations.filter((conversation) => conversation.archived === true)));
      setError(null);
    } catch (reason: unknown) {
      if (!mountedRef.current || requestId !== loadRequestRef.current) return;
      setError(readableError(reason, "Не удалось загрузить архив. Повторите попытку."));
    } finally {
      if (mountedRef.current && requestId === loadRequestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadArchived();
    const handleHistoryChanged = () => { void loadArchived(); };
    window.addEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
    return () => {
      mountedRef.current = false;
      loadRequestRef.current += 1;
      window.removeEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
    };
  }, [loadArchived]);

  async function restoreConversation(conversation: ArchivedConversation) {
    if (restoringId) return;
    loadRequestRef.current += 1;
    setLoading(false);
    setRestoringId(conversation.id);
    setError(null);
    setConversations((current) => current.filter((item) => item.id !== conversation.id));
    try {
      await updateConversation(conversation.id, { archived: false });
      notifyHistoryChanged();
    } catch (reason: unknown) {
      if (!mountedRef.current) return;
      setConversations((current) => sortConversationsByUpdated([...current, conversation]));
      setError(readableError(reason, "Не удалось восстановить разбор. Попробуйте ещё раз."));
    } finally {
      if (mountedRef.current) setRestoringId(null);
    }
  }

  const content = useMemo(() => {
    if (loading) return <p className={styles.state}>Загружаем архив…</p>;
    if (error) return (
      <div aria-live="polite" className={styles.stateError} role="alert">
        <p>{error}</p>
        <button className={styles.retry} onClick={() => void loadArchived()} type="button">Повторить загрузку</button>
      </div>
    );
    if (!conversations.length) return <p className={styles.state}>Архив пуст. Здесь появятся разборы, которые вы отправите в архив.</p>;
    return (
      <div className={styles.list}>
        {conversations.map((conversation) => (
          <article className={styles.row} key={conversation.id}>
            <span className={styles.icon} aria-hidden="true"><Icon name={conversation.task_line ? "file" : "chat"} size={16} /></span>
            <div className={styles.copy}>
              <strong title={conversation.title}>{conversation.title}</strong>
              <small>{conversation.project_id ? "С проектом" : "Без проекта"}</small>
            </div>
            <button className={styles.restore} disabled={restoringId === conversation.id} onClick={() => void restoreConversation(conversation)} type="button">
              <Icon name="refresh" size={14} />
              {restoringId === conversation.id ? "Восстанавливаем…" : "Восстановить"}
            </button>
          </article>
        ))}
      </div>
    );
  }, [conversations, error, loadArchived, loading, restoringId]);

  return (
    <section aria-labelledby="archived-chats-title" className={styles.section}>
      <div className={styles.heading}>
        <div>
          <span className="eyebrow">История разборов</span>
          <h2 id="archived-chats-title">Архив</h2>
          <p>Архив скрывает разборы из навигации, но не удаляет их. Восстановите нужный разбор в один клик.</p>
        </div>
        <Icon aria-hidden="true" className={styles.headingIcon} name="download" size={20} />
      </div>
      {content}
    </section>
  );
}
