"""Response curves: a line through editable points on the unit square.

A curve maps 0..1 to 0..1. Its points always start at x = 0 and end at x = 1 (their heights
are free, which gives a minimum output or an early full output), joined by straight lines, so
a flat stretch stays exactly flat and what you see in the editor is what you get.

static/curves.js implements the same math for the phone; keep the two in step.
"""

import math

# Points closer together than this on x are merged when a curve is cleaned up.
MIN_GAP = 0.01


def _sample(function, count: int) -> list[list[float]]:
    return [[i / (count - 1), round(function(i / (count - 1)), 4)] for i in range(count)]


PRESETS = {
    "linear": [[0.0, 0.0], [1.0, 1.0]],
    "gentle start": _sample(lambda x: x**2, 9),
    "quick start": _sample(lambda x: 1 - (1 - x) ** 2, 9),
    "s-curve": _sample(lambda x: x * x * (3 - 2 * x), 9),
    # Engagement grows with (released travel)^4: the bite sits mid-pedal instead of at the
    # first few percent, where many games (Rigs of Rods especially) put it.
    "clutch bite": _sample(lambda x: 1 - (1 - x) ** 4, 13),
    "dead zone 10%": [[0.0, 0.0], [0.1, 0.0], [1.0, 1.0]],
    "full at 70%": [[0.0, 0.0], [0.7, 1.0], [1.0, 1.0]],
}


def normalize(points) -> list[list[float]]:
    """Clean up stored or edited points: clamp, sort, merge near duplicates, pin the ends."""
    cleaned = []
    for point in points:
        x, y = float(point[0]), float(point[1])
        if math.isfinite(x) and math.isfinite(y):
            cleaned.append([min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)])
    cleaned.sort(key=lambda point: point[0])
    merged: list[list[float]] = []
    for point in cleaned:
        if merged and point[0] - merged[-1][0] < MIN_GAP:
            continue
        merged.append(point)
    if len(merged) < 2:
        return [list(point) for point in PRESETS["linear"]]
    merged[0][0] = 0.0
    merged[-1][0] = 1.0
    if len(merged) > 2 and merged[-1][0] - merged[-2][0] < MIN_GAP:
        del merged[-2]
    return merged


class Curve:
    def __init__(self, points):
        self.points = normalize(points)
        self.xs = [point[0] for point in self.points]
        self.ys = [point[1] for point in self.points]

    def __call__(self, x: float) -> float:
        xs, ys = self.xs, self.ys
        if x <= 0:
            return ys[0]
        if x >= 1:
            return ys[-1]
        i = 0
        while xs[i + 1] < x:
            i += 1
        t = (x - xs[i]) / (xs[i + 1] - xs[i])
        return ys[i] + (ys[i + 1] - ys[i]) * t

    def signed(self, x: float) -> float:
        """For steering: the curve shapes each side of center the same way. Center stays
        center even when the curve starts above 0 (that lifts the smallest turn instead)."""
        return 0.0 if x == 0 else math.copysign(self(abs(x)), x)
