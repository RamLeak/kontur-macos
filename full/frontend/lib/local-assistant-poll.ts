export interface RunStatus { status?: string | null }
interface PollClock {
  schedule: (callback: () => void, milliseconds: number) => ReturnType<typeof setTimeout>;
  cancel: (timer: ReturnType<typeof setTimeout>) => void;
  now: () => number;
}
const terminal = new Set(["succeeded", "failed", "timed_out", "cancelled", "unavailable"]);

/** Same authoritative run snapshots as Send's polling; no EventSource or URL. */
export function subscribeLocalRunStatus(
  read: (signal: AbortSignal) => Promise<RunStatus>,
  handlers: { snapshot: (run: RunStatus) => void; terminal: (status: string) => void; error: (reason: unknown) => void },
  clock: PollClock = { schedule: setTimeout, cancel: clearTimeout, now: Date.now },
): () => void {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const controller = new AbortController();
  const deadline = clock.now() + 125_000;
  const poll = async () => {
    if (!active) return;
    if (clock.now() >= deadline) { active = false; handlers.error(new Error("Обновление статуса остановлено по тайм-ауту.")); return; }
    try {
      const run = await read(controller.signal);
      if (!active) return;
      handlers.snapshot(run);
      if (typeof run.status === "string" && terminal.has(run.status)) {
        active = false; handlers.terminal(run.status); return;
      }
    } catch (reason) { if (active) handlers.error(reason); }
    if (active) timer = clock.schedule(() => { void poll(); }, 1000);
  };
  void poll();
  return () => { active = false; controller.abort(); if (timer !== null) clock.cancel(timer); };
}
