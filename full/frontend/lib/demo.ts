import type {
  AnalyticsOverview,
  Annotation,
  Attempt,
  BoardDocument,
  Capabilities,
  CodeDocument,
  CodeRun,
  CodeSample,
  ConversationSummary,
  DailyPlan,
  ErrorEvent,
  ExamBlueprint,
  MasteryState,
  Message,
  Project,
  ReviewItem,
  SolveSession,
  TaskRecord,
} from "@/lib/types";

export const DEMO_TASK: TaskRecord = {
  id: "task-demo-27-31112",
  line_number: 27,
  exam_year: 2025,
  target_exam_year: null,
  title: "Демо-задача 27",
  difficulty: "hard",
  source_url: "demo://fixture/task-27",
  demo: true,
  revision: {
    id: "revision-demo-27-v1",
    content_hash: "demo:27:v1",
    source_url: "demo://fixture/task-27",
    source_name: "demo",
    retrieved_at: "2026-09-02T08:00:00Z",
    revision_label: "fixture v1 · демо",
    statement_html:
      "В каталоге находятся файлы со следующими именами: 1.txt, 2.txt, 3.txt, 4.txt, 5.txt, 6.txt, 7.txt, 8.txt, 9.txt.\n\nОпределите, сколько файлов удовлетворяют указанным условиям.",
    target_exam_year: null,
  },
  assets: [
    {
      id: "asset-demo-27",
      name: "27_условие.pdf",
      media_type: "application/pdf",
      size_bytes: 134144,
      download_url: "/api/v1/assets/asset-demo-27/download",
      available: true,
      cache_state: "ready",
      content_hash: "demo:asset-27:v1",
      rights_status: "test_only",
      canonical_url: "demo://fixture/task-27/27_условие.pdf",
      progress_percent: 100,
    },
  ],
  skills: ["Работа с файлами", "Фильтрация", "Строки и списки"],
};

export const DEMO_MESSAGES: Message[] = [
  {
    id: "message-1",
    role: "assistant",
    content:
      "Определи, какие файлы удовлетворяют условию. Напиши программу на Python, которая подсчитает их количество.",
    created_at: "2026-09-02T10:14:00Z",
  },
  {
    id: "message-2",
    role: "user",
    content: "Написал решение, проверьте, пожалуйста.",
    created_at: "2026-09-02T10:17:00Z",
  },
  {
    id: "message-3",
    role: "assistant",
    content:
      "Почти верно: нужно учитывать ведущие нули в шестнадцатеричной записи размера файла. Проверь условие и поправь фильтр.",
    created_at: "2026-09-02T10:20:00Z",
  },
  {
    id: "message-4",
    role: "user",
    content: "Исправил, запускаю снова.",
    created_at: "2026-09-02T10:21:00Z",
  },
  {
    id: "message-5",
    role: "assistant",
    content: "Демо preview: production runner не подключён, автоматическая проверка не выполнялась.",
    created_at: "2026-09-02T10:23:00Z",
  },
];

export const DEMO_CODE: CodeDocument = {
  id: "code-demo-27",
  language: "python",
  version: 3,
  updated_at: "2026-09-02T10:23:00Z",
  content: [
    "from pathlib import Path",
    "",
    "count = 0",
    "",
    "for path in Path(\".\").glob(\"*.txt\"):",
    "    size = path.stat().st_size",
    "    hex_size = format(size, \"X\")",
    "    if hex_size.endswith(\"F\") and hex_size.count(\"F\") == 2:",
    "        count += 1",
    "",
    "print(count)",
  ].join("\n"),
};

export const DEMO_RUN: CodeRun = {
  id: "run-demo-3",
  status: "unavailable",
  stdout: "",
  stderr: "",
  duration_ms: null,
  exit_code: null,
  created_at: "2026-09-02T10:23:00Z",
  reason: "Демо-режим: production runner не подключён.",
};

export const DEMO_ATTEMPTS: Attempt[] = [
  {
    id: "attempt-demo-1",
    status: "failed",
    user_answer: "Проверил фильтр на одном файле",
    verified_correct: false,
    verification_method: "self_reported",
    independent: true,
    hint_count: 1,
    error_type: "Граничный случай",
    notes: "TEST_ONLY: не учётная запись реального результата.",
    started_at: "2026-09-02T10:17:00Z",
    completed_at: "2026-09-02T10:20:00Z",
  },
];

export const DEMO_BOARD: BoardDocument = {
  id: "board-demo-27",
  version: 4,
  strokes: [
    {
      id: "stroke-demo-1",
      color: "#6257e8",
      width: 3,
      points: [
        { x: 155, y: 148 },
        { x: 200, y: 128 },
        { x: 250, y: 144 },
        { x: 294, y: 120 },
      ],
    },
  ],
  texts: [{ id: "text-demo-1", x: 150, y: 195, text: "два F → считаем" }],
  viewport: { x: 0, y: 0, zoom: 1 },
  updated_at: "2026-09-02T10:22:00Z",
};

export const DEMO_ANNOTATIONS: Annotation[] = [
  {
    id: "annotation-demo-1",
    kind: "highlight",
    color: "#fef08a",
    text: "Определите, сколько файлов удовлетворяют указанным условиям.",
    anchor: { start: 116, end: 176, content_hash: DEMO_TASK.revision.content_hash },
  },
];

export const DEMO_SESSION: SolveSession = {
  id: "solve-demo-27",
  task: DEMO_TASK,
  messages: DEMO_MESSAGES,
  code: DEMO_CODE,
  runs: [DEMO_RUN],
  attempts: DEMO_ATTEMPTS,
  board: DEMO_BOARD,
  annotations: DEMO_ANNOTATIONS,
  sealed_exam: false,
};

export const DEMO_PROJECTS: Project[] = [
  {
    id: "project-1",
    name: "ЕГЭ 2025",
    color: "#6257e8",
    conversation_count: 18,
    updated_at: "2026-09-02T10:23:00Z",
  },
  {
    id: "project-2",
    name: "Строки и списки",
    color: "#8b7cf6",
    conversation_count: 7,
    updated_at: "2026-09-01T16:40:00Z",
  },
  {
    id: "project-3",
    name: "Графы",
    color: "#14a38b",
    conversation_count: 4,
    updated_at: "2026-08-29T19:15:00Z",
  },
  {
    id: "project-4",
    name: "Динамика",
    color: "#d79028",
    conversation_count: 5,
    updated_at: "2026-08-28T14:20:00Z",
  },
];

export const DEMO_CONVERSATIONS: ConversationSummary[] = [
  {
    id: "conversation-27",
    title: "Задача 27 №31112",
    project_id: "project-1",
    updated_at: "2026-09-02T10:23:00Z",
    task_line: 27,
  },
  {
    id: "conversation-24",
    title: "Задача 24 №27891",
    project_id: "project-1",
    updated_at: "2026-09-01T18:10:00Z",
    task_line: 24,
  },
  {
    id: "conversation-17",
    title: "Задача 17 №34105",
    project_id: "project-2",
    updated_at: "2026-08-31T12:04:00Z",
    task_line: 17,
  },
  {
    id: "conversation-27b",
    title: "Задача 27 №30518",
    project_id: "project-1",
    updated_at: "2026-08-30T20:12:00Z",
    task_line: 27,
  },
  {
    id: "conversation-12",
    title: "Архив 12 №26874",
    project_id: "project-2",
    updated_at: "2026-08-28T09:35:00Z",
    task_line: 12,
  },
];

export const DEMO_CAPABILITIES: Capabilities = {
  source_sync: {
    available: false,
    reason: "Синхронизация отключена до прохождения SourcePolicy.",
  },
  source_variant_sync: {
    available: false,
    reason: "TEST_ONLY: variant sync недоступна в demo preview.",
  },
  asset_fetch: { available: true, reason: null, summary: "Локальный fixture asset" },
  runner: {
    available: false,
    reason: "Production runner ещё не подключён. Код можно сохранять, запуск будет недоступен.",
  },
  codex: {
    available: false,
    reason: "Codex gateway ожидает owner-operated auth.",
  },
  demo_mode: true,
};

export const DEMO_CODE_SAMPLES: CodeSample[] = [
  {
    id: "sample-1",
    title: "Чтение файлов из каталога",
    language: "python",
    content: "from pathlib import Path\n\nfor path in Path('.').glob('*.txt'):\n    print(path.name)",
    task_lines: [9, 17, 27],
    tags: ["pathlib", "файлы"],
    usage_note: "Базовый шаблон для файловых задач",
    enabled: true,
    version: 2,
    content_hash: "demo:sample-1:v2",
  },
  {
    id: "sample-2",
    title: "Подсчёт по условию",
    language: "python",
    content: "answer = sum(1 for item in items if condition(item))",
    task_lines: [6, 8, 27],
    tags: ["счётчик", "условие"],
    usage_note: "Компактный вариант, если условие читается однозначно",
    enabled: true,
    version: 1,
    content_hash: "demo:sample-2:v1",
  },
];

export const DEMO_ERRORS: ErrorEvent[] = [
  {
    id: "error-1",
    task_line: 27,
    title: "Ведущие нули в шестнадцатеричной записи",
    type: "Граничный случай",
    note: "Проверять форматирование размера до фильтрации.",
    resolved: false,
    next_review: "Сегодня",
    status: "proposed",
  },
  {
    id: "error-2",
    task_line: 17,
    title: "Индексирование строк",
    type: "Реализация",
    note: "Сначала выписать индексы на коротком примере.",
    resolved: false,
    next_review: "Через 3 дня",
    status: "confirmed",
  },
  {
    id: "error-3",
    task_line: 24,
    title: "Состояние динамики",
    type: "Алгоритм",
    note: "Не смешивать переход и значение ответа.",
    resolved: false,
    next_review: "Через 7 дней",
    status: "rejected",
  },
];

export const DEMO_MASTERY: MasteryState[] = [
  {
    id: "skill-files",
    name: "Работа с файлами",
    status: "learning",
    evidence_count: 8,
    independent_successes: 5,
    independent_success_rate: 62,
    reason: "5 самостоятельных успехов из 8 наблюдений",
  },
  {
    id: "skill-strings",
    name: "Строки и списки",
    status: "stable",
    evidence_count: 12,
    independent_successes: 10,
    independent_success_rate: 83,
    reason: "Успех подтверждён в трёх разных типах задач",
  },
  {
    id: "skill-graphs",
    name: "Графы",
    status: "needs_attention",
    evidence_count: 5,
    independent_successes: 2,
    independent_success_rate: 40,
    reason: "Повторяется ошибка в выборе обхода",
  },
  {
    id: "skill-dp",
    name: "Динамическое программирование",
    status: "learning",
    evidence_count: 6,
    independent_successes: 4,
    independent_success_rate: 67,
    reason: "Нужны две независимые попытки без подсказки",
  },
];

export const DEMO_REVIEW: ReviewItem[] = [
  {
    id: "review-1",
    label: "Файлы: фильтр по записи",
    task_line: 27,
    due_label: "Сегодня",
    reason: "Повторить после ошибки в граничном случае",
  },
  {
    id: "review-2",
    label: "Графы: поиск кратчайшего пути",
    task_line: 24,
    due_label: "Завтра",
    reason: "Закрепить после подсказки",
  },
  {
    id: "review-3",
    label: "Строки: срезы и индексы",
    task_line: 17,
    due_label: "Через 3 дня",
    reason: "Проверка удержания",
  },
];

export const DEMO_PLAN: DailyPlan = {
  date: "2 сентября",
  title: "Небольшой план на сегодня",
  items: [
    {
      id: "plan-1",
      label: "Задача 27 №31112",
      detail: "Файлы · 25 минут",
      done: false,
    },
    {
      id: "plan-2",
      label: "Повторение: строки и индексы",
      detail: "1 карточка · 10 минут",
      done: true,
    },
    {
      id: "plan-3",
      label: "Задача 24 №27891",
      detail: "Алгоритмы · 30 минут",
      done: false,
    },
  ],
};

export const DEMO_EXAMS: ExamBlueprint[] = [
  {
    id: "exam-2025-1",
    name: "Пробный вариант 01",
    year: 2025,
    seed: "ege-2025-01-a",
    blueprint_version: "blueprint-2025-v1",
    revision_ids: ["demo-revision-01", "demo-revision-02", "demo-revision-03"],
    revision_count: 27,
    scoring_version: "2025-v1",
    duration_minutes: 235,
    status: "ready",
  },
  {
    id: "exam-errors",
    name: "Вариант по слабым темам",
    year: 2025,
    seed: "weak-areas-02",
    blueprint_version: "blueprint-weak-v1",
    revision_ids: ["demo-revision-17", "demo-revision-24", "demo-revision-27"],
    revision_count: 15,
    scoring_version: "2025-v1",
    duration_minutes: 120,
    status: "ready",
  },
];

export const DEMO_ANALYTICS: AnalyticsOverview = {
  solved_count: 46,
  independent_rate: 74,
  average_minutes: 18,
  evidence_count: 82,
  coverage: 68,
  weekly: [
    { label: "Пн", value: 4 },
    { label: "Вт", value: 7 },
    { label: "Ср", value: 5 },
    { label: "Чт", value: 8 },
    { label: "Пт", value: 6 },
    { label: "Сб", value: 10 },
    { label: "Вс", value: 6 },
  ],
};
