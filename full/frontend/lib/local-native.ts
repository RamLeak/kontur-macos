export interface LocalNativeError { code: string; message: string }
type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
const pendingActions = new Set<Promise<unknown>>();

export function runLocalAction<T>(work: () => Promise<T>): Promise<T> {
  const operation = Promise.resolve().then(work);
  pendingActions.add(operation);
  void operation.finally(() => pendingActions.delete(operation)).catch(() => undefined);
  return operation;
}
export async function waitForLocalActions(): Promise<void> {
  while (pendingActions.size) await Promise.all([...pendingActions]);
}

export async function localNative<T>(command: "codex_status" | "codex_login" | "local_import_preview" | "local_import_confirm" | "local_export_bundle" | "confirm_close", args?: Record<string, unknown>): Promise<T> {
  const target = typeof window === "undefined" ? undefined : window as unknown as { __TAURI__?: { core?: { invoke?: Invoke } } };
  const invoke = target?.__TAURI__?.core?.invoke;
  if (!invoke) throw new Error("Откройте установленное локальное приложение «Контур».");
  try {
    const operation = invoke<T>(command, args);
    if (command === "local_import_confirm" || command === "local_export_bundle") {
      pendingActions.add(operation);
      void operation.finally(() => pendingActions.delete(operation)).catch(() => undefined);
    }
    return await operation;
  }
  catch (reason) {
    if (reason && typeof reason === "object" && "message" in reason && typeof reason.message === "string") throw new Error(reason.message);
    throw new Error("Локальная операция не выполнена. Повторите попытку.");
  }
}

export interface LocalCodexStatus {
  state: "checking" | "ready" | "unavailable";
  cliVersion?: string;
  gatewayProtocol?: number;
  loginState: "unknown" | "inProgress" | "required" | "ready";
  manualModelSupported: boolean;
  manualModels: string[];
  error?: LocalNativeError;
}
