import type { JsonObject } from "./native-types";

const labels: Record<string, string> = {
  legacy_attempt_code_not_linked_to_version: "Код некоторых старых попыток не удалось связать с сохранённой версией.",
  legacy_unproven_verdicts_are_unverified: "Неподтверждённые старые результаты перенесены без оценки верности.",
  exam_answers_imported_without_grade_claims: "Экзаменационные ответы перенесены без утверждения о правильности.",
  browser_draft_time_unknown_zero: "Время части браузерных черновиков неизвестно.",
  browser_recovery_kept_as_separate_documents: "Восстановленные браузерные черновики сохранены отдельными работами.",
};

export function transferNotes(values: JsonObject): string[] {
  const raw = values["kontur.legacy12.transferWarnings"];
  if (!Array.isArray(raw)) return [];
  const codes = raw.filter((item): item is string => typeof item === "string");
  return [...new Set(codes.map(code => labels[code] ?? "Часть импортированных данных требует проверки."))];
}
