"use client";

export type LocalDraftKind = "code" | "board" | "answer" | "ink" | "mark";

export interface LocalDraft<T> {
  readonly scope: string;
  readonly kind: LocalDraftKind;
  readonly baseVersion: number;
  readonly content: T;
}

export type LocalDraftAssessment<T> = {
  status: "none" | "confirmed" | "restore" | "conflict";
  content: T | null;
};

const STORAGE_PREFIX = "kontur:pending-draft:v1";
let localDraftOwner: string | null | undefined;
const memoryDrafts=new Map<string,string>();
const MAX_MEMORY_BYTES=8*1024*1024;
function retainMemory(key:string,raw:string){
 const total=[...memoryDrafts].reduce((size,[entry,value])=>size+(entry===key?0:serializedByteLength(value)),0);
 if((memoryDrafts.has(key)||memoryDrafts.size<32)&&total+serializedByteLength(raw)<=MAX_MEMORY_BYTES)memoryDrafts.set(key,raw);
}
/** In-memory recovery is not durable storage and must never authorize native close. */
export function isLocalDraftDurable(scope:string,kind:LocalDraftKind){
 const key=storageKey(scope,kind);if(!key)return false;
 try{const raw=getStorage()?.getItem(key);return typeof raw==="string"&&(!memoryDrafts.has(key)||raw===memoryDrafts.get(key));}catch{return false;}
}
export function setLocalDraftOwner(owner: string | null) {localDraftOwner=owner;}
export function archiveDraftConflict(scope:string,kind:LocalDraftKind,local:unknown,remote:unknown){
  const key=storageKey(scope,kind);if(!key)return;
  try{const content=JSON.stringify({local,remote});if(content.length<=MAX_SERIALIZED_BYTES)getStorage()?.setItem(key+":conflict",content);}catch{}
}

const MAX_SERIALIZED_BYTES = 2 * 1024 * 1024;

export function saveLocalDraft(scope: string, kind: LocalDraftKind, baseVersion: number, content: unknown): void {
  const key = storageKey(scope, kind);
  if (!key || !isValidVersion(baseVersion) || content === undefined) return;

  let serialized: string;
  try {
    serialized = JSON.stringify({ scope, kind, baseVersion, content });
  } catch {
    return;
  }
  if (typeof serialized !== "string" || serializedByteLength(serialized) > MAX_SERIALIZED_BYTES) return;

  retainMemory(key,serialized);
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.setItem(key, serialized);
    memoryDrafts.delete(key);
  } catch {
    // Local recovery is best effort; the server remains authoritative.
  }
}

export function readLocalDraft<T>(scope: string, kind: LocalDraftKind): LocalDraft<T> | null {
  const key = storageKey(scope, kind);
  if (!key) return null;
  const storage = getStorage();
  let raw: string | null=memoryDrafts.get(key)??null;
  if(raw===null){try{raw=storage?.getItem(key)??null;}catch{return null;}}
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed || serializedByteLength(trimmed) > MAX_SERIALIZED_BYTES) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (!isRecord(parsed) || parsed.scope !== scope || parsed.kind !== kind || !Object.prototype.hasOwnProperty.call(parsed, "content") || !isValidVersion(parsed.baseVersion)) {
    return null;
  }
  return {
    scope,
    kind,
    baseVersion: parsed.baseVersion,
    content: parsed.content as T,
  };
}

export function clearLocalDraft(scope: string, kind: LocalDraftKind): void {
  const key = storageKey(scope, kind);
  if (!key) return;
  memoryDrafts.delete(key);
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.removeItem(key);
  } catch {
    // Clearing is best effort; callers must not treat it as a server save.
  }
}

export function assessLocalDraft<T>(draft: LocalDraft<T> | null | undefined, serverVersion: number, serverContent: T): LocalDraftAssessment<T> {
  if (!draft) return { status: "none", content: null };
  if (!isValidVersion(draft.baseVersion) || !isValidVersion(serverVersion)) return { status: "conflict", content: draft.content };

  if (sameContent(draft.content, serverContent)) return { status: "confirmed", content: null };
  if (draft.baseVersion === serverVersion) return { status: "restore", content: draft.content };
  return { status: "conflict", content: draft.content };
}

function storageKey(scope: string, kind: LocalDraftKind): string | null {
  if (typeof scope !== "string" || !isDraftKind(kind)) return null;
  if(localDraftOwner===null)return null;
  const ownerPart=localDraftOwner===undefined?"":`owner:${encodeURIComponent(localDraftOwner)}:`;
  return `${STORAGE_PREFIX}:${ownerPart}${scope}:${kind}`;
}

function isDraftKind(value: unknown): value is LocalDraftKind {
  return value === "code" || value === "board" || value === "answer" || value === "ink" || value === "mark";
}

function isValidVersion(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function getStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> | null {
  if (typeof window === "undefined") return null;
  try {
    const storage = window.localStorage;
    if (!storage || typeof storage.getItem !== "function" || typeof storage.setItem !== "function" || typeof storage.removeItem !== "function") return null;
    return storage;
  } catch {
    return null;
  }
}

function serializedByteLength(value: string): number {
  try {
    return typeof TextEncoder === "function" ? new TextEncoder().encode(value).byteLength : value.length;
  } catch {
    return value.length;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function sameContent(left: unknown, right: unknown): boolean {
  try {
    return stableSerialize(left) === stableSerialize(right);
  } catch {
    return false;
  }
}

function stableSerialize(value: unknown, seen = new WeakSet<object>()): string {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (typeof value === "string") return `string:${JSON.stringify(value)}`;
  if (typeof value === "boolean") return `boolean:${value ? "1" : "0"}`;
  if (typeof value === "number") {
    if (Number.isNaN(value)) return "number:NaN";
    if (Object.is(value, -0)) return "number:-0";
    if (value === Infinity) return "number:Infinity";
    if (value === -Infinity) return "number:-Infinity";
    return `number:${value}`;
  }
  if (typeof value === "bigint") return `bigint:${value.toString()}`;
  if (typeof value === "symbol" || typeof value === "function") return `${typeof value}:${String(value)}`;
  if (value instanceof Date) return `date:${value.toISOString()}`;
  if (seen.has(value)) throw new Error("Circular content");
  seen.add(value);
  let serialized: string;
  if (Array.isArray(value)) {
    serialized = `[${value.map((item) => stableSerialize(item, seen)).join(",")}]`;
  } else {
    const record = value as Record<string, unknown>;
    serialized = `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableSerialize(record[key], seen)}`).join(",")}}`;
  }
  seen.delete(value);
  return serialized;
}
