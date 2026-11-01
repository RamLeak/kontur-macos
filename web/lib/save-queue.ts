export interface Revisioned {
  id: string;
  revision: number;
  updatedAt: number;
}

export interface SaveState<T> {
  value: T;
  dirty: boolean;
  saving: boolean;
  error: unknown;
}

/** A single writer: an acknowledgement never replaces edits made in flight. */
export class SaveQueue<T extends Revisioned> {
  private value: T;
  private revision: number;
  private generation = 0;
  private durableGeneration = 0;
  private pending: Promise<T> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private error: unknown = null;
  private saving = false;
  private disposed = false;

  constructor(
    initial: T,
    private readonly save: (value: T, expectedRevision: number) => Promise<T>,
    private readonly changed: (state: SaveState<T>) => void,
    private readonly delay = 250,
  ) {
    this.value = initial;
    this.revision = initial.revision;
  }

  get state(): SaveState<T> {
    return {
      value: this.value,
      dirty: this.generation !== this.durableGeneration,
      saving: this.saving,
      error: this.error,
    };
  }

  edit(patch: Partial<Omit<T, "id" | "revision" | "updatedAt">>): void {
    if (this.disposed) return;
    this.value = { ...this.value, ...patch };
    this.generation += 1;
    this.changed(this.state);
    if (this.timer !== null) clearTimeout(this.timer);
    if (!this.error) {
      this.timer = setTimeout(() => { void this.flush().catch(() => undefined); }, this.delay);
    }
  }

  flush(): Promise<T> {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (this.pending) return this.pending;
    if (!this.state.dirty) return Promise.resolve(this.value);
    this.pending = this.drain().finally(() => { this.pending = null; });
    return this.pending;
  }

  private async drain(): Promise<T> {
    this.error = null;
    this.saving = true;
    this.changed(this.state);
    try {
      while (this.generation !== this.durableGeneration) {
        const sentGeneration = this.generation;
        const sent = this.value;
        const saved = await this.save(sent, this.revision);
        if (saved.id !== sent.id || !Number.isSafeInteger(saved.revision) || saved.revision <= this.revision) {
          throw new Error("INVALID_STORAGE_ACK");
        }
        this.revision = saved.revision;
        this.durableGeneration = sentGeneration;
        this.value = this.generation === sentGeneration
          ? saved
          : { ...this.value, revision: saved.revision, updatedAt: saved.updatedAt };
        this.changed(this.state);
      }
      return this.value;
    } catch (error) {
      this.error = error;
      throw error;
    } finally {
      this.saving = false;
      this.changed(this.state);
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
