"use client";
import { useEffect, useState, type ReactNode } from "react";
import { initializeLocalClientState, localClientStateSnapshot, subscribeLocalClientState, resolveLocalClientStateConflict, flushLocalClientState } from "@/lib/local-client-state";
import styles from "./local-client-state-gate.module.css";

export function LocalClientStateGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null), [state, setState] = useState(localClientStateSnapshot);
  async function attempt(choice?: "local" | "remote") {
    setBusy(true); setError(null);
    try { if (choice) await resolveLocalClientStateConflict(choice); else if (ready) await flushLocalClientState(); else await initializeLocalClientState(); setReady(true); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось загрузить локальные настройки."); }
    finally { setBusy(false); setState(localClientStateSnapshot()); }
  }
  useEffect(() => {
    let active = true, unsubscribe: (() => void) | undefined;
    try { unsubscribe = subscribeLocalClientState(() => { if (active) setState(localClientStateSnapshot()); }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Локальное хранилище недоступно."); }
    void initializeLocalClientState().then(() => { if (active) setReady(true); }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : "Не удалось загрузить локальные настройки."); });
    return () => { active = false; unsubscribe?.(); };
  }, []);
  const notice = error ?? state.error;
  return <>{(notice || !ready) && <div className={ready ? "session-notice" : styles.startup} role={notice ? "alert" : "status"}>
    {!ready && <><span className={styles.mark} aria-hidden="true">К</span><h1>Контур</h1></>}
    {!ready && !notice && <span className={styles.spinner} aria-hidden="true" />}
    <span>{notice ?? "Открываем задания и ваши записи…"}</span>{notice && <>
      <button className="outline-button" type="button" disabled={busy} onClick={() => void attempt()}>Повторить</button>
      {state.conflict && <><button className="outline-button" type="button" disabled={busy} onClick={() => void attempt("local")}>Сохранить данные устройства</button><button className="outline-button" type="button" disabled={busy} onClick={() => void attempt("remote")}>Восстановить из копии</button></>}
    </>}
  </div>}{ready && children}</>;
}
