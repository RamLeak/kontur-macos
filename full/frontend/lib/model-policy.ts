import type { Effort, ModelId, ModelOption, ModelSelection } from "@/lib/types";

const EFFORT_LABELS: Record<Effort, string> = { low: "низкий", medium: "средний", high: "высокий", xhigh: "очень высокий" };
export const MODEL_OPTIONS: ModelOption[] = (["luna", "terra", "sol"] as const).flatMap((model) =>
  (["low", "medium", "high", "xhigh"] as const).map((effort) => ({
    model, effort, label: `${model[0].toUpperCase() + model.slice(1)} · ${EFFORT_LABELS[effort]}`,
    description: `Ручной выбор: ${effort}. Автоматический маршрут не применяется.`,
  })),
);

export function isAllowedModelEffort(model: ModelId, effort: Effort | undefined): boolean {
  if (model === "auto") return effort === undefined;
  return MODEL_OPTIONS.some((option) => option.model === model && option.effort === effort);
}

export function normalizeSelection(selection: ModelSelection): ModelSelection {
  if (selection.model === "auto") return { model: "auto" };
  if (!isAllowedModelEffort(selection.model, selection.effort)) {
    throw new Error("Выбранная комбинация модели и мощности недоступна");
  }
  return selection;
}

export function modelLabel(selection: ModelSelection): string {
  if (selection.model === "auto") return "Авто";
  return MODEL_OPTIONS.find((option) => option.model === selection.model && option.effort === selection.effort)?.label ?? "Маршрут недоступен";
}

export function modelName(model: Exclude<ModelId, "auto">): string {
  return model[0].toUpperCase() + model.slice(1);
}
