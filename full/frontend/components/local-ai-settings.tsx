"use client";
import { useEffect, useState } from "react";
import { request } from "@/lib/api";
import { AI_OPTIONS, defaultAIProfile, getAISettings, saveAIProfile, type AIKind, type AIProfile, type AISettings } from "@/lib/local-ai";
import { LocalCodexSettings } from "./local-codex-settings";
import styles from "./local-ai-settings.module.css";

const API_PRESETS = [
  ["OpenAI", "https://api.openai.com/v1"], ["OpenRouter", "https://openrouter.ai/api/v1"],
  ["DeepSeek", "https://api.deepseek.com/v1"], ["Groq", "https://api.groq.com/openai/v1"],
  ["Mistral", "https://api.mistral.ai/v1"], ["Другой адрес", ""],
] as const;

export function LocalAISettings() {
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [profile, setProfile] = useState<AIProfile>(defaultAIProfile("codex_cli"));
  const [apiKey, setApiKey] = useState(""), [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  useEffect(() => { let active = true;
    void getAISettings().then(value => { if (!active) return; setSettings(value); setProfile(value.profiles.find(item => item.kind === value.selectedKind) ?? defaultAIProfile("codex_cli")); })
      .catch(() => { if (active) setError("Не удалось открыть настройки помощника. Попробуйте открыть этот раздел ещё раз."); });
    return () => { active = false; };
  }, []);
  const local = ["ollama", "lm_studio", "openai_compatible_local"].includes(profile.kind);
  const keyed = profile.kind === "opencode_cli" || profile.kind === "openai_compatible";
  function choose(kind: AIKind) {
    setProfile(settings?.profiles.find(item => item.kind === kind) ?? defaultAIProfile(kind));
    setApiKey(""); setClearKey(false); setError(null); setNotice(null);
  }
  function connectionChange(change: Partial<AIProfile>) {
    if (("endpoint" in change && change.endpoint !== profile.endpoint) || ("opencodeProvider" in change && change.opencodeProvider !== profile.opencodeProvider)) {
      setApiKey(""); setClearKey(Boolean(profile.hasApiKey));
    }
    setProfile(value => ({ ...value, ...change }));
  }
  function modelChange(model: string) {
    if (profile.kind === "opencode_cli" && model.includes("/")) {
      connectionChange({ model, opencodeProvider: model.split("/", 1)[0] });
    } else setProfile(value => ({ ...value, model }));
  }
  async function save(check = false) {
    if (busy) return; setBusy(true); setError(null); setNotice(null);
    try {
      const saved = await saveAIProfile(profile, apiKey, clearKey);
      setSettings(saved); setProfile(saved.profiles.find(item => item.kind === profile.kind) ?? profile); setApiKey(""); setClearKey(false);
      if (check) {
        const response = await request<{ result?: { available?: boolean; codex?: { available?: boolean }; opencode?: { available?: boolean } }; available?: boolean }>("/local/ai/check", { method: "POST", body: JSON.stringify({ kind: profile.kind, network: true }) });
        if (!(response.result?.available ?? response.result?.codex?.available ?? response.result?.opencode?.available ?? response.available)) throw new Error("unavailable");
        setNotice(profile.kind.endsWith("_cli") ? "Программа найдена. Доступ к выбранной модели проверится при отправке вопроса." : "Подключение доступно. Можно отправлять вопросы помощнику.");
      } else setNotice("Настройки сохранены. Помощник будет использовать выбранное подключение.");
    } catch { setError(check ? "Не удалось подключиться. Проверьте адрес, название модели и ключ; для локальной модели откройте её программу." : "Не удалось сохранить настройки. Проверьте заполненные поля."); }
    finally { setBusy(false); }
  }
  return <section className={styles.section} aria-labelledby="ai-connection-title">
    <div><h3 id="ai-connection-title">Подключение помощника</h3><p>Можно использовать Codex, OpenCode или модель на вашем компьютере.</p></div>
    <label className={styles.field}>Способ подключения<select disabled={busy} value={profile.kind} onChange={event => choose(event.target.value as AIKind)}>{AI_OPTIONS.map(item => <option value={item.kind} key={item.kind}>{item.label}</option>)}</select></label>
    {profile.kind === "codex_cli" ? <LocalCodexSettings /> : <>
      {profile.kind === "openai_compatible" && <label className={styles.field}>Провайдер<select disabled={busy} value={API_PRESETS.find(item => item[1] === profile.endpoint)?.[0] ?? "Другой адрес"} onChange={event => { const preset = API_PRESETS.find(item => item[0] === event.target.value); if (preset) connectionChange({ endpoint: preset[1] }); }}>{API_PRESETS.map(item => <option key={item[0]}>{item[0]}</option>)}</select></label>}
      {profile.kind === "opencode_cli" && <label className={styles.field}>Провайдер в OpenCode<input disabled={busy} placeholder="openai, anthropic, google, openrouter…" value={profile.opencodeProvider ?? ""} onChange={event => connectionChange({ opencodeProvider: event.target.value })} spellCheck={false} maxLength={128} /></label>}
      <label className={styles.field}>Название модели<input disabled={busy} placeholder={profile.kind === "opencode_cli" ? "Провайдер/модель из OpenCode" : "Точное название из вашей программы или кабинета провайдера"} value={profile.model} onChange={event => modelChange(event.target.value)} spellCheck={false} maxLength={512} autoCapitalize="none" /></label>
      <label className={styles.field}>Адрес {profile.kind === "opencode_cli" ? "(если используется свой)" : "подключения"}<input disabled={busy} placeholder={local ? "http://127.0.0.1:1234/v1" : "https://…"} value={profile.endpoint ?? ""} onChange={event => connectionChange({ endpoint: event.target.value || null })} spellCheck={false} maxLength={2048} autoCapitalize="none" /></label>
      {keyed && <label className={styles.field}>API-ключ<input type="password" disabled={busy} autoComplete="new-password" spellCheck={false} maxLength={4096} value={apiKey} placeholder={profile.hasApiKey && !clearKey ? "Ключ сохранён. Оставьте пустым, чтобы сохранить его." : "Вставьте ключ провайдера"} onChange={event => { setApiKey(event.target.value); setClearKey(false); }} />{profile.hasApiKey && <button type="button" className="tiny-action" disabled={busy} onClick={() => { setClearKey(true); setApiKey(""); }}>Забыть сохранённый ключ</button>}{clearKey && <small>Ключ будет удалён после сохранения.</small>}</label>}
      <p className={styles.help}>{local ? "Сначала загрузите модель и включите её локальный сервер в Ollama или LM Studio. Интернет для ответов такой модели не нужен." : profile.kind === "opencode_cli" ? "Используется установленный OpenCode. Введите ключ здесь; вопросы отправляются только выбранному провайдеру." : "Поддерживаются провайдеры с подключением в формате OpenAI. Вопросы и выбранный контекст отправляются при нажатии «Отправить»."}</p>
    </>}
    <div className={styles.actions}><button className="primary-button" type="button" disabled={busy || (profile.kind !== "codex_cli" && !profile.model.trim())} onClick={() => void save()}>{busy ? "Подождите…" : "Использовать это подключение"}</button><button className="outline-button" type="button" disabled={busy || (profile.kind !== "codex_cli" && !profile.model.trim())} onClick={() => void save(true)}>Сохранить и проверить</button></div>
    {notice && <p role="status" className={styles.notice}>{notice}</p>}{error && <p role="alert" className={styles.error}>{error}</p>}
  </section>;
}
