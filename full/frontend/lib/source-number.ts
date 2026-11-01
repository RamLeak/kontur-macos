/** Keep upstream numbers literal. Catalog/exam positions are separate values. */
export function resolveSourceNumber(externalId: unknown, revisionSourceId: unknown): string | null {
  const checked = (value: unknown): string | null => {
    if (value == null) return null;
    if (typeof value !== "string" || !value || value.length > 255 || value.trim() !== value || /[\u0000-\u001f\u007f]/.test(value)) {
      throw new Error("Источник вернул некорректный номер задания.");
    }
    return value;
  };
  const current = checked(externalId);
  const pinned = checked(revisionSourceId);
  if (current !== null && pinned !== null && current !== pinned) {
    throw new Error("Номер задания не совпадает с номером сохранённой версии.");
  }
  return pinned ?? current;
}

export function sourceNumberLabel(number: string | null | undefined): string {
  return number ? `КЕГЭ № ${number}` : "Номер источника не указан";
}
