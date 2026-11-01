"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Composer } from "@/components/composer";
import { Icon } from "@/components/icon";
import { ProjectPicker } from "@/components/project-picker";
import { createConversation, getConversations, getMessages, getProjects } from "@/lib/api";
import { HISTORY_CHANGED_EVENT, notifyHistoryChanged } from "@/lib/history-events";
import { readableError } from "@/lib/ui-copy";
import type { Message, Project } from "@/lib/types";
import styles from "./chat-view.module.css";

export function ChatView({ initialConversationId = null, projectId = null }: { initialConversationId?: string | null; projectId?: string | null }) {
  const [conversationId, setConversationId] = useState(initialConversationId);
  const [title, setTitle] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentProjectId, setCurrentProjectId] = useState(projectId);
  const [loading, setLoading] = useState(Boolean(initialConversationId));
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [composerKey, setComposerKey] = useState(0);
  const [draft, setDraft] = useState<{ text: string; key: number }>();
  const createdHere = useRef<string | null>(null);
  const internalProjectSwitchRef = useRef<{ fromRouteKey: string; toRouteKey: string } | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const routeKey = JSON.stringify([initialConversationId, projectId]);
  const viewScope = useRef({ route: routeKey, generation: 0 });
  if (viewScope.current.route !== routeKey) {
    const internalSwitch = internalProjectSwitchRef.current;
    if (internalSwitch?.fromRouteKey === viewScope.current.route && internalSwitch.toRouteKey === routeKey) {
      // The App Router has reflected our own picker change. The generation
      // was already advanced when the picker changed, so don't invalidate a
      // create/reply that started after that local transition.
      viewScope.current.route = routeKey;
    } else {
      viewScope.current = { route: routeKey, generation: viewScope.current.generation + 1 };
    }
  }
  const generation = viewScope.current.generation;
  const isCurrentView = () => mounted.current && viewScope.current.generation === generation;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (initialConversationId && createdHere.current === initialConversationId) return;
    const currentRouteKey = JSON.stringify([initialConversationId, projectId]);
    const internalSwitch = internalProjectSwitchRef.current;
    if (!initialConversationId && internalSwitch?.toRouteKey === currentRouteKey) {
      internalProjectSwitchRef.current = null;
      return;
    }
    let cancelled = false;
    createdHere.current = null;
    setConversationId(initialConversationId);
    setCurrentProjectId(projectId);
    setMessages([]);
    setTitle("");
    setDraft(undefined);
    setComposerKey((value) => value + 1);
    setLoading(Boolean(initialConversationId));
    setError(null);
    Promise.all([getProjects(), initialConversationId ? getMessages(initialConversationId) : Promise.resolve([]), initialConversationId ? getConversations() : Promise.resolve([])])
      .then(([nextProjects, nextMessages, conversations]) => {
        if (cancelled) return;
        setProjects(nextProjects.filter((project) => !project.archived));
        setMessages(nextMessages);
        const conversation = conversations.find((item) => item.id === initialConversationId);
        if (conversation) { setTitle(conversation.title); setCurrentProjectId(conversation.project_id); }
      })
      .catch((reason: unknown) => { if (!cancelled) setError(readableError(reason, "Не удалось загрузить чат. Попробуйте ещё раз.")); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [initialConversationId, projectId, attempt]);

  useEffect(() => {
    let cancelled = false;
    const refreshProjects = () => {
      getProjects()
        .then((nextProjects) => {
          if (!cancelled) setProjects(nextProjects.filter((project) => !project.archived));
        })
        .catch((reason: unknown) => {
          if (!cancelled) setError(readableError(reason, "Не удалось обновить список проектов."));
        });
    };
    window.addEventListener(HISTORY_CHANGED_EVENT, refreshProjects);
    return () => {
      cancelled = true;
      window.removeEventListener(HISTORY_CHANGED_EVENT, refreshProjects);
    };
  }, []);

  useEffect(() => { feed.current?.scrollTo({ top: feed.current.scrollHeight }); }, [messages]);

  const project = projects.find((item) => item.id === currentProjectId);
  const bankHref = currentProjectId ? `/tasks?project=${encodeURIComponent(currentProjectId)}` : "/tasks";
  const welcome = !conversationId && !messages.length;

  function selectProject(nextProjectId: string | null) {
    if (!isCurrentView()) return;
    if (nextProjectId === currentProjectId) return;
    const fromRouteKey = viewScope.current.route;
    const toRouteKey = JSON.stringify([initialConversationId, nextProjectId]);
    internalProjectSwitchRef.current = { fromRouteKey, toRouteKey };
    // Invalidate callbacks from a create/reply that started before the local
    // folder switch, while keeping the Composer mounted and stateful.
    viewScope.current.generation += 1;
    setCurrentProjectId(nextProjectId);
    const params = new URLSearchParams(window.location.search);
    if (nextProjectId) params.set("project", nextProjectId);
    else params.delete("project");
    const query = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
  }

  async function startConversation(nextTitle: string) {
    const created = await createConversation({ title: nextTitle, project_id: currentProjectId });
    notifyHistoryChanged();
    if (!isCurrentView()) throw new Error("Чат открыт в другой рабочей области.");
    createdHere.current = created.id;
    setTitle(created.title);
    return created.id;
  }

  function conversationReady(id: string) {
    if (!isCurrentView()) return;
    setConversationId(id);
    const params = new URLSearchParams();
    params.set("conversation", id);
    if (currentProjectId) params.set("project", currentProjectId);
    // Canonicalizing a chat created here must keep its pending response alive.
    viewScope.current.route = JSON.stringify([id, currentProjectId]);
    window.history.replaceState(null, "", `/?${params.toString()}`);
    notifyHistoryChanged();
  }

  return (
    <main className={`${styles.page} ${welcome ? styles.isWelcome : ""}`}>
      <header className={styles.header}>
        {!welcome ? <span className={styles.title}><Icon name={project ? "folder" : "chat"} size={18} /><span>{title || "Новый чат"}</span></span> : <span />}
        <Link className="icon-button compact" aria-label="Открыть банк заданий" href={bankHref} title="Банк заданий"><Icon name="book" size={19} /></Link>
      </header>
      <div className={styles.content} ref={feed}>
        {error ? <div className={styles.notice} role="alert"><p>{error}</p><button className="text-button" onClick={() => setAttempt((value) => value + 1)} type="button">Повторить загрузку</button></div> : null}
        {welcome ? <section className={styles.welcome} aria-labelledby="welcome-title">
          <Icon className={styles.emblem} name="book" size={50} />
          <h1 id="welcome-title">Что разберём {project ? <>в <span className={styles.projectName}>{project.name}</span>?</> : "сегодня?"}</h1>
          <div className={styles.suggestions}>
            <Link className={styles.card} data-testid="welcome-tasks" href={bankHref}><Icon name="search" size={20} /><span>Выберите задание<br />из банка КЕГЭ</span></Link>
            <button className={styles.card} onClick={() => setDraft({ text: "Объясни мне тему по информатике простыми словами. ", key: Date.now() })} type="button"><Icon name="book" size={20} /><span>Разберитесь<br />в сложной теме</span></button>
            <button className={styles.card} onClick={() => setDraft({ text: "Помоги найти ошибку в моём коде. ", key: Date.now() })} type="button"><Icon name="code" size={20} /><span>Проверьте код<br />и найдите ошибку</span></button>
            <Link className={styles.card} href="/exams"><Icon name="exam" size={20} /><span>Попробуйте силы<br />на пробном ЕГЭ</span></Link>
          </div>
        </section> : <section className={styles.messages} aria-label="Разговор с репетитором" aria-busy={loading}>
          {loading ? <p className={styles.empty}>Загружаем сообщения…</p> : !messages.length ? <p className={styles.empty}>Задайте вопрос в поле внизу — начнём разбор.</p> : null}
          {messages.map((message) => <article className={`${styles.message} ${message.role === "user" ? styles.userMessage : styles.assistantMessage}`} key={message.id}>
            <span className="sr-only">{message.role === "user" ? "Вы" : "Наставник"}</span>
            <p>{message.content}</p>
            {message.model ? <small>{message.model}</small> : null}
          </article>)}
        </section>}
      </div>
      <div className={styles.composeArea}>
        {welcome ? <ProjectPicker projects={projects} value={currentProjectId} onChange={selectProject} /> : null}
        <Composer key={composerKey} draftProjectId={currentProjectId} conversationId={conversationId ?? undefined} draft={draft} onStartConversation={startConversation} onConversationReady={conversationReady} placeholder="Спросите об информатике…" onMessage={(message) => { if (isCurrentView()) setMessages((items) => [...items, message]); notifyHistoryChanged(); }} />
      </div>
    </main>
  );
}
