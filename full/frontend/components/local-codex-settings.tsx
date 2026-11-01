"use client";
import { useEffect, useState } from "react";
import { localNative, type LocalCodexStatus } from "@/lib/local-native";
import styles from "./settings.module.css";

export function LocalCodexSettings() {
  const [status, setStatus] = useState<LocalCodexStatus | null>(null); const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true; let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try { const next = await localNative<LocalCodexStatus>("codex_status"); if (!active) return; setStatus(next); if (next.state === "checking" || next.loginState === "inProgress") timer = setTimeout(refresh, 500); }
      catch (reason) { if (active) setError(reason instanceof Error ? reason.message : "Не удалось проверить Codex."); }
    };
    void refresh(); return () => { active = false; clearTimeout(timer); };
  }, [attempt]);
  async function login() { setBusy(true); setError(null); try { setStatus(await localNative<LocalCodexStatus>("codex_login")); setAttempt(value => value + 1); } catch (reason) { setError(reason instanceof Error ? reason.message : "Вход не выполнен."); } finally { setBusy(false); } }
  return <section aria-label="Локальный Codex">
    <h3>Codex</h3><p role="status">{!status || status.state === "checking" ? "Проверяем подключение…" : status.state === "ready" ? "Codex найден" : "Codex не найден или недоступен"}</p>
    {status?.error && <p className={styles.inlineError}>{status.error.message}</p>}
    {status?.loginState === "inProgress" ? <p role="status">Завершите вход в открывшемся окне.</p> : <button type="button" className="outline-button" disabled={busy || status?.state !== "ready"} onClick={() => void login()}>Войти в Codex</button>}
    <button type="button" className={styles.resetButton} disabled={busy} onClick={() => { setError(null); setAttempt(value => value + 1); }}>Проверить снова</button>
    {error && <p className={styles.inlineError} role="alert">{error}</p>}
  </section>;
}
