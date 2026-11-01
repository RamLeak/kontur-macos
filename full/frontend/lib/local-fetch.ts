const LOCAL_API_COMMAND = "local_api_request" as const;

export const MAX_LOCAL_REQUEST_BYTES = 8 * 1024 * 1024;
export const MAX_LOCAL_RESPONSE_BYTES = 64_000_000;
const MAX_LOCAL_HEADER_ENTRIES = 3;
const MAX_LOCAL_HEADER_VALUE_BYTES = 1024;

const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const MAX_BASE64_LENGTH = Math.ceil(MAX_LOCAL_RESPONSE_BYTES / 3) * 4;
const pendingMutations = new Set<Promise<unknown>>();

export interface LocalApiRequest {
  method: string;
  path: string;
  body?: string;
  headers?: Record<string, string>;
}

export interface LocalApiResponseEnvelope {
  status: number;
  headers: Array<[string, string]>;
  bodyBase64: string;
}

type TauriInvoke = (command: string, args?: unknown) => Promise<unknown>;
type GlobalWithTauri = typeof globalThis & {
  __TAURI__?: { core?: { invoke?: unknown } };
  window?: {
    __TAURI__?: { core?: { invoke?: unknown } };
    location?: { origin?: string };
  };
};

export class LocalFetchError extends TypeError {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "LocalFetchError";
    this.code = code;
  }
}

function globalWithTauri(): GlobalWithTauri {
  return globalThis as GlobalWithTauri;
}

function getInvoke(): TauriInvoke | null {
  const scope = globalWithTauri();
  try {
    const tauri = scope.__TAURI__ ?? scope.window?.__TAURI__;
    const core = tauri?.core;
    if (!core || typeof core.invoke !== "function") return null;
    return core.invoke.bind(core) as TauriInvoke;
  } catch {
    return null;
  }
}

export function isLocalDesktop(): boolean {
  return getInvoke() !== null;
}

function abortError(): Error {
  if (typeof DOMException === "function") return new DOMException("The operation was aborted.", "AbortError");
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

function isAbortError(reason: unknown): boolean {
  return Boolean(reason && typeof reason === "object" && (reason as { name?: unknown }).name === "AbortError");
}

function throwIfAborted(signals: readonly AbortSignal[]): void {
  if (signals.some((signal) => signal.aborted)) throw abortError();
}

function abortable<T>(promise: Promise<T>, signals: readonly AbortSignal[]): Promise<T> {
  const active = signals.filter(Boolean);
  if (!active.length) return promise;
  if (active.some(signal => signal.aborted)) {
    // Invocation may have synchronously caused cancellation before this
    // wrapper subscribes; still consume its eventual rejection.
    void promise.catch(() => undefined);
    throw abortError();
  }
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = () => active.forEach((signal) => signal.removeEventListener("abort", onAbort));
    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(abortError());
    };
    active.forEach((signal) => signal.addEventListener("abort", onAbort, { once: true }));
    promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (reason) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(reason);
      },
    );
  });
}

function localError(code: string, message: string): LocalFetchError {
  return new LocalFetchError(code, message);
}

function decodePath(raw: string): string {
  if (!raw || !raw.startsWith("/")) throw localError("INVALID_LOCAL_PATH", "Локальный API принимает только путь приложения.");
  if (raw.includes("#") || raw.includes("\\") || /[\u0000-\u001f\u007f]/.test(raw)) {
    throw localError("INVALID_LOCAL_PATH", "Путь локального API недопустим.");
  }
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    throw localError("INVALID_LOCAL_PATH", "Путь локального API недопустим.");
  }
  if (decoded.includes("\\") || /[\u0000-\u001f\u007f]/.test(decoded)) {
    throw localError("INVALID_LOCAL_PATH", "Путь локального API недопустим.");
  }
  const queryAt = decoded.indexOf("?");
  const pathname = queryAt < 0 ? decoded : decoded.slice(0, queryAt);
  if (!pathname.startsWith("/api/v1/")) {
    throw localError("INVALID_LOCAL_PATH", "Локальный API принимает только путь /api/v1/.");
  }
  if (pathname.split("/").some((segment) => segment === "." || segment === "..")) {
    throw localError("INVALID_LOCAL_PATH", "Путь локального API недопустим.");
  }
  return raw;
}

function requestInput(input: RequestInfo | URL): { path: string; request: Request | null } {
  if (typeof input === "string") return { path: decodePath(input), request: null };
  if (typeof Request !== "undefined" && input instanceof Request) {
    let url: URL;
    try {
      url = new URL(input.url);
    } catch {
      throw localError("INVALID_LOCAL_PATH", "Путь локального API недопустим.");
    }
    const origin = globalWithTauri().window?.location?.origin;
    if (!origin || url.origin !== origin) {
      throw localError("INVALID_LOCAL_PATH", "Локальный API не принимает абсолютные URL.");
    }
    return { path: decodePath(url.pathname + url.search), request: input };
  }
  throw localError("INVALID_LOCAL_PATH", "Локальный API принимает только путь приложения.");
}

function requestMethod(request: Request | null, init: RequestInit | undefined): string {
  const raw = init?.method ?? request?.method ?? "GET";
  if (typeof raw !== "string") throw localError("INVALID_LOCAL_METHOD", "Метод локального API недопустим.");
  const method = raw.trim().toUpperCase();
  if (!ALLOWED_METHODS.has(method)) throw localError("INVALID_LOCAL_METHOD", "Метод локального API недопустим.");
  return method;
}

function requestHeaders(request: Request | null, init: RequestInit | undefined): Record<string, string> | undefined {
  const headers = new Headers();
  try {
    if (request) request.headers.forEach((value, name) => headers.append(name, value));
    if (init?.headers) new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  } catch {
    throw localError("INVALID_LOCAL_HEADERS", "Заголовки локального запроса недопустимы.");
  }
  const normalized: Record<string, string> = {};
  if ([...headers].length > MAX_LOCAL_HEADER_ENTRIES) {
    throw localError("LOCAL_HEADERS_TOO_LARGE", "Локальный API принимает не более трёх заголовков.");
  }
  for (const [name, value] of headers) {
    if (name !== "content-type" && name !== "idempotency-key" && name !== "x-sync-owner") {
      throw localError("LOCAL_HEADERS_FORBIDDEN", "Локальный API не принимает browser auth, owner или custom headers.");
    }
    if (/[\u0000-\u001f\u007f-\u009f]/.test(value) || new TextEncoder().encode(value).byteLength > MAX_LOCAL_HEADER_VALUE_BYTES) {
      throw localError("INVALID_LOCAL_HEADERS", "Значение заголовка локального запроса недопустимо.");
    }
    if (name === "content-type" && !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(value.trim())) {
      throw localError("LOCAL_HEADERS_FORBIDDEN", "Локальный API принимает только JSON Content-Type.");
    }
    normalized[name] = value;
  }
  return Object.keys(normalized).length ? normalized : undefined;
}

function bodySize(body: string): void {
  const bytes = new TextEncoder().encode(body).byteLength;
  if (bytes > MAX_LOCAL_REQUEST_BYTES) throw localError("LOCAL_REQUEST_TOO_LARGE", "Тело локального запроса слишком велико.");
}

function trackMutation(promise: Promise<unknown>): Promise<unknown> {
  pendingMutations.add(promise);
  void promise.finally(() => pendingMutations.delete(promise)).catch(() => undefined);
  return promise;
}

export async function waitForLocalMutations(): Promise<void> {
  while (pendingMutations.size) {
    const results = await Promise.allSettled([...pendingMutations]);
    const failure = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
    if (failure) throw localError("LOCAL_MUTATION_FAILED", "Локальная операция не завершилась успешно.");
  }
}

function concatBytes(chunks: Uint8Array[], size: number): Uint8Array {
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

async function readRequestBody(request: Request, signals: readonly AbortSignal[]): Promise<string | undefined> {
  if (!request.body) return undefined;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    reader = request.clone().body?.getReader();
  } catch {
    throw localError("INVALID_REQUEST_BODY", "Тело локального запроса недоступно.");
  }
  if (!reader) return undefined;
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await abortable(reader.read(), signals);
      if (part.done) break;
      size += part.value.byteLength;
      if (size > MAX_LOCAL_REQUEST_BYTES) {
        await reader.cancel();
        throw localError("LOCAL_REQUEST_TOO_LARGE", "Тело локального запроса слишком велико.");
      }
      chunks.push(part.value);
    }
  } finally {
    reader.releaseLock();
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(concatBytes(chunks, size));
  } catch {
    throw localError("INVALID_REQUEST_BODY", "Тело локального запроса должно быть UTF-8.");
  }
}

async function resolveBody(request: Request | null, init: RequestInit | undefined, signals: readonly AbortSignal[]): Promise<string | undefined> {
  if (init && init.body !== undefined) {
    if (init.body === null) return undefined;
    if (typeof init.body !== "string") throw localError("INVALID_REQUEST_BODY", "Тело локального запроса должно быть UTF-8 строкой.");
    bodySize(init.body);
    return init.body;
  }
  if (!request) return undefined;
  const body = await readRequestBody(request, signals);
  if (body !== undefined) bodySize(body);
  return body;
}

function envelopeHeaders(value: unknown): Headers {
  if (!Array.isArray(value)) throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректные заголовки.");
  const headers = new Headers();
  try {
    for (const pair of value) {
      if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string" || typeof pair[1] !== "string") throw new Error("invalid header");
      headers.append(pair[0], pair[1]);
    }
  } catch {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректные заголовки.");
  }
  return headers;
}

function decodeBase64(value: string): Uint8Array {
  if (value.length > MAX_BASE64_LENGTH || value.length % 4 !== 0 || !validBase64(value)) {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректное тело.");
  }
  if (!value) return new Uint8Array();
  let binary: string;
  try {
    binary = atob(value);
  } catch {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректное тело.");
  }
  if (binary.length > MAX_LOCAL_RESPONSE_BYTES) throw localError("LOCAL_RESPONSE_TOO_LARGE", "Ответ локального API слишком велик.");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function validBase64(value: string): boolean {
  if (!value) return true;
  const firstPadding = value.indexOf("=");
  const dataLength = firstPadding < 0 ? value.length : firstPadding;
  for (let index = 0; index < dataLength; index += 1) {
    const code = value.charCodeAt(index);
    const valid = code >= 65 && code <= 90 || code >= 97 && code <= 122 || code >= 48 && code <= 57 || code === 43 || code === 47;
    if (!valid) return false;
  }
  if (firstPadding < 0) return true;
  const padding = value.length - firstPadding;
  if (padding > 2 || value.slice(firstPadding) !== "=".repeat(padding)) return false;
  return padding === 1 ? dataLength % 4 === 3 : dataLength % 4 === 2;
}

function responseFromEnvelope(value: unknown): Response {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректный ответ.");
  const envelope = value as Partial<LocalApiResponseEnvelope>;
  const status = envelope.status;
  if (typeof status !== "number" || !Number.isInteger(status) || status < 200 || status > 599 || typeof envelope.bodyBase64 !== "string") {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректный ответ.");
  }
  const headers = envelopeHeaders(envelope.headers);
  if ((status === 204 || status === 205 || status === 304) && envelope.bodyBase64 !== "") {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул тело для пустого ответа.");
  }
  const bytes = decodeBase64(envelope.bodyBase64);
  try {
    const body = status === 204 || status === 205 || status === 304 ? null : bytes;
    return new Response(body, { status, headers });
  } catch {
    throw localError("INVALID_LOCAL_RESPONSE", "Локальный API вернул некорректный ответ.");
  }
}

export async function localFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const { path, request } = requestInput(input);
  const headers = requestHeaders(request, init);
  const signals = [init?.signal, request?.signal].filter((signal): signal is AbortSignal => Boolean(signal));
  throwIfAborted(signals);
  const method = requestMethod(request, init);
  const body = await resolveBody(request, init, signals);
  throwIfAborted(signals);
  const invoke = getInvoke();
  if (!invoke) throw localError("LOCAL_DESKTOP_REQUIRED", "Локальное приложение недоступно в этом окне.");
  const localRequest: LocalApiRequest = { method, path };
  if (body !== undefined) localRequest.body = body;
  if (headers) localRequest.headers = headers;
  let envelope: unknown;
  try {
    let invocation: Promise<unknown>;
    try {
      invocation = (async () => {
        const deadline = Date.now() + 185_000;
        for (;;) {
          throwIfAborted(signals);
          try { return await invoke(LOCAL_API_COMMAND, { request: localRequest }); }
          catch (reason) {
            if (!reason || typeof reason !== "object" || (reason as { code?: unknown }).code !== "local_backend_starting") throw reason;
            if (Date.now() >= deadline) throw reason;
            await abortable(new Promise<void>(resolve => setTimeout(resolve, 400)), signals);
          }
        }
      })();
    } catch (reason) {
      invocation = Promise.reject(reason);
    }
    if (method !== "GET") invocation = trackMutation(invocation);
    envelope = await abortable(invocation, signals);
  } catch (reason) {
    if (isAbortError(reason) || signals.some((signal) => signal.aborted)) throw abortError();
    throw localError("LOCAL_API_REQUEST_FAILED", "Не удалось открыть данные. Повторите попытку или перезапустите Контур. Ваши записи остаются на устройстве.");
  }
  throwIfAborted(signals);
  return responseFromEnvelope(envelope);
}

export default localFetch;
