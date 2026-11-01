type SurfaceFlush = () => Promise<void>;
type SurfaceCapture = () => void;

type SurfaceRegistration = {
  flush: SurfaceFlush;
  capture?: SurfaceCapture;
};

const surfaces = new Set<SurfaceRegistration>();

export function registerSurfaceFlush(flush: SurfaceFlush, capture?: SurfaceCapture): () => void {
  const registration: SurfaceRegistration = { flush, capture };
  surfaces.add(registration);
  return () => { surfaces.delete(registration); };
}

/** Commit transient in-memory edits without claiming that storage acknowledged them. */
export function captureSurfaces(): void {
  for (const surface of surfaces) surface.capture?.();
}

export async function flushSurfaces(): Promise<void> {
  const results = await Promise.allSettled([...surfaces].map(({ flush }) => flush()));
  const failure = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
  if (failure) throw failure.reason;
}
