"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { createPortal } from "react-dom";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, FormEvent, MouseEvent, ReactNode } from "react";
import { Icon, type IconName } from "@/components/icon";
import { createProject, DEMO_MODE, getConversations, getProjects, updateConversation } from "@/lib/api";
import {
  getActiveProjectId,
  HISTORY_CHANGED_EVENT,
  isActiveConversation,
  isPinnedConversation,
  isRecentConversation,
  notifyHistoryChanged,
  sortConversationsByUpdated,
} from "@/lib/history-events";
import type { ConversationSummary, NavKey, Project } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";
import styles from "./sidebar.module.css";
import railStyles from "./sidebar-rail.module.css";

const navigation: Array<{ key: NavKey; href: string; label: string; icon: IconName }> = [
  { key: "bank", href: "/tasks", label: "Банк заданий", icon: "book" },
  { key: "exams", href: "/exams", label: "Пробники", icon: "exam" },
  { key: "errors", href: "/errors", label: "Ошибки", icon: "warning" },
  { key: "bookmarks", href: "/bookmarks", label: "Закладки", icon: "bookmark" },
  { key: "analytics", href: "/analytics", label: "Прогресс", icon: "chart" },
];

type SidebarConversation = ConversationSummary & { archived?: boolean; pinned?: boolean };
type SidebarProject = Project & { archived?: boolean; pinned?: boolean };

interface ActionMenuState {
  anchor: HTMLButtonElement;
  conversationId: string;
  left: number;
  top: number;
}

export function Sidebar(props: {
  active: NavKey;
  open: boolean;
  collapsed: boolean;
  rail?: boolean;
  onToggle: () => void;
  onClose: () => void;
  onSignOut: () => Promise<void>;
  signingOut: boolean;
}) {
  return (
    <Suspense fallback={<aside aria-hidden="true" className="primary-sidebar" />}>
      <SidebarContent {...props} />
    </Suspense>
  );
}

function SidebarContent({
  active,
  open,
  collapsed,
  rail = false,
  onToggle,
  onClose,
  onSignOut,
  signingOut,
}: {
  active: NavKey;
  open: boolean;
  collapsed: boolean;
  rail?: boolean;
  onToggle: () => void;
  onClose: () => void;
  onSignOut: () => Promise<void>;
  signingOut: boolean;
}) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const locationSearch = searchParams?.toString() ?? "";
  const returnTo=pathname==="/settings"?searchParams?.get("returnTo")??"/tasks":pathname+(locationSearch?"?"+locationSearch:"");
  const settingsHref=(section?:string)=>"/settings?"+new URLSearchParams({...section?{section}:{},returnTo}).toString();
  const currentConversationId = searchParams?.get("conversation") ?? null;
  const projectIdFromUrl = searchParams?.get("project") ?? null;
  const [projects, setProjects] = useState<SidebarProject[]>([]);
  const [conversations, setConversations] = useState<SidebarConversation[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [projectsExpanded, setProjectsExpanded] = useState(true);
  const [pinnedExpanded, setPinnedExpanded] = useState(true);
  const [historyExpanded, setHistoryExpanded] = useState(true);
  const [historyShowAll, setHistoryShowAll] = useState(false);
  const [expandedProjectIds, setExpandedProjectIds] = useState<Set<string>>(() => new Set());
  const [conversationMutationId, setConversationMutationId] = useState<string | null>(null);
  const [actionMenu, setActionMenu] = useState<ActionMenuState | null>(null);
  const [renameConversationId, setRenameConversationId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameSubmitting, setRenameSubmitting] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectSubmitting, setProjectSubmitting] = useState(false);
  const [mobile, setMobile] = useState(false);
  const projectNameInputRef = useRef<HTMLInputElement>(null);
  const projectDialogTriggerRef = useRef<HTMLButtonElement>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renameDialogTriggerRef = useRef<HTMLButtonElement | null>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const actionMenuRef = useRef<HTMLDivElement>(null);
  const firstActionRef = useRef<HTMLButtonElement>(null);
  const loadRequestRef = useRef(0);

  const loadHistory = useCallback(async () => {
    const requestId = ++loadRequestRef.current;
    setHistoryLoading(true);
    try {
      const [nextProjects, nextConversations] = await Promise.all([getProjects(), getConversations()]);
      if (requestId !== loadRequestRef.current) return;
      setProjects(nextProjects as SidebarProject[]);
      setConversations(nextConversations as SidebarConversation[]);
      setError(null);
    } catch (reason: unknown) {
      if (requestId !== loadRequestRef.current) return;
      if (!DEMO_MODE) setError(readableError(reason, "Не удалось загрузить историю. Обновите страницу."));
    } finally {
      if (requestId === loadRequestRef.current) setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadHistory();
    const handleHistoryChanged = () => { void loadHistory(); };
    window.addEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
    return () => window.removeEventListener(HISTORY_CHANGED_EVENT, handleHistoryChanged);
  }, [loadHistory, pathname, locationSearch]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const update = () => setMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if ((!mobile && !rail) || !open) return;
    const previous = document.activeElement as HTMLElement | null;
    const focusTimer = requestAnimationFrame(() => sidebarRef.current?.querySelector<HTMLButtonElement>(".sidebar-close")?.focus());
    return () => { cancelAnimationFrame(focusTimer); if (previous?.isConnected) previous.focus(); };
  }, [mobile, open, rail]);

  useEffect(() => {
    if (projectDialogOpen) projectNameInputRef.current?.focus();
  }, [projectDialogOpen]);

  useEffect(() => {
    if (renameConversationId) renameInputRef.current?.focus();
  }, [renameConversationId]);

  useEffect(() => {
    if (actionMenu) firstActionRef.current?.focus();
  }, [actionMenu?.conversationId]);

  useEffect(() => {
    if (!actionMenu) return;
    const closeFromOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (actionMenuRef.current?.contains(target) || actionMenu.anchor.contains(target)) return;
      setActionMenu(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      const anchor = actionMenu.anchor;
      setActionMenu(null);
      requestAnimationFrame(() => anchor.focus());
    };
    const closeOnViewportChange = () => setActionMenu(null);
    window.addEventListener("pointerdown", closeFromOutside);
    window.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      window.removeEventListener("pointerdown", closeFromOutside);
      window.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [actionMenu]);

  const currentConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === currentConversationId) ?? null,
    [conversations, currentConversationId],
  );
  const currentConversationProjectId = currentConversation?.project_id ?? null;
  const activeProjectId = getActiveProjectId(projectIdFromUrl, currentConversationProjectId);
  const activeConversations = useMemo(
    () => sortConversationsByUpdated(conversations.filter(isActiveConversation)),
    [conversations],
  );
  const pinnedConversations = useMemo(() => activeConversations.filter(isPinnedConversation), [activeConversations]);
  const recentConversations = useMemo(() => activeConversations.filter(isRecentConversation), [activeConversations]);
  const visibleRecentConversations = historyShowAll ? recentConversations : recentConversations.slice(0, 5);
  const visibleProjects = useMemo(() => projects.filter((project) => project.archived !== true), [projects]);
  const newChatHref = activeProjectId ? `/chat?project=${encodeURIComponent(activeProjectId)}` : "/chat";
  const renameConversation = renameConversationId ? conversations.find((conversation) => conversation.id === renameConversationId) ?? null : null;
  const actionConversation = actionMenu ? conversations.find((conversation) => conversation.id === actionMenu.conversationId) ?? null : null;

  function closeSidebar() {
    onClose();
  }

  function openRailDrawer(section?: "projects" | "history") {
    if (section === "projects") setProjectsExpanded(true);
    if (section === "history") setHistoryExpanded(true);
    if (!open) onToggle();
  }

  useEffect(() => {
    if (!currentConversationProjectId) return;
    setExpandedProjectIds((current) => {
      if (current.has(currentConversationProjectId)) return current;
      return new Set(current).add(currentConversationProjectId);
    });
  }, [currentConversationProjectId]);

  async function mutateConversation(
    conversationId: string,
    patch: { title?: string | null; project_id?: string | null; pinned?: boolean; archived?: boolean },
    fallbackMessage: string,
    onFailure?: (message: string) => void,
  ): Promise<boolean> {
    const previous = conversations.find((conversation) => conversation.id === conversationId);
    if (!previous || conversationMutationId) return false;
    const optimistic: SidebarConversation = {
      ...previous,
      ...patch,
      title: patch.title === undefined ? previous.title : patch.title ?? "Без названия",
      project_id: patch.project_id === undefined ? previous.project_id : patch.project_id,
    };
    setConversationMutationId(conversationId);
    setConversations((current) => current.map((conversation) => conversation.id === conversationId ? optimistic : conversation));
    try {
      const saved = await updateConversation(conversationId, patch);
      const normalized = saved as SidebarConversation;
      setConversations((current) => current.map((conversation) => conversation.id === conversationId ? {
        ...conversation,
        ...normalized,
        archived: normalized.archived ?? conversation.archived ?? optimistic.archived ?? false,
        pinned: normalized.pinned ?? conversation.pinned ?? optimistic.pinned ?? false,
      } : conversation));
      setError(null);
      notifyHistoryChanged();
      return true;
    } catch (reason: unknown) {
      setConversations((current) => current.map((conversation) => conversation.id === conversationId ? previous : conversation));
      const message = readableError(reason, fallbackMessage);
      if (onFailure) onFailure(message);
      else setError(message);
      return false;
    } finally {
      setConversationMutationId(null);
    }
  }

  function archiveConversation(conversation: SidebarConversation) {
    if (conversationMutationId) return;
    void mutateConversation(
      conversation.id,
      { archived: true },
      "Не удалось отправить разбор в архив. Попробуйте ещё раз.",
    );
  }

  function openActionMenu(event: MouseEvent<HTMLButtonElement>, conversationId: string) {
    event.stopPropagation();
    if (actionMenu?.conversationId === conversationId) {
      const anchor = actionMenu.anchor;
      setActionMenu(null);
      requestAnimationFrame(() => anchor.focus());
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const width = 244;
    const margin = 10;
    const left = Math.max(margin, Math.min(rect.right - width, window.innerWidth - width - margin));
    const estimatedHeight = 230;
    const top = rect.bottom + estimatedHeight <= window.innerHeight - margin
      ? rect.bottom + 6
      : Math.max(margin, rect.top - estimatedHeight - 6);
    setActionMenu({ anchor: event.currentTarget, conversationId, left, top });
  }

  function toggleProjectExpanded(projectId: string) {
    setExpandedProjectIds((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }

  function openProjectDialog() {
    setActionMenu(null);
    setError(null);
    setProjectName("");
    setProjectDialogOpen(true);
  }

  function closeProfileMenu(event: MouseEvent<HTMLAnchorElement>) {
    event.currentTarget.closest("details")?.removeAttribute("open");
    closeSidebar();
  }

  function closeProjectDialog() {
    if (projectSubmitting) return;
    setProjectDialogOpen(false);
    setProjectName("");
    requestAnimationFrame(() => projectDialogTriggerRef.current?.focus());
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = projectName.trim();
    if (!name) {
      setError("Введите название проекта");
      projectNameInputRef.current?.focus();
      return;
    }
    if (projectSubmitting) return;
    setProjectSubmitting(true);
    try {
      const project = await createProject({ name });
      setProjects((current) => [...current, project as SidebarProject]);
      setError(null);
      setProjectDialogOpen(false);
      setProjectName("");
      notifyHistoryChanged();
      requestAnimationFrame(() => projectDialogTriggerRef.current?.focus());
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось создать проект. Попробуйте ещё раз."));
    } finally {
      setProjectSubmitting(false);
    }
  }

  function startRename(conversation: SidebarConversation, trigger: HTMLButtonElement) {
    setActionMenu(null);
    renameDialogTriggerRef.current = trigger;
    setRenameError(null);
    setRenameTitle(conversation.title);
    setRenameConversationId(conversation.id);
  }

  function closeRenameDialog() {
    if (renameSubmitting) return;
    setRenameConversationId(null);
    setRenameTitle("");
    setRenameError(null);
    requestAnimationFrame(() => renameDialogTriggerRef.current?.focus());
  }

  async function handleRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const conversation = renameConversation;
    const title = renameTitle.trim();
    if (!conversation) return;
    if (!title) {
      setRenameError("Введите название разбора");
      renameInputRef.current?.focus();
      return;
    }
    if (renameSubmitting) return;
    setRenameSubmitting(true);
    const saved = await mutateConversation(
      conversation.id,
      { title },
      "Не удалось переименовать разбор. Попробуйте ещё раз.",
      setRenameError,
    );
    setRenameSubmitting(false);
    if (saved) closeRenameDialog();
  }

  function closeActionMenuAndRestoreFocus() {
    const anchor = actionMenu?.anchor;
    setActionMenu(null);
    requestAnimationFrame(() => anchor?.focus());
  }

  function renderActionMenu() {
    if (!actionMenu || !actionConversation) return null;
    const menuStyle: CSSProperties = { left: actionMenu.left, top: actionMenu.top };
    const move = (projectId: string | null) => {
      closeActionMenuAndRestoreFocus();
      void mutateConversation(
        actionConversation.id,
        { project_id: projectId },
        "Не удалось переместить разбор. Попробуйте ещё раз.",
      );
    };
    const pin = () => {
      closeActionMenuAndRestoreFocus();
      void mutateConversation(
        actionConversation.id,
        { pinned: actionConversation.pinned !== true },
        "Не удалось изменить закрепление разбора. Попробуйте ещё раз.",
      );
    };
    const archive = () => {
      closeActionMenuAndRestoreFocus();
      void mutateConversation(
        actionConversation.id,
        { archived: true },
        "Не удалось отправить разбор в архив. Попробуйте ещё раз.",
      );
    };
    return createPortal(
      <div
        aria-label={`Действия: ${actionConversation.title}`}
        className={styles.actionMenu}
        onKeyDown={(event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          closeActionMenuAndRestoreFocus();
        }}
        ref={actionMenuRef}
        role="menu"
        style={menuStyle}
      >
        <button className={styles.actionMenuItem} onClick={() => startRename(actionConversation, actionMenu.anchor)} ref={firstActionRef} role="menuitem" type="button">
          <Icon name="pen" size={15} />
          <span>Переименовать</span>
        </button>
        <label className={styles.actionMenuField}>
          <span>Переместить в</span>
          <select aria-label={`Переместить ${actionConversation.title}`} disabled={conversationMutationId === actionConversation.id} onChange={(event) => move(event.target.value || null)} value={actionConversation.project_id ?? ""}>
            <option value="">Без проекта</option>
            {visibleProjects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </label>
        <button className={styles.actionMenuItem} disabled={conversationMutationId === actionConversation.id} onClick={pin} role="menuitem" type="button">
          <Icon name="spark" size={15} />
          <span>{actionConversation.pinned === true ? "Открепить" : "Закрепить"}</span>
        </button>
        <button className={`${styles.actionMenuItem} ${styles.actionMenuDanger}`} disabled={conversationMutationId === actionConversation.id} onClick={archive} role="menuitem" type="button">
          <Icon name="download" size={15} />
          <span>В архив</span>
        </button>
      </div>,
      document.body,
    );
  }

  return (
    <aside
      aria-hidden={!rail && mobile && !open ? true : undefined}
      className={`primary-sidebar ${styles.sidebar} ${open ? "is-open" : ""} ${rail ? railStyles.railSidebar : ""}`}
      inert={!rail && mobile && !open}
      onKeyDown={(event) => {
        if ((!mobile && !rail) || !open || projectDialogOpen || renameConversationId || actionMenu) return;
        if (event.key === "Escape") { event.preventDefault(); closeSidebar(); return; }
        if (event.key !== "Tab") return;
        const items = [...event.currentTarget.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), select:not(:disabled), summary, [tabindex="0"]')].filter((item) => item.getClientRects().length > 0);
        const first = items[0];
        const last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}
      ref={sidebarRef}
    >
      {rail ? (
        <RailNavigation
          active={active}
          locationSearch={locationSearch}
          newChatHref={newChatHref}
          onClose={closeSidebar}
          onOpenDrawer={openRailDrawer}
          onSignOut={onSignOut}
          settingsHref={settingsHref}
          signingOut={signingOut}
          open={open}
        />
      ) : null}
      <div
        aria-hidden={rail && !open ? true : undefined}
        className={rail ? `${railStyles.sidebarContent} ${railStyles.railDrawer}` : railStyles.sidebarContent}
        data-rail-drawer={rail ? "true" : undefined}
        inert={rail ? !open : undefined}
        id="kontur-navigation-drawer"
      >
      <div className="sidebar-top">
        <Link className="brand-mark" href="/" onClick={closeSidebar}><span>Контур</span></Link>
        <button aria-label={collapsed ? "Развернуть навигацию" : "Свернуть навигацию"} className="icon-button sidebar-collapse" onClick={onToggle} type="button" title={collapsed ? "Развернуть навигацию" : "Свернуть навигацию"}><Icon name="panel" size={18} /></button>
        <button aria-label="Закрыть навигацию" className="icon-button sidebar-close" onClick={closeSidebar} type="button"><Icon name="close" size={17} /></button>
      </div>

      <Link className={`new-analysis ${styles.newAnalysis}`} href={newChatHref} onClick={closeSidebar} title="Новый разбор"><Icon name="plus" size={19} /><span>Новый разбор</span></Link>

      <div className="sidebar-scroll">
        <nav aria-label="Основная навигация" className="primary-nav">
          {navigation.map((item) => (
            <Link aria-current={active === item.key && (item.key !== "today" || locationSearch.includes("view=today")) ? "page" : undefined} className={`nav-item ${active === item.key && (item.key !== "today" || locationSearch.includes("view=today")) ? "is-active" : ""}`} href={item.href} key={item.key} onClick={closeSidebar} title={item.label}>
              <Icon name={item.icon} size={18} />
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-divider" />

        <SidebarSectionHeader
          expanded={projectsExpanded}
          label="Проекты"
          onToggle={() => setProjectsExpanded((value) => !value)}
          action={<button aria-haspopup="dialog" aria-label="Создать проект" className="section-action" onClick={openProjectDialog} ref={projectDialogTriggerRef} type="button"><Icon name="plus" size={16} /></button>}
        />
        {projectsExpanded ? (
          <div className={`sidebar-list ${styles.projectList}`}>
            {error ? <p className="sidebar-error">{error}</p> : null}
            {historyLoading && !visibleProjects.length ? <p className="sidebar-muted">Загружаем историю…</p> : null}
            {visibleProjects.map((project) => {
              const expanded = expandedProjectIds.has(project.id);
              const projectConversations = activeConversations.filter((conversation) => conversation.project_id === project.id);
              const activeProject = activeProjectId === project.id;
              return (
                <div className={styles.projectGroup} key={project.id}>
                  <div className={`project-row ${styles.projectRow} ${activeProject ? styles.projectRowActive : ""}`}>
                    <Link aria-current={activeProject ? "page" : undefined} className={styles.projectLink} href={`/?project=${encodeURIComponent(project.id)}`} onClick={closeSidebar} title={project.name}>
                      <Icon name="folder" size={16} />
                      <span className={styles.itemName}>{project.name}</span>
                      {project.conversation_count != null ? <span className="row-count">{project.conversation_count}</span> : null}
                    </Link>
                    <button aria-controls={`project-chats-${project.id}`} aria-expanded={expanded} aria-label={`${expanded ? "Свернуть" : "Развернуть"} проект ${project.name}`} className={styles.projectChevron} onClick={() => toggleProjectExpanded(project.id)} title={expanded ? "Свернуть разборы" : "Показать разборы"} type="button"><Icon name={expanded ? "chevron-down" : "chevron-right"} size={14} /></button>
                  </div>
                  {expanded ? (
                    <div className={styles.projectChildren} id={`project-chats-${project.id}`}>
                      {projectConversations.length ? projectConversations.map((conversation) => (
                        <ConversationRow active={currentConversationId === conversation.id} conversation={conversation} key={conversation.id} menuOpen={actionMenu?.conversationId === conversation.id} onArchive={() => archiveConversation(conversation)} onClose={closeSidebar} onOpenMenu={(event) => openActionMenu(event, conversation.id)} pending={conversationMutationId === conversation.id} />
                      )) : <p className={styles.projectEmpty}>Пока нет активных разборов</p>}
                    </div>
                  ) : null}
                </div>
              );
            })}
            {!historyLoading && !visibleProjects.length && !error ? <p className="sidebar-muted">Проектов пока нет</p> : null}
          </div>
        ) : null}

        {pinnedConversations.length ? (
          <>
            <div className="sidebar-divider sidebar-divider-tight" />
            <SidebarSectionHeader expanded={pinnedExpanded} label="Закреплённые" onToggle={() => setPinnedExpanded((value) => !value)} />
            {pinnedExpanded ? <div className={`sidebar-list ${styles.historyList}`}>{pinnedConversations.map((conversation) => <ConversationRow active={currentConversationId === conversation.id} conversation={conversation} key={conversation.id} menuOpen={actionMenu?.conversationId === conversation.id} onArchive={() => archiveConversation(conversation)} onClose={closeSidebar} onOpenMenu={(event) => openActionMenu(event, conversation.id)} pending={conversationMutationId === conversation.id} />)}</div> : null}
          </>
        ) : null}

        <div className="sidebar-divider sidebar-divider-tight" />

        <SidebarSectionHeader expanded={historyExpanded} label="Недавние" onToggle={() => setHistoryExpanded((value) => !value)} />
        {historyExpanded ? (
          <div className={`sidebar-list ${styles.historyList}`}>
            {visibleRecentConversations.length ? visibleRecentConversations.map((conversation) => <ConversationRow active={currentConversationId === conversation.id} conversation={conversation} key={conversation.id} menuOpen={actionMenu?.conversationId === conversation.id} onArchive={() => archiveConversation(conversation)} onClose={closeSidebar} onOpenMenu={(event) => openActionMenu(event, conversation.id)} pending={conversationMutationId === conversation.id} />) : <p className="sidebar-muted">{historyLoading ? "Загружаем историю…" : "История появится после первого разбора"}</p>}
            {recentConversations.length > 5 ? <button className="show-more" onClick={() => setHistoryShowAll((value) => !value)} type="button">{historyShowAll ? "Свернуть" : "Показать ещё"} <Icon name="chevron-down" size={14} /></button> : null}
          </div>
        ) : null}
      </div>

      <div className="sidebar-bottom">
        <div className="profile-row">
          <details className="profile-menu" onKeyDown={(event) => { if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); } }}>
            <summary aria-label="Открыть меню профиля" title="Профиль Даниила"><span className="profile-avatar">Д</span><span className="profile-copy"><strong>Даниил</strong></span></summary>
            <div className="profile-dropdown">
              <div className="profile-menu-heading"><strong>Даниил</strong><small>Личная подготовка · ЕГЭ 2027</small></div>
              <Link href={settingsHref("profile")} onClick={closeProfileMenu}><Icon name="user" size={17} />Профиль</Link>
              <Link href={settingsHref("samples")} onClick={closeProfileMenu}><Icon name="code" size={17} />Мои образцы кода</Link>
              <Link href={settingsHref("advanced")} onClick={closeProfileMenu}><Icon name="settings" size={17} />Настройки подключений</Link>
              <button disabled={signingOut} onClick={() => void onSignOut()} type="button"><Icon name="logout" size={17} />{signingOut ? "Сохраняем и выходим…" : "Выйти из рабочей области"}</button>
            </div>
          </details>
          <Link aria-label="Открыть настройки" aria-current={active === "settings" ? "page" : undefined} className="icon-button compact profile-settings" href={settingsHref()} onClick={closeSidebar} title="Настройки"><Icon name="settings" size={19} /></Link>
          <button aria-label="Выйти из рабочей области" className="icon-button compact profile-exit" disabled={signingOut} onClick={() => void onSignOut()} title="Выйти из рабочей области" type="button"><Icon name="logout" size={19} /></button>
        </div>
      </div>

      {projectDialogOpen ? (
        <div className="project-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeProjectDialog(); }}>
          <section aria-labelledby="create-project-title" aria-modal="true" className="project-dialog" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); closeProjectDialog(); } }} role="dialog">
            <div className="project-dialog-header"><div><span className="eyebrow">Проекты</span><h2 id="create-project-title">Создать проект</h2></div><button aria-label="Закрыть диалог создания проекта" className="icon-button compact" disabled={projectSubmitting} onClick={closeProjectDialog} type="button"><Icon name="close" size={17} /></button></div>
            <form className="project-dialog-form" onSubmit={handleCreateProject}>
              <label className="project-dialog-label" htmlFor="project-name">Название проекта</label>
              <input aria-describedby={error ? "project-dialog-error" : undefined} aria-invalid={error ? true : undefined} autoComplete="off" className="project-dialog-input" id="project-name" maxLength={120} name="name" onChange={(event) => setProjectName(event.target.value)} ref={projectNameInputRef} required value={projectName} />
              {error ? <p aria-live="polite" className="project-dialog-error" id="project-dialog-error">{error}</p> : null}
              <div className="project-dialog-actions"><button className="outline-button" disabled={projectSubmitting} onClick={closeProjectDialog} type="button">Отмена</button><button className="primary-button" disabled={projectSubmitting || !projectName.trim()} type="submit">{projectSubmitting ? "Создаём…" : "Создать"}</button></div>
            </form>
          </section>
        </div>
      ) : null}

      {renameConversationId && renameConversation ? (
        <div className={styles.renameBackdrop} onMouseDown={(event) => { if (event.target === event.currentTarget) closeRenameDialog(); }}>
          <section aria-labelledby="rename-conversation-title" aria-modal="true" className={styles.renameDialog} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); closeRenameDialog(); } }} role="dialog">
            <div className={styles.renameHeader}><div><span className="eyebrow">Разбор</span><h2 id="rename-conversation-title">Переименовать разбор</h2></div><button aria-label="Закрыть диалог переименования" className="icon-button compact" disabled={renameSubmitting} onClick={closeRenameDialog} type="button"><Icon name="close" size={17} /></button></div>
            <form className={styles.renameForm} onSubmit={handleRename}>
              <label className={styles.renameLabel} htmlFor="conversation-title">Название разбора</label>
              <input aria-describedby={renameError ? "conversation-title-error" : undefined} aria-invalid={renameError ? true : undefined} autoComplete="off" className={styles.renameInput} id="conversation-title" maxLength={160} onChange={(event) => setRenameTitle(event.target.value)} ref={renameInputRef} required value={renameTitle} />
              {renameError ? <p aria-live="polite" className={styles.renameError} id="conversation-title-error">{renameError}</p> : null}
              <div className={styles.renameActions}><button className="outline-button" disabled={renameSubmitting} onClick={closeRenameDialog} type="button">Отмена</button><button className="primary-button" disabled={renameSubmitting || !renameTitle.trim()} type="submit">{renameSubmitting ? "Сохраняем…" : "Сохранить"}</button></div>
            </form>
          </section>
        </div>
      ) : null}
      {renderActionMenu()}
      </div>
    </aside>
  );
}

function RailNavigation({
  active,
  locationSearch,
  newChatHref,
  onClose,
  onOpenDrawer,
  onSignOut,
  settingsHref,
  signingOut,
  open,
}: {
  active: NavKey;
  locationSearch: string;
  newChatHref: string;
  onClose: () => void;
  onOpenDrawer: (section?: "projects" | "history") => void;
  onSignOut: () => Promise<void>;
  settingsHref: (section?: string) => string;
  signingOut: boolean;
  open: boolean;
}) {
  return (
    <nav aria-label="Быстрая навигация" className={railStyles.rail}>
      <Link aria-label="Главная Контур" className={railStyles.railBrand} href="/" title="Главная">
        <span aria-hidden="true">К</span>
      </Link>
      <div className={railStyles.railMiddle}>
        <Link aria-label="Новый разбор" className={railStyles.railItem} href={newChatHref} onClick={onClose} title="Новый разбор">
          <Icon name="plus" size={19} />
        </Link>
        <div className={railStyles.railGroup}>
          {navigation.map((item) => {
            const isActive = active === item.key && (item.key !== "today" || locationSearch.includes("view=today"));
            return (
              <Link aria-current={isActive ? "page" : undefined} aria-label={item.label} className={`${railStyles.railItem} ${isActive ? railStyles.railItemActive : ""}`} href={item.href} key={item.key} onClick={onClose} title={item.label}>
                <Icon name={item.icon} size={19} />
              </Link>
            );
          })}
        </div>
        <div className={railStyles.railDivider} />
        <div className={railStyles.railGroup}>
          <button aria-controls="kontur-navigation-drawer" aria-expanded={open} aria-label="Проекты" className={railStyles.railItem} onClick={() => onOpenDrawer("projects")} title="Проекты" type="button">
            <Icon name="folder" size={19} />
          </button>
          <button aria-controls="kontur-navigation-drawer" aria-expanded={open} aria-label="История чатов" className={railStyles.railItem} onClick={() => onOpenDrawer("history")} title="История чатов" type="button">
            <Icon name="chat" size={19} />
          </button>
        </div>
      </div>
      <div className={railStyles.railBottom}>
        <details className={`profile-menu ${railStyles.railProfile}`} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); } }}>
          <summary aria-label="Открыть меню профиля" title="Профиль Даниила"><span className="profile-avatar">Д</span></summary>
          <div className={`profile-dropdown ${railStyles.railProfileDropdown}`}>
            <div className="profile-menu-heading"><strong>Даниил</strong><small>Личная подготовка · ЕГЭ 2027</small></div>
            <Link href={settingsHref("profile")} onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); onClose(); }}><Icon name="user" size={17} />Профиль</Link>
            <Link href={settingsHref("samples")} onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); onClose(); }}><Icon name="code" size={17} />Мои образцы кода</Link>
            <Link href={settingsHref("advanced")} onClick={(event) => { event.currentTarget.closest("details")?.removeAttribute("open"); onClose(); }}><Icon name="settings" size={17} />Настройки подключений</Link>
            <button disabled={signingOut} onClick={() => void onSignOut()} type="button"><Icon name="logout" size={17} />{signingOut ? "Сохраняем и выходим…" : "Выйти из рабочей области"}</button>
          </div>
        </details>
        <Link aria-label="Открыть настройки" className={railStyles.railItem} href={settingsHref()} onClick={onClose} title="Настройки"><Icon name="settings" size={19} /></Link>
        <button aria-label="Выйти из рабочей области" className={railStyles.railItem} disabled={signingOut} onClick={() => void onSignOut()} title="Выйти из рабочей области" type="button"><Icon name="logout" size={19} /></button>
      </div>
    </nav>
  );
}

function ConversationRow({
  active,
  conversation,
  menuOpen,
  onArchive,
  onClose,
  onOpenMenu,
  pending,
}: {
  active: boolean;
  conversation: SidebarConversation;
  menuOpen: boolean;
  onArchive: () => void;
  onClose: () => void;
  onOpenMenu: (event: MouseEvent<HTMLButtonElement>) => void;
  pending: boolean;
}) {
  return (
    <div className={`history-row ${styles.chatRow} ${active ? styles.chatRowActive : ""}`}>
      <Link aria-current={active ? "page" : undefined} className={styles.chatLink} href={`/workspace?conversation=${encodeURIComponent(conversation.id)}`} onClick={onClose} title={conversation.title}>
        <Icon name={conversation.task_line ? "file" : "chat"} size={15} />
        <span className={styles.itemName}>{conversation.title}</span>
      </Link>
      <button aria-label={`В архив: ${conversation.title}`} className={styles.archiveTrigger} disabled={pending} onClick={(event) => { event.stopPropagation(); onArchive(); }} title="В архив" type="button"><Icon name="download" size={15} /></button>
      <button aria-expanded={menuOpen} aria-haspopup="menu" aria-label={`Действия: ${conversation.title}`} className={styles.actionTrigger} disabled={pending} onClick={onOpenMenu} title="Действия" type="button"><Icon name="dots" size={16} /></button>
    </div>
  );
}

function SidebarSectionHeader({
  label,
  expanded,
  onToggle,
  action,
}: {
  label: string;
  expanded: boolean;
  onToggle: () => void;
  action?: ReactNode;
}) {
  return (
    <div className="sidebar-section-header">
      <button aria-expanded={expanded} className="section-toggle" onClick={onToggle} type="button"><span>{label}</span><Icon name={expanded ? "chevron-down" : "chevron-right"} size={14} /></button>
      {action}
    </div>
  );
}
