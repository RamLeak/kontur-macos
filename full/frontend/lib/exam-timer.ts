export interface ExamTimerSnapshot {
  status: string;
  serverNow: string | null;
  remainingSeconds: number | null;
  deadlineAt: string | null;
}

export interface ExamTimerAnchor {
  key: string;
  monotonicDeadlineMs: number | null;
}

export function examTimerSnapshotKey(snapshot: ExamTimerSnapshot): string {
  return `${snapshot.status}:${snapshot.serverNow ?? ""}:${snapshot.remainingSeconds ?? ""}:${snapshot.deadlineAt ?? ""}`;
}

export function createExamTimerAnchor(
  snapshot: ExamTimerSnapshot,
  key: string,
  wallNowMs: number,
  monotonicNowMs: number,
): ExamTimerAnchor {
  const remainingMs = finiteRemainingMs(snapshot.remainingSeconds)
    ?? legacyRemainingMs(snapshot.deadlineAt, wallNowMs);
  return {
    key,
    monotonicDeadlineMs: snapshot.status === "paused" || remainingMs == null
      ? null
      : monotonicNowMs + remainingMs,
  };
}

export function remainingExamSeconds(
  snapshot: ExamTimerSnapshot,
  anchor: ExamTimerAnchor,
  monotonicNowMs: number,
): number | null {
  if (snapshot.status === "paused") return finiteRemainingSeconds(snapshot.remainingSeconds);
  if (anchor.monotonicDeadlineMs != null) {
    return Math.max(0, Math.ceil((anchor.monotonicDeadlineMs - monotonicNowMs) / 1000));
  }
  return finiteRemainingSeconds(snapshot.remainingSeconds);
}

function finiteRemainingMs(value: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) * 1000 : null;
}

function finiteRemainingSeconds(value: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.ceil(value)) : null;
}

function legacyRemainingMs(deadlineAt: string | null, wallNowMs: number): number | null {
  if (!deadlineAt) return null;
  const deadlineMs = Date.parse(deadlineAt);
  return Number.isFinite(deadlineMs) ? Math.max(0, deadlineMs - wallNowMs) : null;
}
