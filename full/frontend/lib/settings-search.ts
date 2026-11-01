export type SettingsSearchItem = {
  label: string;
  description?: string;
  keywords?: readonly string[];
};

export function filterSettingsSections<T extends SettingsSearchItem>(items: readonly T[], query: string): T[] {
  const normalize = (value: string) => value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
  const needle = normalize(query);
  if (!needle) return [...items];
  return items.filter((item) => normalize([item.label, item.description ?? "", ...(item.keywords ?? [])].join(" ")).includes(needle));
}
