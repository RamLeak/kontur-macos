import { ApiError } from "./types";

/** Keep machine-readable errors in the API layer, not in the learning interface. */
export function readableError(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return fallback;
  if (reason.code === "API_UNREACHABLE") return "Не удалось связаться с приложением. Проверьте, что оно запущено, и повторите попытку.";
  if (reason.status === 401 || reason.status === 403) return "Личная сессия недоступна. Войдите снова и повторите действие.";
  if (reason.status === 429) return "Слишком много запросов. Подождите немного и повторите попытку.";
  if (reason.code === "SEALED_EXAM_ACTIVE") return "Во время экзамена подсказки и образцы кода недоступны.";
  if (reason.code === "VERSION_CONFLICT" || reason.code === "STALE_VERSION") return "Эти данные изменились в другом окне. Обновите страницу перед продолжением.";
  if (reason.code === "ASSISTANT_RUN_TIMEOUT") return "Ответ занимает больше времени, чем обычно. Повторите проверку — новый запрос не будет создан.";
  const nativeCode = typeof reason.details.native_code === "string" ? reason.details.native_code : null;
  if (reason.details.retryable === true && nativeCode === "codex_unavailable") return "Подключение помощника ещё проверяется. Подождите немного и повторите отправку. Вопрос сохранён в поле.";
  const assistantError = assistantFailureMessage(nativeCode) ?? assistantFailureMessage(reason.code);
  if (assistantError) return assistantError;
  return fallback;
}

/** Only application-owned codes become copy; raw provider errors never reach the UI. */
export function assistantFailureMessage(code: string | null | undefined): string | null {
  return ({
    codex_not_found: "Codex не найден. Откройте настройки помощника и проверьте подключение.",
    codex_incompatible: "Эта версия Codex пока не поддерживается. Проверьте подключение в настройках помощника.",
    codex_unsafe_configuration: "Не удалось подготовить Codex для разбора. Проверьте подключение в настройках помощника.",
    codex_request_invalid: "Не удалось отправить этот вопрос. Сократите сообщение или выбранный фрагмент и повторите.",
    codex_busy: "Codex ещё занят проверкой, входом или другим ответом. Дождитесь завершения и повторите отправку.",
    codex_timeout: "Codex не успел ответить. Проверьте подключение к интернету и повторите отправку.",
    codex_output_limit: "Ответ оказался слишком большим. Попробуйте задать более короткий вопрос.",
    codex_tool_event: "Этот ответ потребовал недоступного действия. Попробуйте другую модель или уточните вопрос.",
    codex_protocol: "Не удалось прочитать ответ Codex. Повторите отправку; если ошибка остаётся, проверьте подключение в настройках.",
    codex_process_failed: "Codex завершил запрос с ошибкой. Проверьте интернет и выбранную модель: успешный вход ещё не подтверждает, что модель доступна.",
    codex_login_failed: "Вход в Codex не завершён. Повторите вход из настроек помощника.",
    codex_spawn_failed: "Не удалось запустить Codex. Проверьте подключение в настройках помощника.",
    codex_runtime_unavailable: "Подключение Codex недоступно. Закройте Контур и откройте его снова.",
    codex_cancel_failed: "Не удалось завершить предыдущий запрос Codex. Закройте Контур и откройте его снова.",
    codex_cancelled: "Запрос отменён. Вопрос сохранён в поле.",
    AI_UNAVAILABLE: "Помощник сейчас недоступен. Откройте настройки помощника и проверьте подключение.",
    AGENT_PROTOCOL_ERROR: "Не удалось прочитать ответ помощника. Повторите отправку или проверьте выбранную модель в настройках.",
    AGENT_FAILED: "Помощник не смог завершить ответ. Проверьте подключение и выбранную модель в настройках.",
  } as Record<string, string>)[code ?? ""] ?? null;
}

export function assistantStatusLabel(status: string): string {
  return ({
    queued: "Запрос в очереди…", running: "Наставник готовит ответ…",
    started: "Наставник готовит ответ…", succeeded: "Ответ готов", completed: "Ответ готов",
    failed: "Не удалось подготовить ответ", timed_out: "Время ожидания истекло",
    cancelled: "Запрос отменён", cancelling: "Отменяем запрос…", unavailable: "Помощник пока недоступен",
  } as Record<string, string>)[status] ?? "Обрабатываем запрос…";
}
