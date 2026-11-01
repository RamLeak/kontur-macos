export const HISTORY_CHANGED_EVENT = "kontur:history-changed";

export interface HistoryConversationLike {
  project_id: string | null;
  updated_at?: string;
  archived?: boolean;
  pinned?: boolean;
}

/**
 * Notify mounted history surfaces that a conversation or project mutation has
 * completed. This is intentionally a browser-only event: server rendering
 * should never attempt to access window or dispatch a DOM event.
 */
export function notifyHistoryChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(HISTORY_CHANGED_EVENT));
}

export function isActiveConversation(conversation: HistoryConversationLike): boolean {
  return conversation.archived !== true;
}

export function isPinnedConversation(conversation: HistoryConversationLike): boolean {
  return isActiveConversation(conversation) && conversation.pinned === true;
}

export function isRecentConversation(conversation: HistoryConversationLike): boolean {
  return isActiveConversation(conversation) && conversation.project_id === null && conversation.pinned !== true;
}

export function getActiveProjectId(
  projectIdFromUrl: string | null | undefined,
  currentConversationProjectId: string | null | undefined,
): string | null {
  const explicitProjectId = projectIdFromUrl?.trim();
  if (explicitProjectId) return explicitProjectId;
  return currentConversationProjectId ?? null;
}

export function sortConversationsByUpdated<T extends HistoryConversationLike>(conversations: T[]): T[] {
  return [...conversations].sort((left, right) => {
    const leftTime = left.updated_at ? Date.parse(left.updated_at) : 0;
    const rightTime = right.updated_at ? Date.parse(right.updated_at) : 0;
    return (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0);
  });
}
