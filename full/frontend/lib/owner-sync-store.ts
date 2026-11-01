/** Pure, dependency-injected state machine shared by independent client stores. */
export type SyncContext = {
  kind: "practice_answer" | "composer" | "preferences" | "catalog_filters";
  solve_session_id?: string; conversation_id?: string;
  task_id?: string; task_revision_id?: string; content_hash?: string;
};
export type SyncValue = Record<string, unknown>;
export type SyncRecord = {context: SyncContext; version: number; value: SyncValue | null; updated_at: string | null};
export type SyncState = {
  value: SyncValue; version: number; remote: SyncRecord | null;
  dirty: boolean; saving: boolean; conflict: boolean; offline: boolean;
  unsupported: boolean; ready: boolean; error: string | null;
};
type Pending = {context: SyncContext; base_version: number; idempotency_key: string; value: SyncValue};
type Transport = <T>(path: string, init?: RequestInit) => Promise<T>;
type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const keyFor = (context: SyncContext) => JSON.stringify(Object.entries(context).sort(([a], [b]) => a.localeCompare(b)));

export class OwnerSyncStore {
  private state: SyncState;
  private listeners = new Set<() => void>();
  private pending: Pending | null = null;
  private saving: Promise<void> | null = null;
  private active = true;
  private epoch = 0;
  private key: string;
  constructor(readonly owner: string, readonly context: SyncContext, initial: SyncValue,
    private transport: Transport, private storage: StorageLike | null,
    private randomKey: () => string = () => crypto.randomUUID()) {
    this.key = `kontur:owner-sync:v1:${encodeURIComponent(owner)}:${keyFor(context)}`;
    this.state = {value: initial, version: 0, remote: null, dirty: false, saving: false,
      conflict: false, offline: false, unsupported: false, ready: false, error: null};
    try {
      const raw = storage?.getItem(this.key);
      if (raw && raw.length < 100_000) {
        const saved = JSON.parse(raw);
        if (saved.owner === owner && same(saved.context, context) && Number.isSafeInteger(saved.version)
          && saved.version >= 0 && saved.value && typeof saved.value === "object") {
          this.state = {...this.state, value: saved.value, version: saved.version, dirty: saved.dirty === true,
            remote: saved.remote ?? null, conflict: saved.conflict === true};
          const pending=saved.pending;
          this.pending=pending&&same(pending.context,context)&&Number.isSafeInteger(pending.base_version)&&pending.base_version>=0
            &&typeof pending.idempotency_key==="string"&&/^[A-Za-z0-9_.:-]{1,128}$/.test(pending.idempotency_key)?pending:null;
        }
      }
    } catch { /* Recoverable storage is best effort; never use another owner. */ }
  }
  snapshot = (): SyncState => this.state;
  subscribe = (listener: () => void) => {this.listeners.add(listener); return () => this.listeners.delete(listener);};
  private update(patch: Partial<SyncState>) {
    if (!this.active) return;
    this.state = {...this.state, ...patch};
    this.persist();
    this.listeners.forEach(listener => listener());
  }
  private persist() {
    try {
      this.storage?.setItem(this.key, JSON.stringify({owner: this.owner, context: this.context,
        version: this.state.version, value: this.state.value, dirty: this.state.dirty,
        remote: this.state.remote, conflict: this.state.conflict, pending: this.pending}));
    } catch {
      if (this.state.dirty || this.pending) this.state = {...this.state,
        error: "Локальное восстановление недоступно. Сохраните копию текста до закрытия окна."};
    }
  }
  edit(value: SyncValue) {
    this.epoch++;
    this.update({value, dirty: true, error: null});
  }
  private failed(reason: unknown) {
    const error = reason as {status?: number; code?: string; message?: string; details?: {current?: SyncRecord}};
    if (error.status === 404 && error.code !== "NOT_FOUND") this.update({unsupported: true, ready: true,
      error: "Синхронизация недоступна на этом сервере. Доступно только локальное восстановление."});
    else if (error.code === "VERSION_CONFLICT" && error.details?.current) {
      this.pending = null;
      this.update({remote: error.details.current, conflict: true, offline: false,
        error: "Черновик изменён на другом устройстве. Выберите версию."});
    } else this.update({offline: error.status === 0, error: error.message ?? "Не удалось синхронизировать черновик."});
  }
  async refresh(): Promise<void> {
    if (!this.active || this.state.unsupported || this.saving) return;
    if (this.pending) {
      await this.flush();
      if (this.pending || !this.active || this.state.conflict) return;
    }
    const epoch = this.epoch;
    try {
      const params = new URLSearchParams(Object.entries(this.context));
      const remote = await this.transport<SyncRecord>(`/sync/records?${params}`);
      if (!this.active) return;
      const changedWhileReading = epoch !== this.epoch;
      if (this.state.dirty || changedWhileReading) {
        if (same(remote.value, this.state.value)) {
          this.update({version: remote.version, dirty: false, remote: null, conflict: false, ready: true, offline: false, error: null});
        } else if (remote.version !== this.state.version) {
          this.update({remote, conflict: true, ready: true, offline: false,
            error: "Черновик изменён на другом устройстве. Выберите версию."});
        } else this.update({ready: true, offline: false});
      } else this.update({value: remote.value ?? this.state.value, version: remote.version,
        remote: null, conflict: false, ready: true, offline: false, error: null});
    } catch (reason) {this.failed(reason);}
  }
  flush(): Promise<void> {
    if (this.saving) return this.saving;
    if (!this.active || this.state.unsupported || (!this.state.dirty && !this.pending)) return Promise.resolve();
    if (this.state.conflict) return Promise.reject(new Error("Сначала выберите версию черновика."));
    if (!this.state.ready && !this.pending) return Promise.reject(new Error("Черновик ещё загружается."));
    const save = async () => {
      this.pending ??= {context: this.context, base_version: this.state.version,
        idempotency_key: this.randomKey(), value: this.state.value};
      const pending = this.pending;
      this.update({saving: true});
      try {
        const remote = await this.transport<SyncRecord>("/sync/records", {method: "PUT",
          body: JSON.stringify({...pending.context, base_version: pending.base_version,
            idempotency_key: pending.idempotency_key, value: pending.value})});
        if (!this.active) return;
        this.pending = null;
        this.update({version: remote.version, dirty: !same(this.state.value, pending.value),
          offline: false, error: null, ready: true});
      } catch (reason) {this.failed(reason); throw reason;}
      finally {this.update({saving: false});}
    };
    this.saving = save().finally(() => {this.saving = null;});
    return this.saving;
  }
  async flushAll(): Promise<void> {
    await this.flush();
    if (this.state.dirty && !this.state.unsupported && !this.state.conflict) await this.flush();
  }
  resolve(choice: "local" | "remote") {
    const remote = this.state.remote;
    if (!remote) return;
    try {this.storage?.setItem(this.key + ":conflict", JSON.stringify({local: this.state.value, remote}));} catch {}
    this.pending = null;
    this.epoch++;
    this.update({version: remote.version, value: choice === "remote" ? remote.value ?? {} : this.state.value,
      remote: null, dirty: choice === "local", conflict: false, error: null});
  }
  localRecoverable(): boolean {
    if(!this.state.dirty&&!this.pending)return true;
    try {const saved=JSON.parse(this.storage?.getItem(this.key)??"null");
      return saved?.owner===this.owner&&same(saved.context,this.context)&&same(saved.value,this.state.value);
    } catch {return false;}
  }
  close() {this.persist(); this.active = false; this.listeners.clear();}
}
