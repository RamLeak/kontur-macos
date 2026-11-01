import { clientStateFingerprint, emptyClientState, validateClientStateRecord, validateClientStateValue, type ClientStateRecord, type ClientStateValue } from "./local-client-state-schema";

type Inflight = { expectedVersion: number; valueSha256: string };
export type ClientStateOutbox = { version: 1; baseVersion: number; inflight?: Inflight };
export type ClientStateSnapshot = { ready: boolean; dirty: boolean; saving: boolean; conflict: boolean; localRecoverable: boolean; error: string | null; version: number };
type StorageIO = {
  readLocal(): ClientStateValue; applyLocal(value: ClientStateValue): void;
  readOutbox(): unknown; writeOutbox(value: ClientStateOutbox | null): void;
  readRemote(): Promise<unknown>; writeRemote(expectedVersion: number, value: ClientStateValue): Promise<unknown>;
};
const equal = (a: ClientStateValue, b: ClientStateValue) => clientStateFingerprint(a) === clientStateFingerprint(b);
const copy = (value: ClientStateValue) => structuredClone(value);
const message = (reason: unknown) => reason instanceof Error ? reason.message : "Не удалось сохранить локальные настройки.";
async function sha256(value: ClientStateValue): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(clientStateFingerprint(value)));
  return [...new Uint8Array(hash)].map(v => v.toString(16).padStart(2, "0")).join("");
}
function outbox(value: unknown): ClientStateOutbox | null {
  if (value === null) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Журнал локальных настроек повреждён.");
  const raw = value as Record<string, unknown>;
  if (raw.version !== 1 || !Number.isSafeInteger(raw.baseVersion) || (raw.baseVersion as number) < 0 || Object.keys(raw).some(k => !["version", "baseVersion", "inflight"].includes(k))) throw new Error("Журнал локальных настроек повреждён.");
  if (raw.inflight !== undefined) {
    const item = raw.inflight as Inflight;
    if (!item || typeof item !== "object" || Object.keys(item).sort().join() !== "expectedVersion,valueSha256" || item.expectedVersion !== raw.baseVersion || !/^[0-9a-f]{64}$/.test(item.valueSha256)) throw new Error("Журнал локальных настроек повреждён.");
  }
  return raw as ClientStateOutbox;
}

/** One optimistic writer. Original allowed localStorage values hold the recovery data. */
export class LocalClientStateStore {
  private desired = emptyClientState(); private acknowledged = emptyClientState(); private version = 0;
  private ready = false; private conflict = false; private durable = false; private error: string | null = null;
  private sourceError: string | null = null; private journalError: string | null = null;
  private inflight: Inflight | undefined; private reconcile = false; private localPending = false;
  private starting: Promise<void> | null = null; private writing: Promise<void> | null = null;
  private listeners = new Set<() => void>();
  private state: ClientStateSnapshot = { ready: false, dirty: true, saving: false, conflict: false, localRecoverable: false, error: null, version: 0 };
  constructor(private io: StorageIO) {}
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.state;
  private changed() {
    this.state = { ready: this.ready, dirty: !this.ready || !!this.sourceError || !!this.journalError || this.conflict || this.localPending || !equal(this.desired, this.acknowledged), saving: !!this.writing,
      conflict: this.conflict, localRecoverable: this.durable && !this.sourceError, error: this.sourceError ?? this.journalError ?? this.error, version: this.version };
    this.listeners.forEach(listener => listener());
  }
  private persist(force = false) {
    try {
      this.io.writeOutbox(force || this.localPending || this.inflight || !equal(this.desired, this.acknowledged) ? { version: 1, baseVersion: this.version, ...(this.inflight ? { inflight: this.inflight } : {}) } : null);
      this.durable = true; this.journalError = null;
    } catch (reason) { this.durable = false; this.journalError = message(reason); this.changed(); throw reason; }
  }
  /** Write the revision marker before the original storage operation, without duplicating its payload. */
  prepareLocalMutation() { if (!this.ready) throw new Error("Локальные настройки ещё не загружены."); this.persist(true); }
  markLocalMutation() { this.localPending = true; this.sourceError = null; this.changed(); }
  change(value: ClientStateValue) {
    try { this.desired = copy(validateClientStateValue(value)); this.localPending = false; this.sourceError = null; this.error = null; this.persist(); this.changed(); }
    catch (reason) { this.failSource(reason); throw reason; }
  }
  failSource(reason: unknown) { this.sourceError = message(reason); this.changed(); }
  private async reconcileRemote(remote: ClientStateRecord, baseVersion: number, inflight?: Inflight) {
    const recovered = equal(remote.value, this.desired) || remote.version === baseVersion || !!(inflight && remote.version === inflight.expectedVersion + 1 && await sha256(remote.value) === inflight.valueSha256);
    if (!recovered) {
      this.conflict = true; this.error = "Конфликт локальных настроек. Выберите данные устройства или резервной копии."; this.changed(); throw new Error(this.error);
    }
    this.version = remote.version; this.acknowledged = copy(remote.value); this.inflight = undefined; this.conflict = false; this.reconcile = false; this.error = null;
  }
  initialize(): Promise<void> {
    if (this.ready) return this.conflict ? Promise.reject(new Error(this.error!)) : Promise.resolve();
    if (this.starting) return this.starting;
    this.starting = this.initializeOnce().finally(() => { this.starting = null; }); return this.starting;
  }
  private async initializeOnce() {
    try {
      const local = copy(validateClientStateValue(this.io.readLocal())); const pending = outbox(this.io.readOutbox());
      this.desired = local; this.version = pending?.baseVersion ?? 0; this.inflight = pending?.inflight;
      const remote = validateClientStateRecord(await this.io.readRemote());
      if (pending) await this.reconcileRemote(remote, pending.baseVersion, pending.inflight);
      else {
        this.version = remote.version; this.acknowledged = copy(remote.value);
        if (remote.version !== 0) this.desired = copy(remote.value);
      }
      this.io.applyLocal(copy(this.desired)); this.ready = true; this.sourceError = null; this.error = null; this.persist(); this.changed();
    } catch (reason) { this.error = message(reason); this.changed(); throw reason; }
  }
  flush(): Promise<void> {
    if (this.writing) return this.writing;
    const operation = Promise.resolve().then(() => this.drain());
    this.writing = operation.finally(() => { this.writing = null; this.changed(); }); this.changed(); return this.writing;
  }
  private async drain() {
    if (!this.ready || this.conflict) throw new Error(this.error ?? "Локальные настройки не готовы к сохранению.");
    this.capture();
    if (this.reconcile) await this.reconcileRemote(validateClientStateRecord(await this.io.readRemote()), this.version, this.inflight);
    while (this.localPending || !equal(this.desired, this.acknowledged)) {
      if (this.localPending) this.capture();
      if (equal(this.desired, this.acknowledged)) break;
      if (this.version >= Number.MAX_SAFE_INTEGER) throw new Error("Исчерпана версия локальных настроек.");
      const value = copy(this.desired), expectedVersion = this.version;
      this.inflight = { expectedVersion, valueSha256: await sha256(value) }; this.persist(true); this.changed();
      let remote: ClientStateRecord;
      try {
        remote = validateClientStateRecord(await this.io.writeRemote(expectedVersion, value));
        if (remote.version !== expectedVersion + 1 || !equal(remote.value, value)) throw new Error("Неверное подтверждение записи локальных настроек.");
      } catch (reason) { this.reconcile = true; this.error = message(reason); this.changed(); throw reason; }
      this.version = remote.version; this.acknowledged = copy(remote.value); this.inflight = undefined; this.error = null; this.persist(); this.changed();
    }
    this.persist(); this.changed();
  }
  private capture() { try { this.change(this.io.readLocal()); } catch (reason) { this.failSource(reason); throw reason; } }
  async reloadAfterImport() {
    await this.flush();
    const remote = validateClientStateRecord(await this.io.readRemote());
    this.io.applyLocal(copy(remote.value)); this.desired = copy(remote.value); this.acknowledged = copy(remote.value); this.version = remote.version;
    this.inflight = undefined; this.conflict = false; this.reconcile = false; this.error = null; this.persist(); this.changed();
  }
  async resolveConflict(choice: "local" | "remote") {
    const remote = validateClientStateRecord(await this.io.readRemote());
    this.desired = choice === "local" ? copy(validateClientStateValue(this.io.readLocal())) : copy(remote.value);
    this.io.applyLocal(copy(this.desired)); this.acknowledged = copy(remote.value); this.version = remote.version;
    this.inflight = undefined; this.conflict = false; this.reconcile = false; this.ready = true; this.sourceError = null; this.error = null; this.persist(); this.changed(); await this.flush();
  }
}
