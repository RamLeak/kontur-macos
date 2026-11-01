import { isClientStateKey, parseClientStorageValue, serializeClientStorageValue, validateClientStateValue, type ClientJson, type ClientStateValue } from "./local-client-state-schema";

type ClientStorage = Pick<Storage, "length" | "key" | "getItem" | "setItem" | "removeItem">;
function allowedKeys(storage: ClientStorage) {
  const keys: string[] = [];
  for (let index = 0; index < storage.length; index++) { const key = storage.key(index); if (key && isClientStateKey(key)) keys.push(key); }
  return keys;
}
/** Inspect values only after a key has matched the exact data allowlist. */
export function readClientStorage(storage: ClientStorage): ClientStateValue {
  const entries: Record<string, ClientJson> = {};
  for (const key of allowedKeys(storage)) {
    const raw = storage.getItem(key); if (raw !== null) entries[key] = parseClientStorageValue(key, raw);
  }
  return validateClientStateValue({ version: 1, entries });
}
/** Preserve excluded keys and roll back allowed values if the WebView quota rejects restoration. */
export function applyClientStorage(storage: ClientStorage, value: ClientStateValue) {
  validateClientStateValue(value);
  const before = new Map(allowedKeys(storage).map(key => [key, storage.getItem(key)!]));
  const writes = new Map(Object.entries(value.entries).map(([key, entry]) => [key, serializeClientStorageValue(key, entry)]));
  try {
    for (const key of before.keys()) if (!writes.has(key)) storage.removeItem(key);
    for (const [key, raw] of writes) if (storage.getItem(key) !== raw) storage.setItem(key, raw);
  } catch (reason) {
    try {
      for (const key of allowedKeys(storage)) if (!before.has(key)) storage.removeItem(key);
      for (const [key, raw] of before) if (storage.getItem(key) !== raw) storage.setItem(key, raw);
    } catch { throw new Error("Не удалось восстановить настройки: хранилище устройства переполнено. Данные в резервной копии сохранены."); }
    throw reason;
  }
}
export function observeClientStorage(storage: Storage, callbacks: { before(): void; after(key: string): void; failed(reason: unknown, key: string, value: string | null): void }, suppressed: () => boolean = () => false) {
  const proto = Object.getPrototypeOf(storage) as Storage, set = proto.setItem, remove = proto.removeItem;
  function mutate(target: Storage, key: string, value: string | null, work: () => void) {
    if (target !== storage || suppressed() || !isClientStateKey(key)) { work(); return; }
    try { callbacks.before(); work(); callbacks.after(key); } catch (reason) { callbacks.failed(reason, key, value); throw reason; }
  }
  const observedSet = function (this: Storage, key: string, value: string) { const name = String(key), raw = String(value); mutate(this, name, raw, () => set.call(this, name, raw)); };
  const observedRemove = function (this: Storage, key: string) { const name = String(key); mutate(this, name, null, () => remove.call(this, name)); };
  proto.setItem = observedSet; proto.removeItem = observedRemove;
  return () => { if (proto.setItem === observedSet) proto.setItem = set; if (proto.removeItem === observedRemove) proto.removeItem = remove; };
}