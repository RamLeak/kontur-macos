import { request } from "./api";

export type AIKind = "codex_cli" | "opencode_cli" | "ollama" | "lm_studio" | "openai_compatible_local" | "openai_compatible";
export interface AIProfile {
  kind: AIKind; model: string; effort: "low" | "medium" | "high" | "xhigh";
  endpoint: string | null; opencodeProvider: string | null; allowRemote: boolean;
  enabled: boolean; label?: string; hasApiKey?: boolean;
}
export interface AISettings { schemaVersion: 1; selectedKind: AIKind | null; profiles: AIProfile[] }
export const AI_OPTIONS: Array<{ kind: AIKind; label: string }> = [
  { kind: "codex_cli", label: "Codex" }, { kind: "opencode_cli", label: "OpenCode" },
  { kind: "ollama", label: "Ollama" }, { kind: "lm_studio", label: "LM Studio" },
  { kind: "openai_compatible_local", label: "Другая локальная модель" },
  { kind: "openai_compatible", label: "Подключение по API-ключу" },
];
export function defaultAIProfile(kind: AIKind): AIProfile {
  return { kind, model: kind === "codex_cli" ? "auto" : "", effort: "medium", endpoint:
    kind === "ollama" ? "http://127.0.0.1:11434" : kind === "lm_studio" || kind === "openai_compatible_local" ? "http://127.0.0.1:1234/v1" :
      kind === "openai_compatible" ? "https://api.openai.com/v1" : null,
    opencodeProvider: kind === "opencode_cli" ? "openai" : null,
    allowRemote: kind === "openai_compatible" || kind === "opencode_cli", enabled: true };
}
export function getAISettings() { return request<AISettings>("/local/ai"); }
export async function saveAIProfile(profile: AIProfile, apiKey?: string, clearApiKey = false) {
  const { kind, model, effort, endpoint, opencodeProvider, allowRemote, enabled, label } = profile;
  const config = { kind, model, effort, endpoint, opencodeProvider, allowRemote, enabled, label };
  const result = await request<AISettings>("/local/ai", { method: "PUT", body: JSON.stringify({ ...config, selected: true, ...(apiKey ? { apiKey } : {}), ...(clearApiKey ? { clearApiKey: true } : {}) }) });
  window.dispatchEvent(new Event("kontur:ai-settings-changed"));
  return result;
}
