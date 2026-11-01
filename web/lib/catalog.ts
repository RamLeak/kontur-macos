import type { Attempt, ProgressSummary, ProgressStatus, Task } from "./native-types";

export const progressLabels: Record<ProgressStatus, string> = {
  not_started: "Не начато", in_progress: "В работе", error: "Ошибка", solved: "Решено",
};
export function taskKey(task: Pick<Task, "sourceId" | "revision">): string {
  return JSON.stringify([task.sourceId, task.revision]);
}
export function progressIndex(progress: ProgressSummary[]): Map<string, ProgressSummary> {
  return new Map(progress.map(item => [taskKey({ sourceId: item.taskSourceId, revision: item.taskRevision }), item]));
}
export function olderAttempts(task: Task, attempts: Attempt[]): number {
  return attempts.filter(item => item.taskSourceId === task.sourceId && item.taskRevision !== null && item.taskRevision !== task.revision).length;
}
export function filterTasks(tasks: Task[], type: string, status: string, query: string, progress: Map<string, ProgressSummary>): Task[] {
  const search = query.trim().toLocaleLowerCase("ru");
  return tasks.filter(task => (!type || task.typeNumber === Number(type))
    && (!status || (progress.get(taskKey(task))?.status ?? "not_started") === status)
    && (!search || `${task.title} ${task.sourceId} ${task.statement}`.toLocaleLowerCase("ru").includes(search)));
}
