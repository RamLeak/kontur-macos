import assert from "node:assert/strict";
import test from "node:test";
import { createInkLayoutAnchor, distanceToSegment, hitTestInkStroke, hitTestInkStrokeInFrame, inkSweepPoints, INK_STROKE_WIDTH, projectInkPoints } from "../lib/statement-ink.ts";

test("a fast eraser drag crosses a mark even when both pointer events miss it", () => {
  const canvas = { width: 400, height: 400 };
  const mark = [{ x: 0, y: .5 }, { x: 1, y: .5 }];
  const start = { x: .5, y: 0 }, end = { x: .5, y: 1 };
  assert.equal(hitTestInkStroke(mark, start, canvas, 3, 22), false);
  assert.equal(hitTestInkStroke(mark, end, canvas, 3, 22), false);
  assert.ok(inkSweepPoints(start, end, canvas, 11).some(point => hitTestInkStroke(mark, point, canvas, 3, 22)));
  assert.ok(inkSweepPoints(start, end, { width: 1e6, height: 1e6 }, 1).length <= 513);
});

test("saving a mark above or below a text line never snaps its start to that line", () => {
  const frame = { width: 400, height: 600 };
  const text = { x: 45, y: 210 };
  for (const y of [190, 230]) {
    const pointer = { x: 0.2, y: y / frame.height };
    const anchor = createInkLayoutAnchor(frame, pointer, text, 12);
    const [saved] = projectInkPoints([pointer], anchor, text, frame);
    assert.equal(saved.x * frame.width, 80);
    assert.ok(Math.abs(saved.y * frame.height - y) < 1e-9);
  }
});

test("reflow follows the text while preserving the offset of the drawn mark", () => {
  const anchor = createInkLayoutAnchor({ width: 400, height: 600 }, { x: .2, y: .4 }, { x: 45, y: 210 }, 12);
  const [point] = projectInkPoints([{ x: .2, y: .4 }], anchor, { x: 25, y: 310 }, { width: 200, height: 900 });
  assert.equal(point.x * 200, 60);
  assert.equal(point.y * 900, 340);
});

test("off-canvas replay remains outside instead of drawing a false line along the block edge", () => {
  const anchor = { frame: { width: 100, height: 100 }, origin: { x: 0, y: 0 } };
  assert.deepEqual(projectInkPoints([{ x: .2, y: .9 }], anchor, { x: 0, y: 30 }, { width: 100, height: 100 }), [{ x: .2, y: 1.2 }]);
  assert.equal(createInkLayoutAnchor(anchor.frame, { x: .2, y: .9 }, null, 4).charOffset, null);
});

test("distanceToSegment measures the closest point on the full segment", () => {
  assert.equal(distanceToSegment({ x: 5, y: 3 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 3);
  assert.equal(distanceToSegment({ x: -2, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 2);
});

test("eraser hit test catches a stroke midpoint after canvas scaling", () => {
  const stroke = [{ x: 0.1, y: 0.5 }, { x: 0.9, y: 0.5 }];
  const target = { x: 0.5, y: 0.56 };
  const canvas = { width: 200, height: 100 };

  assert.equal(hitTestInkStroke(stroke, target, canvas, INK_STROKE_WIDTH.marker, 0), true);
  assert.equal(hitTestInkStroke(stroke, target, canvas, INK_STROKE_WIDTH.pen, 0), false);
});

test("eraser hit test handles a single point without relying on endpoints", () => {
  assert.equal(hitTestInkStroke([{ x: 0.5, y: 0.5 }], { x: 0.53, y: 0.5 }, { width: 100, height: 100 }, INK_STROKE_WIDTH.marker, 0), true);
  assert.equal(hitTestInkStroke([{ x: 0.5, y: 0.5 }], { x: 0.53, y: 0.5 }, { width: 100, height: 100 }, INK_STROKE_WIDTH.pen, 0), false);
});

test("frame-aware hit testing keeps saved marks stable after a responsive resize", () => {
  const stroke = [{ x: 0.1, y: 0.5 }, { x: 0.9, y: 0.5 }];
  assert.equal(
    hitTestInkStrokeInFrame(stroke, { x: 1, y: 0.25 }, { width: 100, height: 200 }, { width: 200, height: 100 }, INK_STROKE_WIDTH.marker, 0),
    true,
  );
});
