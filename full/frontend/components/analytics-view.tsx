"use client";

import { useEffect, useState } from "react";
import { Icon } from "@/components/icon";
import { DEMO_MODE, getAnalytics, rebuildAnalytics } from "@/lib/api";
import type { AnalyticsOverview } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";

export function AnalyticsView() {
  const [analytics, setAnalytics] = useState<AnalyticsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getAnalytics()
      .then((value) => { if (!cancelled) setAnalytics(value); })
      .catch((reason: unknown) => { if (!cancelled) setError(readableError(reason, "Не удалось загрузить статистику. Обновите страницу и попробуйте снова.")); });
    return () => { cancelled = true; };
  }, []);

  async function handleRebuild() {
    setRebuilding(true);
    setError(null);
    try {
      setAnalytics(await rebuildAnalytics());
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось обновить статистику. Попробуйте ещё раз позже."));
    } finally {
      setRebuilding(false);
    }
  }

  if (error && !DEMO_MODE) {
    return <main className="page-state"><div className="state-icon state-icon-error"><Icon name="warning" size={22} /></div><h1>Аналитика недоступна</h1><p>{error}</p><p className="state-help">Сохранённые решения и попытки не изменены.</p></main>;
  }

  return (
    <main className="page-content analytics-page">
      <header className="page-heading">
        <div><span className="eyebrow">Только подтверждённые события</span><h1>Аналитика</h1><p className="page-lead">Сводка попыток, самостоятельности и покрытия тем за доступный период.</p></div>
        <button className="outline-button" type="button" disabled={rebuilding} onClick={handleRebuild}><Icon name="refresh" size={15} />{rebuilding ? "Обновляем…" : "Обновить"}</button>
      </header>
      {DEMO_MODE ? <div className="demo-inline-note" role="note"><Icon name="spark" size={15} /><span>TEST_ONLY preview: цифры демонстрационные и не являются прогнозом результата.</span></div> : null}
      {analytics?.available === false ? <div className="analytics-notice" role="status"><Icon name="info" size={15} /><span>Статистика ещё не собрана. Нажмите «Обновить», чтобы собрать её по сохранённым попыткам.</span></div> : null}
      <section className="metric-strip analytics-metrics">
        <Metric label={analytics?.solved_count != null ? "Решённые попытки" : "Попытки"} value={analytics?.solved_count != null ? String(analytics.solved_count) : analytics?.attempt_count != null ? String(analytics.attempt_count) : "—"} suffix="попыток" />
        <Metric label={analytics?.independent_rate != null ? "Самостоятельно" : "Точность"} value={(analytics?.independent_rate ?? analytics?.accuracy) != null ? `${analytics?.independent_rate ?? analytics?.accuracy}%` : "—"} />
        <Metric label="Среднее время" value={analytics?.average_minutes != null ? `${analytics.average_minutes}` : "—"} suffix="мин" />
        <Metric label="Покрытие карты" value={analytics?.coverage != null ? `${analytics.coverage}%` : "—"} />
      </section>
      <div className="analytics-grid">
        <section className="open-section chart-section">
          <div className="section-heading-row"><div><span className="eyebrow">Активность</span><h2>События обучения</h2></div><span className="muted-inline">по дням</span></div>
          {analytics?.weekly.length ? <WeeklyChart data={analytics.weekly} /> : analytics ? <p className="muted-copy">Детализация по дням пока недоступна.</p> : <div className="loading-row"><span className="button-spinner" /> Собираем сводку…</div>}
          <p className="chart-caption"><Icon name="info" size={14} /> Учитываются только сохранённые учебные события.</p>
        </section>
        <section className="open-section evidence-section">
          <div className="section-heading-row"><div><span className="eyebrow">Основание</span><h2>Доказательства</h2></div><Icon name="target" size={18} /></div>
          <div className="evidence-total">{analytics?.evidence_count ?? "—"}<small>событий обучения</small></div>
          <div className="evidence-list"><EvidenceRow label={analytics?.independent_rate != null ? "Независимый успешный результат" : "Подтверждённая точность"} value={(analytics?.independent_rate ?? analytics?.accuracy) != null ? `${analytics?.independent_rate ?? analytics?.accuracy}%` : "—"} /><EvidenceRow label="Покрытие навыков" value={analytics?.coverage != null ? `${analytics.coverage}%` : "—"} /><EvidenceRow label="Ручная проверка" value="Требуется" /></div>
        </section>
      </div>
    </main>
  );
}

function WeeklyChart({ data }: { data: AnalyticsOverview["weekly"] }) {
  const max = Math.max(...data.map((item) => item.value), 1);
  return <div className="weekly-chart" role="img" aria-label="Учебные события по дням">{data.map((item) => <div className="chart-column" key={item.label}><div className="chart-value">{item.value}</div><div className="chart-bar-track"><span style={{ height: `${Math.max(8, (item.value / max) * 100)}%` }} /></div><small>{item.label}</small></div>)}</div>;
}

function EvidenceRow({ label, value }: { label: string; value: string }) {
  return <div className="evidence-row"><span><span className="status-dot" />{label}</span><strong>{value}</strong></div>;
}

function Metric({ label, value, suffix }: { label: string; value: string; suffix?: string }) {
  return <div className="metric-item"><span>{label}</span><strong>{value}{suffix ? <small>{suffix}</small> : null}</strong></div>;
}
