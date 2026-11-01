"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  downloadDeviceFile,
  loadDeviceAsset,
  loadTaskDeviceFiles,
  type DeviceFile,
} from "@/lib/device-files";
import { startBrowserPython, type BrowserPythonJob } from "@/lib/browser-python";
import type { AssetRef, CodeRun } from "@/lib/types";

const DEVICE_RUNS_KEY = "kontur:device-python-runs:v1:";
const MAX_SAVED_RUNS = 20;

export interface UseDevicePythonOptions {
  assets: AssetRef[];
  localFiles?: DeviceFile[];
  lineNumber: number | null | undefined;
  revisionHash?: string;
  scope: string;
  enabled: boolean;
}

export interface UseDevicePythonResult {
  files: DeviceFile[];
  fileError: string | null;
  filesLoading: boolean;
  running: boolean;
  run: (code: string, version: number) => Promise<CodeRun>;
  stop: () => void;
  retryFiles: () => void;
  download: (asset: AssetRef) => Promise<void>;
}

function runStorageKey(scope: string): string {
  return `${DEVICE_RUNS_KEY}${encodeURIComponent(scope)}`;
}

function isCodeRun(value: unknown): value is CodeRun {
  if (!value || typeof value !== "object") return false;
  const run = value as Partial<CodeRun>;
  return typeof run.id === "string"
    && typeof run.status === "string"
    && typeof run.stdout === "string"
    && typeof run.stderr === "string"
    && typeof run.created_at === "string";
}

/**
 * Browser runs are deliberately kept outside the server exam history. This
 * reader is useful to a view restoring the latest local output for a scope.
 */
export function readDevicePythonRuns(scope: string): CodeRun[] {
  if (typeof window === "undefined") return [];
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(runStorageKey(scope)) ?? "[]");
    return Array.isArray(value) ? value.filter(isCodeRun).slice(0, MAX_SAVED_RUNS) : [];
  } catch {
    return [];
  }
}

function saveDevicePythonRun(scope: string, run: CodeRun): void {
  if (typeof window === "undefined") return;
  try {
    const runs = [run, ...readDevicePythonRuns(scope).filter((item) => item.id !== run.id)].slice(0, MAX_SAVED_RUNS);
    window.localStorage.setItem(runStorageKey(scope), JSON.stringify(runs));
  } catch {
    // Local persistence is best-effort. The current result remains available
    // to the caller even when storage is disabled or full.
  }
}

function isAbortError(reason: unknown): boolean {
  return typeof DOMException !== "undefined" && reason instanceof DOMException && reason.name === "AbortError";
}

export function useDevicePython({
  assets,
  localFiles = [],
  lineNumber,
  revisionHash = "",
  scope,
  enabled,
}: UseDevicePythonOptions): UseDevicePythonResult {
  const [files, setFiles] = useState<DeviceFile[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [filesLoading, setFilesLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const filesAbortRef = useRef<AbortController | null>(null);
  const activeJobRef = useRef<BrowserPythonJob | null>(null);
  const mountedRef = useRef(true);
  const scopeRef = useRef(scope);
  const assetsRef = useRef(assets);
  const localFilesRef = useRef(localFiles);
  scopeRef.current = scope;
  assetsRef.current = assets;
  localFilesRef.current = localFiles;

  const assetsKey = useMemo(
    () => assets.map((asset) => [asset.id, asset.name, asset.media_type, asset.size_bytes ?? "", asset.content_hash ?? "", asset.available ? "1" : "0"].join("\u001f")).join("\u001e"),
    [assets],
  );
  const localFilesKey = useMemo(
    () => localFiles.map((file) => [file.name, file.sha256, file.bytes.byteLength].join("\u001f")).join("\u001e"),
    [localFiles],
  );

  useEffect(() => {
    mountedRef.current = true;
    const previousJob = activeJobRef.current;
    activeJobRef.current = null;
    previousJob?.stop();
    filesAbortRef.current?.abort();
    filesAbortRef.current = null;
    setRunning(false);
    setFiles([]);
    setFileError(null);

    if (!enabled) {
      setFilesLoading(false);
      return () => {
        mountedRef.current = false;
        filesAbortRef.current?.abort();
        activeJobRef.current?.stop();
        activeJobRef.current = null;
      };
    }

    const controller = new AbortController();
    filesAbortRef.current = controller;
    setFilesLoading(true);
    void loadTaskDeviceFiles(assetsRef.current, lineNumber ?? 0, revisionHash, controller.signal, localFilesRef.current)
      .then((next) => {
        if (controller.signal.aborted || !mountedRef.current) return;
        setFiles(next);
        setFileError(null);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || isAbortError(reason) || !mountedRef.current) return;
        setFiles([]);
        setFileError(reason instanceof Error ? reason.message : "Не удалось загрузить файлы задания.");
      })
      .finally(() => {
        if (!controller.signal.aborted && mountedRef.current) setFilesLoading(false);
      });

    return () => {
      controller.abort();
      if (filesAbortRef.current === controller) filesAbortRef.current = null;
      activeJobRef.current?.stop();
      activeJobRef.current = null;
      setRunning(false);
    };
  }, [assetsKey, localFilesKey, lineNumber, revisionHash, scope, enabled, retryToken]);

  useEffect(() => () => {
    mountedRef.current = false;
    filesAbortRef.current?.abort();
    activeJobRef.current?.stop();
    activeJobRef.current = null;
  }, []);

  const run = useCallback(async (code: string, version: number): Promise<CodeRun> => {
    if (!enabled) throw new Error("Локальный запуск сейчас недоступен.");
    if (filesLoading) throw new Error("Файлы задания ещё загружаются.");
    if (fileError) throw new Error(fileError);
    if (activeJobRef.current) throw new Error("Программа уже выполняется.");
    const job = startBrowserPython(code, files, version);
    activeJobRef.current = job;
    setRunning(true);
    try {
      const result = await job.result;
      if (activeJobRef.current === job) activeJobRef.current = null;
      if (mountedRef.current && activeJobRef.current === null) setRunning(false);
      if (mountedRef.current && scopeRef.current === scope) saveDevicePythonRun(scope, result);
      return result;
    } catch (reason) {
      if (activeJobRef.current === job) {
        activeJobRef.current = null;
        if (mountedRef.current && scopeRef.current === scope) setRunning(false);
      }
      throw reason;
    }
  }, [enabled, fileError, files, filesLoading, scope]);

  const stop = useCallback(() => {
    activeJobRef.current?.stop();
  }, []);

  const retryFiles = useCallback(() => {
    if (enabled) setRetryToken((value) => value + 1);
  }, [enabled]);

  const download = useCallback(async (asset: AssetRef): Promise<void> => {
    if (!enabled) throw new Error("Файлы задания сейчас недоступны.");
    const file = await loadDeviceAsset(asset, revisionHash);
    downloadDeviceFile(file);
  }, [enabled, revisionHash]);

  return { files, fileError, filesLoading, running, run, stop, retryFiles, download };
}

