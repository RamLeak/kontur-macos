export interface FullImportPreview {
  formatVersion: number; format: string; contentSha256: string;
  counts: Record<string, number>; conflictCount: number; warnings: string[];
}
export interface FullImportSelection { token: string; preview: FullImportPreview }
export interface FullImportResult { format: string; contentSha256: string; imported: Record<string, number>; duplicateRecords: number; warnings: string[] }
export interface FullExportResult { format: string; contentSha256: string; counts: Record<string, number> }
const invalid = () => new Error("Локальное приложение вернуло неподтверждённый результат переноса.");
function object(value: unknown): Record<string, unknown> { if (!value || typeof value !== "object" || Array.isArray(value)) throw invalid(); return value as Record<string, unknown>; }
function integer(value: unknown): number { if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw invalid(); return value; }
function text(value: unknown): string { if (typeof value !== "string" || !value || value.length > 512) throw invalid(); return value; }
function checksum(value: unknown): string { const result = text(value); if (!/^[a-f0-9]{64}$/.test(result)) throw invalid(); return result; }
function counts(value: unknown): Record<string, number> { const entries = Object.entries(object(value)); if (entries.length > 256) throw invalid(); return Object.fromEntries(entries.map(([key, count]) => { if (!/^[a-zA-Z0-9_-]{1,100}$/.test(key)) throw invalid(); return [key, integer(count)]; })); }
function warnings(value: unknown): string[] { if (!Array.isArray(value) || value.length > 1000 || value.some(item => typeof item !== "string" || item.length > 2048)) throw invalid(); return value as string[]; }
export function importSelection(value: unknown): FullImportSelection | null {
  if (value === null) return null;
  const selection = object(value); const preview = object(selection.preview);
  return { token: text(selection.token), preview: { formatVersion: integer(preview.formatVersion), format: text(preview.format), contentSha256: checksum(preview.contentSha256), counts: counts(preview.counts), conflictCount: integer(preview.conflictCount), warnings: warnings(preview.warnings) } };
}
export function importResult(value: unknown): FullImportResult {
  const result = object(value);
  return { format: text(result.format), contentSha256: checksum(result.contentSha256), imported: counts(result.imported), duplicateRecords: integer(result.duplicateRecords), warnings: warnings(result.warnings) };
}
export function exportResult(value: unknown): FullExportResult | null {
  if (value === null) return null; const result = object(value);
  return { format: text(result.format), contentSha256: checksum(result.contentSha256), counts: counts(result.counts) };
}
