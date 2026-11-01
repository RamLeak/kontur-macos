export type StrokePoint = { x: number; y: number };

function finite(value: number): number {
  return Number.isFinite(value) ? value : 0;
}

function point(value: StrokePoint): StrokePoint {
  return { x: finite(value.x), y: finite(value.y) };
}

/**
 * Paint a round freehand path with quadratic midpoint interpolation.
 *
 * The caller sets the canvas transform (including devicePixelRatio) and the
 * line width.  Keeping interpolation in user space prevents the persisted
 * points from changing when a canvas is resized.
 */
export function drawSmoothRoundStroke(
  context: Pick<CanvasRenderingContext2D, "beginPath" | "moveTo" | "lineTo" | "quadraticCurveTo" | "arc" | "stroke">,
  points: readonly StrokePoint[],
  smoothing = 1,
): void {
  if (!points.length) return;
  const first = point(points[0]);
  const amount = Math.max(0, Math.min(1, finite(smoothing)));
  context.beginPath();
  if (points.length === 1) {
    context.moveTo(first.x, first.y);
    context.arc(first.x, first.y, 0.5, 0, Math.PI * 2);
    context.stroke();
    return;
  }
  context.moveTo(first.x, first.y);
  if (amount === 0) {
    for (const raw of points.slice(1)) {
      const next = point(raw);
      context.lineTo(next.x, next.y);
    }
    context.stroke();
    return;
  }
  if (points.length === 2) {
    const end = point(points[1]);
    context.lineTo(end.x, end.y);
    context.stroke();
    return;
  }
  for (let index = 1; index < points.length - 1; index += 1) {
    const control = point(points[index]);
    const next = point(points[index + 1]);
    const midpoint = { x: (control.x + next.x) / 2, y: (control.y + next.y) / 2 };
    const adjustedControl = {
      x: midpoint.x + (control.x - midpoint.x) * amount,
      y: midpoint.y + (control.y - midpoint.y) * amount,
    };
    context.quadraticCurveTo(adjustedControl.x, adjustedControl.y, midpoint.x, midpoint.y);
  }
  const last = point(points.at(-1) as StrokePoint);
  const previous = point(points[points.length - 2]);
  const finalControl = {
    x: last.x + (previous.x - last.x) * amount,
    y: last.y + (previous.y - last.y) * amount,
  };
  context.quadraticCurveTo(finalControl.x, finalControl.y, last.x, last.y);
  context.stroke();
}

/** Return a de-duplicated copy suitable for persistence after pointer input. */
export function compactStrokePoints(points: readonly StrokePoint[], minimumDistance = 0.001): StrokePoint[] {
  const result: StrokePoint[] = [];
  const threshold = Math.max(0, finite(minimumDistance));
  for (const raw of points) {
    const next = point(raw);
    const previous = result.at(-1);
    if (!previous || Math.hypot(next.x - previous.x, next.y - previous.y) >= threshold) result.push(next);
  }
  return result;
}
