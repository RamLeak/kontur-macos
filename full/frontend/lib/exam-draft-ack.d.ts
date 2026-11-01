export interface ExamCodeAckResult {
  applied: boolean;
  content: string | undefined;
}

export function resolveExamCodeAck(
  localCode: string | undefined,
  acknowledgedCode: string,
  serverCode: string,
): ExamCodeAckResult;
