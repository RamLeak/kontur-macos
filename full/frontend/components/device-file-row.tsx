"use client";

import { useState } from "react";
import { Icon } from "@/components/icon";
import type { DeviceFile } from "@/lib/device-files";
import styles from "./device-file-row.module.css";

export interface DeviceFileRowProps {
  file?: Pick<DeviceFile, "name" | "bytes" | "mediaType">;
  name?: string;
  meta?: string;
  sizeBytes?: number | null;
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  downloadLabel?: string;
  onDownload: () => void | Promise<void>;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`;
  return `${(value / (1024 * 1024)).toFixed(1)} МБ`;
}

export function DeviceFileRow({
  file,
  name,
  meta,
  sizeBytes,
  disabled = false,
  loading = false,
  error = null,
  downloadLabel = "Скачать",
  onDownload,
}: DeviceFileRowProps) {
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const displayName = file?.name ?? name ?? "Файл";
  const displaySize = file?.bytes.byteLength ?? sizeBytes;
  const isBusy = loading || busy;

  async function handleDownload() {
    if (disabled || isBusy) return;
    setBusy(true);
    setLocalError(null);
    try {
      await onDownload();
    } catch (reason) {
      setLocalError(reason instanceof Error ? reason.message : "Не удалось скачать файл.");
    } finally {
      setBusy(false);
    }
  }

  return <div className={styles.row}>
    <Icon name="file" size={15} />
    <div className={styles.info}>
      <strong title={displayName}>{displayName}</strong>
      <span>{meta ?? (displaySize != null ? formatBytes(displaySize) : "")}</span>
    </div>
    <button className="tiny-action" disabled={disabled || isBusy} onClick={() => void handleDownload()} type="button">
      {isBusy ? "Загружаем…" : downloadLabel}
    </button>
    {error || localError ? <p className={styles.error} role="alert">{error ?? localError}</p> : null}
  </div>;
}

