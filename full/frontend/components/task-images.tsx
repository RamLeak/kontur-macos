"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/icon";
import { deviceAssetUrl, loadDeviceAsset } from "@/lib/device-files";
import type { AssetRef } from "@/lib/types";
import { readableError } from "@/lib/ui-copy";
import styles from "./task-images.module.css";

const supportedImageTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const visibilityRootMargin = "240px 0px";
const fallbackVisibleImages = 2;

interface TaskImagesProps {
  assets: AssetRef[];
  canPrepare: boolean;
  onAssetPrepared?: (asset: AssetRef) => void;
  revisionHash?: string;
}

interface ImageState {
  error: string | null;
  loading: boolean;
  retry: number;
  src: string | null;
}

type MessageMap = Record<string, string>;

export function TaskImages({ assets, canPrepare, revisionHash = "" }: TaskImagesProps) {
  const imageAssets = useMemo(() => assets.filter(isSupportedImage), [assets]);
  const headingId = useId();
  const [imageStates, setImageStates] = useState<Record<string, ImageState>>({});
  const [visibleAssets, setVisibleAssets] = useState<Record<string, boolean>>({});
  const [messages, setMessages] = useState<MessageMap>({});
  const imageNodesRef = useRef<Record<string, HTMLElement | null>>({});
  const startedRef = useRef(new Set<string>());
  const objectUrlsRef = useRef(new Map<string, string>());
  const loadControllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const imageAssetsKey = useMemo(
    () => imageAssets.map((asset) => `${asset.id}:${asset.content_hash ?? ""}`).join("\u001e"),
    [imageAssets],
  );

  useEffect(() => {
    const controller = new AbortController();
    const previous = loadControllerRef.current;
    loadControllerRef.current = controller;
    previous?.abort();
    startedRef.current.clear();
    for (const objectUrl of objectUrlsRef.current.values()) URL.revokeObjectURL(objectUrl);
    objectUrlsRef.current.clear();
    setImageStates({});
    setVisibleAssets({});
    setMessages({});
    return () => {
      controller.abort();
      if (loadControllerRef.current === controller) loadControllerRef.current = null;
    };
  }, [imageAssetsKey, revisionHash]);

  useEffect(() => {
    mountedRef.current = true;
    if (!imageAssets.length) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisibleAssets((current) => {
        const next = { ...current };
        for (const asset of imageAssets.slice(0, fallbackVisibleImages)) next[asset.id] = true;
        return next;
      });
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      setVisibleAssets((current) => {
        const next = { ...current };
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const assetId = (entry.target as HTMLElement).dataset.assetId;
            if (assetId) next[assetId] = true;
          }
        }
        return next;
      });
    }, { rootMargin: visibilityRootMargin, threshold: 0.01 });
    for (const asset of imageAssets) {
      const node = imageNodesRef.current[asset.id];
      if (node) {
        node.dataset.assetId = asset.id;
        observer.observe(node);
      }
    }
    return () => observer.disconnect();
  }, [canPrepare, imageAssets]);

  useEffect(() => {
    for (const asset of imageAssets) {
      const loadKey = `${revisionHash}:${asset.id}`;
      if (!visibleAssets[asset.id] || startedRef.current.has(loadKey) || asset.cache_state === "quarantined") continue;
      startedRef.current.add(loadKey);
      void loadImage(asset, loadControllerRef.current?.signal);
    }
  }, [canPrepare, imageAssets, revisionHash, visibleAssets]);

  useEffect(() => {
    const activeIds = new Set(imageAssets.map((asset) => asset.id));
    for (const [assetId, objectUrl] of objectUrlsRef.current) {
      if (!activeIds.has(assetId)) {
        URL.revokeObjectURL(objectUrl);
        objectUrlsRef.current.delete(assetId);
      }
    }
  }, [imageAssets]);

  useEffect(() => () => {
    mountedRef.current = false;
    for (const objectUrl of objectUrlsRef.current.values()) URL.revokeObjectURL(objectUrl);
    objectUrlsRef.current.clear();
  }, []);

  async function loadImage(asset: AssetRef, signal?: AbortSignal) {
    const loadKey = `${revisionHash}:${asset.id}`;
    setImageStates((current) => ({
      ...current,
      [asset.id]: { error: null, loading: true, retry: current[asset.id]?.retry ?? 0, src: current[asset.id]?.src ?? null },
    }));
    setMessages((current) => withoutMessage(current, asset.id));
    try {
      const file = await loadDeviceAsset(asset, revisionHash, signal);
      if (!supportedImageTypes.has(file.mediaType.trim().toLowerCase())) {
        throw new Error("Сервер вернул файл неподдерживаемого типа.");
      }
      if (asset.available && asset.content_hash && file.sha256 && asset.content_hash.toLowerCase() !== file.sha256.toLowerCase()) {
        throw new Error("Проверка целостности изображения не пройдена.");
      }
      const objectUrl = deviceAssetUrl(file);
      if (signal?.aborted || !mountedRef.current) {
        URL.revokeObjectURL(objectUrl);
        return;
      }
      const previousUrl = objectUrlsRef.current.get(asset.id);
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      objectUrlsRef.current.set(asset.id, objectUrl);
      setImageStates((current) => ({
        ...current,
        [asset.id]: { error: null, loading: false, retry: current[asset.id]?.retry ?? 0, src: objectUrl },
      }));
    } catch (reason: unknown) {
      startedRef.current.delete(loadKey);
      if (signal?.aborted || isAbortError(reason)) return;
      if (!mountedRef.current) return;
      const message = readableError(reason, "Не удалось загрузить иллюстрацию.");
      setImageStates((current) => ({
        ...current,
        [asset.id]: { error: message, loading: false, retry: current[asset.id]?.retry ?? 0, src: null },
      }));
      setMessages((current) => ({ ...current, [asset.id]: message }));
    }
  }

  function retryImage(asset: AssetRef) {
    setMessages((current) => withoutMessage(current, asset.id));
    startedRef.current.delete(`${revisionHash}:${asset.id}`);
    setVisibleAssets((current) => ({ ...current, [asset.id]: true }));
  }

  function handleImageError(assetId: string) {
    const objectUrl = objectUrlsRef.current.get(assetId);
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrlsRef.current.delete(assetId);
    }
    setImageStates((current) => ({
      ...current,
      [assetId]: { error: "Не удалось отобразить иллюстрацию.", loading: false, retry: current[assetId]?.retry ?? 0, src: null },
    }));
  }

  if (!imageAssets.length) return null;

  return (
    <section className={styles.taskImages} aria-labelledby={headingId}>
      <div className={styles.heading}>
        <strong id={headingId}>Иллюстрации к условию</strong>
      </div>
      <div className={styles.grid} role="list" aria-label="Изображения задания">
        {imageAssets.map((asset, index) => {
          const state = imageStates[asset.id];
          const imageUrl = state?.src ?? null;
          const waitingForVisibility = !visibleAssets[asset.id] && !state?.src;
          const message = (messages[asset.id] ?? state?.error)
            ?? (waitingForVisibility ? "Иллюстрация загрузится при просмотре." : null)
            ?? (asset.available && !imageUrl && !state?.loading ? "Иллюстрация недоступна для просмотра." : null)
            ?? (!asset.available && !imageUrl && !state?.loading ? imageStateMessage(asset, canPrepare) : null);
          const busy = Boolean(state?.loading);
          const canRetry = Boolean(state?.error || messages[asset.id]) && (asset.available || asset.cache_state !== "quarantined");
          const label = `Иллюстрация ${index + 1}`;
          return (
            <figure
              aria-busy={busy}
              className={styles.card}
              key={asset.id}
              ref={(node) => { imageNodesRef.current[asset.id] = node; }}
              role="listitem"
            >
              {imageUrl && !message ? (
                <img
                  alt={label}
                  className={styles.image}
                  decoding="async"
                  draggable={false}
                  key={`${asset.id}-${state?.retry ?? 0}`}
                  loading="lazy"
                  onError={() => handleImageError(asset.id)}
                  src={imageUrl}
                />
              ) : (
                <div className={styles.placeholder} role="status">
                  <Icon name={busy ? "refresh" : message ? "warning" : "file"} size={18} />
                  <p>{busy ? "Загружаем иллюстрацию…" : message ?? imageStateMessage(asset, canPrepare)}</p>
                  {message && canRetry ? (
                    <button className="tiny-action" disabled={busy} onClick={() => retryImage(asset)} type="button">
                      Повторить
                    </button>
                  ) : null}
                </div>
              )}
            </figure>
          );
        })}
      </div>
    </section>
  );
}

function isSupportedImage(asset: AssetRef): boolean {
  return supportedImageTypes.has(asset.media_type.trim().toLowerCase());
}

function imageStateMessage(asset: AssetRef, canPrepare: boolean): string {
  if (asset.cache_state === "fetching") return "Иллюстрация готовится…";
  if (asset.cache_state === "quarantined") return "Иллюстрация недоступна для просмотра.";
  if (asset.cache_state === "failed") return "Не удалось загрузить иллюстрацию.";
  if (!canPrepare) return "Иллюстрация недоступна для просмотра.";
  return "Загружаем иллюстрацию…";
}

function withoutMessage(current: MessageMap, assetId: string): MessageMap {
  const next = { ...current };
  delete next[assetId];
  return next;
}

function isAbortError(reason: unknown): boolean {
  return (typeof DOMException !== "undefined" && reason instanceof DOMException && reason.name === "AbortError")
    || (reason instanceof Error && reason.name === "AbortError");
}
