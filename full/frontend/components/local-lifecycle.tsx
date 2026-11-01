"use client";

import { useEffect, useState } from "react";
import { getOwnerSaveState, flushOwnerWork } from "@/lib/owner-sync";
import { performLocalClose } from "@/lib/local-close";
import { localNative, waitForLocalActions } from "@/lib/local-native";
import { waitForLocalMutations } from "@/lib/local-fetch";
import { lockLocalInteraction } from "@/lib/local-interaction-lock";

export function LocalLifecycle() {
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const target = window as unknown as { __KONTUR_LOCAL_REQUEST_CLOSE__?: (input: { nonce: string }) => Promise<void> };
    let pending = false;
    let mounted = true;
    const close = async ({ nonce }: { nonce: string }) => {
      if (pending) throw new Error("Сохранение перед закрытием уже выполняется.");
      pending = true; setError(null);
      const release = lockLocalInteraction();
      try {
        await performLocalClose(nonce, {
          mutations: async () => { await waitForLocalMutations(); await waitForLocalActions(); },
          flush: flushOwnerWork,
          read: getOwnerSaveState,
          confirm: value => localNative<void>("confirm_close", { nonce: value, savedDocuments: [], allFlushed: true }),
        });
      } catch (reason) {
        if (mounted) setError(reason instanceof Error ? reason.message : "Не удалось сохранить работу перед закрытием.");
        throw reason;
      } finally {
        pending = false;
        release();
      }
    };
    target.__KONTUR_LOCAL_REQUEST_CLOSE__ = close;
    return () => { mounted = false; if (target.__KONTUR_LOCAL_REQUEST_CLOSE__ === close) delete target.__KONTUR_LOCAL_REQUEST_CLOSE__; };
  }, []);
  return error ? <div className="session-notice" role="alert">{error}</div> : null;
}
