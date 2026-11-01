"use client";

import { useEffect, useState } from "react";
import { DeviceFileRow } from "@/components/device-file-row";
import { downloadDeviceFile, loadDeviceTexts, type DeviceFile } from "@/lib/device-files";
import type { AssetRef } from "@/lib/types";
import styles from "./task-spreadsheet-files.module.css";

export function isSpreadsheetName(name: string): boolean {
  return /\.(xls|xlsx|ods|csv)$/i.test(name);
}

export interface TaskSpreadsheetFilesProps {
  asset: AssetRef;
  disabled?: boolean;
  revisionHash?: string;
  /** Controlled data lets the task runner reuse its prefetched device files. */
  files?: DeviceFile[];
  filesLoading?: boolean;
  fileError?: string | null;
  onRetry?: () => void;
  onDownloadFile?: (file: DeviceFile) => void | Promise<void>;
  /** Kept for callers that still pass the old callback; preparation is device-local now. */
  onPrepared?: (asset: AssetRef) => void;
}

export function TaskSpreadsheetFiles({
  asset,
  disabled = false,
  revisionHash = "",
  files,
  filesLoading = false,
  fileError = null,
  onRetry,
  onDownloadFile,
}: TaskSpreadsheetFilesProps) {
  const controlled = files !== undefined;
  const [localFiles, setLocalFiles] = useState<DeviceFile[]>([]);
  const [localLoading, setLocalLoading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    if (controlled) return undefined;
    const controller = new AbortController();
    setLocalFiles([]);
    setLocalError(null);
    setLocalLoading(true);
    void loadDeviceTexts(asset, revisionHash, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setLocalFiles(next);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setLocalError(reason instanceof Error ? reason.message : "Не удалось подготовить TXT.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLocalLoading(false);
      });
    return () => controller.abort();
  }, [asset, asset.id, controlled, revisionHash, retryToken]);

  const resolvedFiles = controlled ? files : localFiles;
  const resolvedLoading = controlled ? filesLoading : localLoading;
  const resolvedError = controlled ? fileError : localError;
  const retry = onRetry ?? (() => setRetryToken((value) => value + 1));

  if (resolvedLoading) return <div className={styles.tools} role="status"><span className="button-spinner" />Готовим TXT для Python…</div>;
  if (resolvedError) return <div className={styles.tools}><p className="inline-error" role="alert">{resolvedError}</p><button className="outline-button" disabled={disabled} onClick={retry} type="button">Повторить</button></div>;
  if (!resolvedFiles?.length) return null;

  return <div className={styles.tools} aria-label={`Текстовые файлы ${asset.name}`}>
    {resolvedFiles.map((file) => <DeviceFileRow
      file={file}
      disabled={disabled}
      downloadLabel="Скачать TXT"
      key={file.name}
      meta={file.sheetName ? `Лист: ${file.sheetName}` : undefined}
      onDownload={() => onDownloadFile ? onDownloadFile(file) : downloadDeviceFile(file)}
    />)}
    {resolvedFiles.length > 1 ? <small className={styles.note}>Каждый лист сохранён в отдельный TXT с табуляцией между столбцами.</small> : null}
  </div>;
}

