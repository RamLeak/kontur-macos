export type LearningStatus = "neutral" | "started" | "incorrect" | "correct";
export interface TaskProgress {
  task_id: string;
  task_revision_id: string;
  status: LearningStatus;
  bookmarked: boolean;
  solve_session_id: string | null;
}
export interface TaskBookmark {
  task_id: string;
  task_revision_id: string;
  created_at: string;
}
