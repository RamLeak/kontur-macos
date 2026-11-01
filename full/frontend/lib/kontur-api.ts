import {testSessions,testStatus} from "./demo-workspace";
import { DEMO_MODE, request } from "./api";
import type { TaskBookmark, TaskProgress } from "./kontur-types";

export const BOOKMARKS_CHANGED = "kontur:bookmarks-changed";
const TEST_BOOKMARK_KEY = "kontur:test-only:bookmarks";

function testBookmarks(): TaskBookmark[] {
  if (typeof window === "undefined") return [];
  try { return JSON.parse(localStorage.getItem(TEST_BOOKMARK_KEY) || "[]") as TaskBookmark[]; }
  catch { return []; }
}
export async function getBookmarks(): Promise<TaskBookmark[]> {
  if (DEMO_MODE) return testBookmarks();
  return request<TaskBookmark[]>("/bookmarks");
}
export async function getTaskProgress(revisionIds: string[]): Promise<TaskProgress[]> {
  const ids = [...new Set(revisionIds)];
  if (!ids.length) return [];
  if (DEMO_MODE) {
    const bookmarks = testBookmarks();
    return ids.map((id) => ({ task_id: bookmarks.find((b) => b.task_revision_id === id)?.task_id || "",
      task_revision_id: id, status: testStatus(id), bookmarked: bookmarks.some((b) => b.task_revision_id === id),
      solve_session_id:testSessions().findLast(s=>s.task.revision.id===id)?.id??null }));
  }
  const chunks: string[][] = [];
  for (let i = 0; i < ids.length; i += 100) chunks.push(ids.slice(i, i + 100));
  const pages = await Promise.all(chunks.map((chunk) => {
    const params = new URLSearchParams();
    chunk.forEach((id) => params.append("revision_ids", id));
    return request<TaskProgress[]>("/task-progress?" + params.toString());
  }));
  return pages.flat();
}
export async function setBookmark(taskId: string, revisionId: string, selected: boolean): Promise<TaskBookmark | null> {
  let result: TaskBookmark | null = null;
  if (DEMO_MODE) {
    const remaining = testBookmarks().filter((b) => b.task_id !== taskId);
    if (selected) { result = {task_id:taskId, task_revision_id:revisionId, created_at:new Date().toISOString()}; remaining.push(result); }
    localStorage.setItem(TEST_BOOKMARK_KEY, JSON.stringify(remaining));
  } else if (selected) {
    result = await request<TaskBookmark>("/bookmarks/" + encodeURIComponent(taskId), {
      method: "PUT", body: JSON.stringify({task_revision_id: revisionId}) });
  } else {
    await request<void>("/bookmarks/" + encodeURIComponent(taskId), {method:"DELETE"});
  }
  if (typeof window !== "undefined") window.dispatchEvent(new Event(BOOKMARKS_CHANGED));
  return result;
}
