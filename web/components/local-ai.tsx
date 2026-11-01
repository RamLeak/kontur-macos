"use client";

import { useEffect, useRef, useState } from "react";
import { native, safeError } from "@/lib/native";
import type { CodexPoll, CodexStatus, Document, ModelSelection } from "@/lib/native-types";

export function LocalAI({ active, document, statementText }: { active: boolean; document: Document | null; statementText: string }) {
  const [status, setStatus] = useState<CodexStatus | null>(null);
  const [question, setQuestion] = useState("");
  const [withCode, setWithCode] = useState(false);
  const [withStatement, setWithStatement] = useState(false);
  const [modelId, setModelId] = useState("");
  const [poll, setPoll] = useState<CodexPoll | null>(null);
  const [sent, setSent] = useState<{ question: string; title: string; documentId: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const request = useRef<CodexPoll | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const running = poll?.state === "starting" || poll?.state === "running";

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  function accept(next: CodexPoll) {
    if (!mounted.current) return;
    request.current = next; setPoll(next);
    if (next.state !== "starting" && next.state !== "running") setCancelling(false);
  }

  useEffect(() => {
    if (!active || (status && status.state !== "checking" && status.loginState !== "inProgress")) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      try {
        const next = await native.codexStatus();
        if (!alive) return;
        setStatus(next);
        if (next.activeRequestId && !request.current) {
          try { accept(await native.codexPoll(next.activeRequestId)); } catch { /* Status remains actionable. */ }
        }
        if (next.state === "checking" || next.loginState === "inProgress") timer = setTimeout(update, 500);
      } catch (reason) {
        if (alive) setStatus({ state: "unavailable", loginState: "unknown", manualModelSupported: false, manualModels: [], error: safeError(reason) });
      }
    };
    void update();
    return () => { alive = false; clearTimeout(timer); };
  }, [active, status?.state, status?.loginState]);

  useEffect(() => {
    if (!poll || !running) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const update = async () => {
      try {
        const next = await native.codexPoll(poll.requestId);
        if (!alive) return;
        accept(next); setError(null);
        if (next.state === "starting" || next.state === "running") timer = setTimeout(update, 300);
      } catch (reason) {
        if (alive) { setError(safeError(reason, "Не удалось получить ответ. Пробуем снова…").message); timer = setTimeout(update, 1000); }
      }
    };
    timer = setTimeout(update, 300);
    return () => { alive = false; clearTimeout(timer); };
  }, [poll?.requestId, running]);

  useEffect(() => { if (active && feed.current) feed.current.scrollTop = feed.current.scrollHeight; }, [poll?.text, active]);

  async function send() {
    if (busy || running || !question.trim() || !document) return;
    setBusy(true); setError(null);
    const text = question.trim();
    const model: ModelSelection = modelId ? { mode: "manual", id: modelId } : { mode: "cliDefault" };
    try {
      const next = await native.codexStart({ question: text, model,
        ...(withCode ? { code: document.code } : {}),
        ...(withStatement ? { statement: statementText } : {}),
      });
      if (!mounted.current) return;
      setSent({ question: text, title: document.title, documentId: document.id }); setQuestion(""); accept(next);
    } catch (reason) { if (mounted.current) setError(safeError(reason).message); }
    finally { if (mounted.current) setBusy(false); }
  }

  async function cancel() {
    if (!poll || cancelling) return;
    setCancelling(true); setError(null);
    try { accept(await native.codexCancel(poll.requestId)); }
    catch (reason) { if (mounted.current) { setError(safeError(reason).message); setCancelling(false); } }
  }

  async function login() {
    setBusy(true); setError(null);
    try { setStatus(await native.codexLogin()); }
    catch (reason) { setError(safeError(reason).message); }
    finally { setBusy(false); }
  }

  return <section className="kontur-assistant local-ai" aria-label="Помощник" hidden={!active}>
    <header className="kontur-assistant-header"><strong>Помощник</strong><span className="local-muted">Codex CLI</span></header>
    <div className="kontur-ai-context">
      <span>{status?.state === "checking" || !status ? "Проверяем Codex CLI…" : status.state === "ready" ? "CLI доступен" : "Помощник недоступен"}</span>
      {status?.error && <span role="status">{status.error.message}</span>}
      <span>Для ответа нужен интернет. Редактор и сохранение работают локально.</span>
      {sent && sent.documentId !== document?.id && <span role="status">Этот ответ относится к работе «{sent.title}».</span>}
      {status?.state === "ready" && (status.loginState === "required" || status.loginState === "unknown") && <button type="button" className="outline-button" disabled={busy || running} onClick={() => void login()}>Войти в Codex</button>}
      {status?.loginState === "inProgress" && <span role="status">Завершите вход в открывшемся окне…</span>}
      {status?.state === "unavailable" && <button type="button" className="outline-button" onClick={() => setStatus(null)}>Проверить снова</button>}
    </div>
    <div ref={feed} className="kontur-chat-scroll" aria-busy={running || busy}>
      {!sent && !poll && <div className="kontur-ai-empty"><h2>Разберём решение</h2><p>Задайте вопрос и выберите, какие части текущей работы добавить к нему.</p></div>}
      {sent && <div className="message user"><div className="message-body"><small>{sent.title}</small><p>{sent.question}</p></div></div>}
      {poll && <div className="message assistant"><div className="message-body local-ai-answer"><p>{poll.text || (running ? "Готовим ответ…" : "Ответ пуст.")}</p></div></div>}
      <span className="sr-only" role="status">{running ? "Помощник отвечает" : poll?.state === "completed" ? "Ответ получен" : poll?.state === "cancelled" ? "Запрос остановлен" : ""}</span>
      {poll?.state === "cancelled" && <p role="status" className="local-muted">Запрос остановлен.</p>}
      {poll?.error && <p className="panel-error" role="alert">{poll.error.message}</p>}
      {error && <p className="panel-error" role="alert">{error}</p>}
    </div>
    <form className="local-ai-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
      <label className="local-field"><span>Модель</span><select value={modelId} disabled={busy || running} onChange={event => setModelId(event.target.value)}><option value="">По умолчанию в Codex CLI</option>{status?.manualModels.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
      <div className="local-ai-context-choices"><label><input type="checkbox" checked={withCode} onChange={event => setWithCode(event.target.checked)} />Добавить код</label><label><input type="checkbox" checked={withStatement} onChange={event => setWithStatement(event.target.checked)} />Добавить условие</label></div>
      <label className="sr-only" htmlFor="local-ai-question">Вопрос помощнику</label><textarea id="local-ai-question" rows={3} maxLength={16000} value={question} onChange={event => setQuestion(event.target.value)} placeholder="Спросите о решении…" />
      <p className="local-muted">Выбранный текст будет отправлен через Codex только по кнопке «Отправить».</p>
      {running ? <button type="button" className="outline-button" disabled={cancelling} onClick={() => void cancel()}>{cancelling ? "Останавливаем…" : "Остановить"}</button> : <button type="submit" className="primary-button" disabled={busy || !document || !question.trim() || status?.state !== "ready"}>{busy ? "Отправляем…" : "Отправить"}</button>}
    </form>
  </section>;
}
