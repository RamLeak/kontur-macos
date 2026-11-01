export type InkPoint = { x: number; y: number };

export type CanvasSize = {
  width: number;
  height: number;
};

/** The CSS-pixel frame in which a saved normalized stroke was created. */
export type InkFrame = CanvasSize;

export type InkLayoutAnchor = { frame: InkFrame; origin: InkPoint; charOffset: number | null };

/** The replay origin is the text anchor, never the pointer's stroke start. */
export function createInkLayoutAnchor(frame: InkFrame, pointer: InkPoint, character: InkPoint | null, offset: number | null): InkLayoutAnchor {
  const anchored = character !== null && offset !== null;
  return {
    frame: { ...frame },
    origin: anchored ? { ...character } : { x: pointer.x * frame.width, y: pointer.y * frame.height },
    charOffset: anchored ? offset : null,
  };
}

/** Keep the pointer-to-text offset through save/replay and subsequent reflow. */
export function projectInkPoints(points: readonly InkPoint[], anchor: Pick<InkLayoutAnchor, "frame" | "origin">, character: InkPoint, canvas: CanvasSize): InkPoint[] {
  return points.map((point) => ({
    x: (point.x * anchor.frame.width - anchor.origin.x + character.x) / Math.max(1, canvas.width),
    y: (point.y * anchor.frame.height - anchor.origin.y + character.y) / Math.max(1, canvas.height),
  }));
}

/** Cover a fast pointer drag in CSS pixels, including the gaps between events. */
export function inkSweepPoints(from: InkPoint, to: InkPoint, canvas: CanvasSize, spacing: number): InkPoint[] {
  const length = Math.hypot((to.x - from.x) * canvas.width, (to.y - from.y) * canvas.height);
  const steps = Math.min(512, Math.max(1, Math.ceil(length / Math.max(1, spacing))));
  return Array.from({ length: steps + 1 }, (_, index) => ({
    x: from.x + (to.x - from.x) * index / steps,
    y: from.y + (to.y - from.y) * index / steps,
  }));
}

export const INK_STROKE_WIDTH = {
  marker: 14,
  pen: 3,
} as const;

const DEFAULT_ERASER_TOLERANCE = 6;

function finiteCoordinate(value: number): number {
  return Number.isFinite(value) ? value : 0;
}

/** Return the shortest distance from point p to the closed segment a-b. */
export function distanceToSegment(p: InkPoint, a: InkPoint, b: InkPoint): number {
  const px = finiteCoordinate(p.x);
  const py = finiteCoordinate(p.y);
  const ax = finiteCoordinate(a.x);
  const ay = finiteCoordinate(a.y);
  const bx = finiteCoordinate(b.x);
  const by = finiteCoordinate(b.y);
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSquared = dx * dx + dy * dy;

  if (lengthSquared === 0) {
    return Math.hypot(px - ax, py - ay);
  }

  const projection = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared));
  return Math.hypot(px - (ax + projection * dx), py - (ay + projection * dy));
}

/**
 * Hit-test a normalized ink stroke in CSS pixels.
 *
 * Converting both the stroke and pointer to the current canvas dimensions is
 * important when the statement reflows or the viewport changes. The width is
 * the painted stroke width; tolerance gives the eraser a small forgiving edge.
 */
export function hitTestInkStroke(
  points: readonly InkPoint[],
  target: InkPoint,
  canvas: CanvasSize,
  strokeWidth: number,
  tolerance = DEFAULT_ERASER_TOLERANCE,
): boolean {
  const width = Math.max(0, finiteCoordinate(canvas.width));
  const height = Math.max(0, finiteCoordinate(canvas.height));
  if (width === 0 || height === 0 || points.length === 0) return false;

  const targetPixels = {
    x: finiteCoordinate(target.x) * width,
    y: finiteCoordinate(target.y) * height,
  };
  const pixelPoints = points.map((point) => ({
    x: finiteCoordinate(point.x) * width,
    y: finiteCoordinate(point.y) * height,
  }));
  const radius = Math.max(0, finiteCoordinate(strokeWidth)) / 2 + Math.max(0, finiteCoordinate(tolerance));

  if (pixelPoints.length === 1) {
    return distanceToSegment(targetPixels, pixelPoints[0], pixelPoints[0]) <= radius;
  }

  for (let index = 1; index < pixelPoints.length; index += 1) {
    if (distanceToSegment(targetPixels, pixelPoints[index - 1], pixelPoints[index]) <= radius) return true;
  }
  return false;
}

/**
 * Hit-test a saved stroke whose normalized points refer to an older layout
 * frame.  The pointer is first mapped into that frame, so a responsive
 * reflow does not change which saved mark the eraser can reach.
 */
export function hitTestInkStrokeInFrame(
  points: readonly InkPoint[],
  target: InkPoint,
  currentCanvas: CanvasSize,
  frame: InkFrame,
  strokeWidth: number,
  tolerance = DEFAULT_ERASER_TOLERANCE,
): boolean {
  const currentWidth = Math.max(0, finiteCoordinate(currentCanvas.width));
  const currentHeight = Math.max(0, finiteCoordinate(currentCanvas.height));
  const frameWidth = Math.max(0, finiteCoordinate(frame.width));
  const frameHeight = Math.max(0, finiteCoordinate(frame.height));
  if (currentWidth === 0 || currentHeight === 0 || frameWidth === 0 || frameHeight === 0) return false;
  const mappedTarget = {
    x: finiteCoordinate(target.x) * currentWidth / frameWidth,
    y: finiteCoordinate(target.y) * currentHeight / frameHeight,
  };
  return hitTestInkStroke(points, mappedTarget, { width: frameWidth, height: frameHeight }, strokeWidth, tolerance);
}
