"use strict";

// Response curves: the same math as phonecontrollers/curves.py (keep the two in step). A curve maps
// 0..1 to 0..1 through points that start at x = 0 and end at x = 1, joined by straight lines.
(function (global) {
  const MIN_GAP = 0.01;
  const LINEAR = [[0, 0], [1, 1]];

  function clamp01(value) {
    return Math.min(Math.max(value, 0), 1);
  }

  function normalize(points) {
    const cleaned = (Array.isArray(points) ? points : [])
      .map((point) => [Number(point?.[0]), Number(point?.[1])])
      .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y))
      .map(([x, y]) => [clamp01(x), clamp01(y)])
      .sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const point of cleaned) {
      if (merged.length && point[0] - merged[merged.length - 1][0] < MIN_GAP) continue;
      merged.push(point);
    }
    if (merged.length < 2) return LINEAR.map((point) => [...point]);
    merged[0][0] = 0;
    merged[merged.length - 1][0] = 1;
    if (merged.length > 2 && merged[merged.length - 1][0] - merged[merged.length - 2][0] < MIN_GAP) {
      merged.splice(merged.length - 2, 1);
    }
    return merged;
  }

  // Returns a function of x; .signed(x) shapes each side of center the same way (steering).
  function build(points) {
    const normalized = normalize(points);
    const xs = normalized.map((point) => point[0]);
    const ys = normalized.map((point) => point[1]);
    const curve = (x) => {
      if (!(x > 0)) return ys[0];
      if (x >= 1) return ys[ys.length - 1];
      let i = 0;
      while (xs[i + 1] < x) i++;
      const t = (x - xs[i]) / (xs[i + 1] - xs[i]);
      return ys[i] + (ys[i + 1] - ys[i]) * t;
    };
    curve.signed = (x) => Math.sign(x) * curve(Math.abs(x));
    return curve;
  }

  global.PhoneControllersCurves = { build, normalize };
})(globalThis);
