export function normalizeExamAnswers(value: unknown): Record<string, string> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const entries = Object.entries(value as Record<string, unknown>).flatMap(([itemId, answer]) => {
    if (typeof answer === "string") return [[itemId, answer] as [string, string]];
    if (answer && typeof answer === "object" && !Array.isArray(answer) && typeof (answer as Record<string, unknown>).content === "string") {
      return [[itemId, (answer as Record<string, unknown>).content as string] as [string, string]];
    }
    return [];
  });
  return Object.fromEntries(entries);
}
