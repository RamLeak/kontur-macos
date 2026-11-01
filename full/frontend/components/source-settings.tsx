"use client";

import { useRef, useState } from "react";
import { Icon } from "@/components/icon";
import {
  DEMO_MODE,
  getCapabilities,
  getSourceHealth,
  syncConfiguredVariant,
} from "@/lib/api";
import { readableError } from "@/lib/ui-copy";
import type {
  Capabilities,
  SourceHealth,
  SourceVariantSyncResult,
} from "@/lib/types";
import styles from "./settings.module.css";

type CapabilityKey = "source_variant_sync" | "source_sync" | "asset_fetch" | "runner" | "codex";

/**
 * Infrequent source and service actions belong in Settings. Nothing here is
 * requested on mount: checking the source or asking the backend for service
 * capabilities only happens after the owner presses a button.
 */
export function SourceSettings({ onImport }: { onImport: () => void }) {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [syncResult, setSyncResult] = useState<SourceVariantSyncResult | null>(null);
  const [checkingConnections, setCheckingConnections] = useState(false);
  const [checkingHealth, setCheckingHealth] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sourceKeyRef = useRef<string | null>(null);

  async function checkConnections() {
    setCheckingConnections(true);
    setError(null);
    setNotice(null);
    try {
      setCapabilities(await getCapabilities());
      setNotice("Подключения проверены.");
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось проверить подключения. Повторите попытку."));
    } finally {
      setCheckingConnections(false);
    }
  }

  async function checkHealth() {
    setCheckingHealth(true);
    setError(null);
    setNotice(null);
    try {
      setHealth(await getSourceHealth());
      setNotice("Состояние источника обновлено.");
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось проверить состояние КЕГЭ. Повторите попытку."));
    } finally {
      setCheckingHealth(false);
    }
  }

  async function syncSourceVariant() {
    if (DEMO_MODE) {
      setError("В демонстрационном режиме обновление банка отключено.");
      return;
    }

    setSyncing(true);
    setError(null);
    setNotice(null);
    try {
      // Fetch the capability only after an explicit owner action. This keeps
      // source checks out of the normal Settings page load.
      const nextCapabilities = capabilities ?? await getCapabilities();
      setCapabilities(nextCapabilities);
      const nextSourceCapability = nextCapabilities.source_variant_sync ?? nextCapabilities.source_sync;
      if (!nextSourceCapability.available) {
        setError("Обновление банка сейчас недоступно. Проверьте подключения или обратитесь к настройкам сервера.");
        return;
      }

      const idempotencyKey = sourceKeyRef.current ?? `source-variant-sync-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      sourceKeyRef.current = idempotencyKey;
      const result = await syncConfiguredVariant(27, idempotencyKey);
      setSyncResult(result);
      sourceKeyRef.current = null;
      setNotice(`Вариант обновлён. Заданий: ${result.count ?? "число не указано"}.`);
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось обновить вариант КЕГЭ. Повторите попытку."));
    } finally {
      setSyncing(false);
    }
  }

  return (
    <section className={styles.sourcePanel} aria-labelledby="source-settings-title">
      <details className={styles.advancedDetails}>
        <summary className={styles.advancedSummary}>
          <span className={styles.summaryIcon}><Icon name="settings" size={17} /></span>
          <span className={styles.summaryCopy}>
            <strong id="source-settings-title">Подключения и источник заданий</strong>
            <small>КЕГЭ, файлы, помощник и запуск кода</small>
          </span>
          <span className={styles.summaryChevron}><Icon name="chevron-down" size={16} /></span>
        </summary>

        <div className={styles.advancedBody}>
          <div className={styles.advancedIntro}>
            <div>
              <span className="eyebrow">Дополнительно</span>
              <h2>Сервисы рабочей области</h2>
              <p className="muted-copy">Проверки запускаются по вашему действию. Задания и вложения добавляются из файла .kontur.</p>
            </div>
            <button className="secondary-button" disabled={checkingConnections} onClick={() => void checkConnections()} type="button">
              {checkingConnections ? <><span className="button-spinner" />Проверяем…</> : <><Icon name="refresh" size={15} />Проверить подключения</>}
            </button>
          </div>

          {notice ? <div className={styles.feedbackSuccess} role="status"><Icon name="check" size={15} />{notice}</div> : null}
          {error ? <div className={styles.feedbackError} role="alert"><Icon name="warning" size={15} />{error}</div> : null}

          <div className={styles.serviceGrid} aria-label="Состояние сервисов">
            {serviceKeys.map((key) => <ServiceRow key={key} capability={key === "source_variant_sync" ? capabilities?.source_variant_sync ?? capabilities?.source_sync : capabilities?.[key]} label={capabilityLabel(key)} />)}
          </div>

          <div className={styles.sourceActionCard}>
            <div className={styles.sourceActionCopy}>
              <span className="eyebrow">Источник задач</span>
              <h3>КЕГЭ · подготовка 2027</h3>
              <p>Данные хранятся на этом компьютере. Чтобы добавить задания, импортируйте файл .kontur.</p>
            </div>
            <div className={styles.sourceActionButtons}>
              <button className="secondary-button" disabled={checkingHealth} onClick={() => void checkHealth()} type="button">
                {checkingHealth ? <><span className="button-spinner" />Проверяем…</> : <><Icon name="refresh" size={15} />Проверить состояние</>}
              </button>
              <button className="primary-button" disabled={syncing || DEMO_MODE} onClick={onImport} type="button">
                Импортировать .kontur
              </button>
            </div>
          </div>

          {health ? <HealthSummary health={health} /> : <p className={styles.mutedHint}>Состояние источника ещё не проверялось.</p>}
          {syncResult ? <SyncSummary result={syncResult} /> : null}
        </div>
      </details>
    </section>
  );
}

const serviceKeys: CapabilityKey[] = ["source_variant_sync", "asset_fetch", "runner", "codex"];

function ServiceRow({ capability, label }: { capability: Capabilities[CapabilityKey] | undefined; label: string }) {
  if (!capability) {
    return (
      <div className={styles.serviceRow}>
        <span className={styles.serviceDot} />
        <span className={styles.serviceCopy}><strong>{label}</strong><small>Не проверено</small></span>
      </div>
    );
  }
  return (
    <div className={styles.serviceRow}>
      <span className={`${styles.serviceDot} ${capability.available ? styles.available : styles.unavailable}`} />
      <span className={styles.serviceCopy}>
        <strong>{label}</strong>
        <small>{capability.available ? "Доступно" : capabilityHint(label)}</small>
      </span>
    </div>
  );
}

function HealthSummary({ health }: { health: SourceHealth }) {
  const coverage = Object.entries(health.coverage);
  return (
    <div className={styles.resultCard} role="status">
      <div className={styles.resultHeading}><Icon name="check" size={15} /><strong>Состояние источника получено</strong></div>
      <div className={styles.resultFacts}>
        <span><small>Источник</small><strong>{health.source || "КЕГЭ"}</strong></span>
        <span><small>Редакция</small><strong>{health.last_sync ? health.last_sync.status === "succeeded" ? "Обновлена" : "Есть замечания" : "Ещё не обновлялась"}</strong></span>
        {coverage.length ? <span><small>Покрытие</small><strong>{coverage.reduce((total, [, count]) => total + count, 0)} заданий</strong></span> : null}
      </div>
    </div>
  );
}

function SyncSummary({ result }: { result: SourceVariantSyncResult }) {
  return (
    <div className={styles.resultCard} role="status">
      <div className={styles.resultHeading}><Icon name="check" size={15} /><strong>Последнее обновление варианта</strong></div>
      <div className={styles.resultFacts}>
        <span><small>Заданий</small><strong>{result.count ?? "—"}</strong></span>
        <span><small>Ошибок</small><strong>{result.failures.length ? result.failures.length : "Нет"}</strong></span>
        {result.target_exam_year ? <span><small>Подготовка</small><strong>ЕГЭ-{result.target_exam_year}</strong></span> : null}
      </div>
    </div>
  );
}

function capabilityLabel(key: CapabilityKey): string {
  return {
    source_variant_sync: "Банк КЕГЭ",
    source_sync: "Банк КЕГЭ",
    asset_fetch: "Файлы заданий",
    runner: "Запуск кода",
    codex: "Наставник Codex",
  }[key];
}

function capabilityHint(label: string): string {
  if (label === "Запуск кода") return "Ожидает защищённую среду";
  if (label === "Банк КЕГЭ") return "Обновление отключено";
  return "Пока недоступно";
}
