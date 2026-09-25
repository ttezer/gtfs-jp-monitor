"""Route geometry of a line direction and how it changed (docs/semantic/03-report.md §3).

Geometry is the feed's shape when trips have one, else the line through the stops. It is
simplified before storage. The change measure compares both lines point by point: each line
is sampled every `sample_m` metres and every sample's distance to the other line is measured;
parts farther than `diverge_m` are where the route runs elsewhere.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .reader import Table

EARTH_M = 6371008.8
Point = tuple[float, float]  # (lat, lon)


def load_shapes(table: Table | None) -> dict[str, list[Point]]:
    """shape_id -> points in shape_pt_sequence order; unreadable rows are skipped."""
    if table is None or table.status != "ok":
        return {}
    h = table.header
    need = ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence")
    if not all(c in h for c in need):
        return {}
    i, la, lo, sq = (h.index(c) for c in need)
    rows: dict[str, list[tuple[float, Point]]] = defaultdict(list)
    for r in table.rows:
        try:
            lat, lon, seq = float(r[la]), float(r[lo]), float(r[sq])
        except ValueError:
            continue
        if math.isfinite(lat) and math.isfinite(lon) and math.isfinite(seq):
            rows[r[i]].append((seq, (lat, lon)))
    return {sid: [p for _, p in sorted(pts)] for sid, pts in rows.items() if len(pts) >= 2}


def _xy(p: Point, lat0: float) -> tuple[float, float]:
    """Local metres around latitude lat0 (equirectangular; fine at route scale)."""
    return (math.radians(p[1]) * EARTH_M * math.cos(math.radians(lat0)), math.radians(p[0]) * EARTH_M)


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def simplify(points: list[Point], tolerance_m: float) -> list[Point]:
    """Douglas-Peucker in local metres; keeps the first and last point."""
    if len(points) < 3:
        return list(points)
    lat0 = points[0][0]
    xy = [_xy(p, lat0) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        best, idx = 0.0, -1
        for k in range(a + 1, b):
            d = _seg_dist(xy[k], xy[a], xy[b])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tolerance_m:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def encode(points: list[Point]) -> list[list[int]]:
    """[[lat * 1e5, lon * 1e5], ...]: about 1 m, compact in JSON."""
    return [[round(p[0] * 1e5), round(p[1] * 1e5)] for p in points]


def length_m(points: list[Point]) -> float:
    if len(points) < 2:
        return 0.0
    lat0 = points[0][0]
    xy = [_xy(p, lat0) for p in points]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:]))


def _samples(xy: list[tuple[float, float]], step: float) -> list[tuple[tuple[float, float], int]]:
    """Points every `step` metres along the line, each with the index of its segment."""
    out = [(xy[0], 0)]
    carry = 0.0
    for k, (a, b) in enumerate(zip(xy, xy[1:])):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        pos = step - carry
        while pos <= seg:
            t = pos / seg
            out.append(((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])), k))
            pos += step
        carry = (carry + seg) % step
    out.append((xy[-1], len(xy) - 2))
    return out


class _Index:
    """Segments of a line in a square grid, for distance queries up to `cap` metres."""

    def __init__(self, xy: list[tuple[float, float]], cap: float) -> None:
        self.xy, self.cell = xy, cap
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for k, (a, b) in enumerate(zip(xy, xy[1:])):
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for gx in range(math.floor(x0 / cap), math.floor(x1 / cap) + 1):
                for gy in range(math.floor(y0 / cap), math.floor(y1 / cap) + 1):
                    self.grid[(gx, gy)].append(k)

    def distance(self, p) -> float:
        """Distance to the line, or `cap` when it is at least that far."""
        gx, gy = math.floor(p[0] / self.cell), math.floor(p[1] / self.cell)
        best = self.cell
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k in self.grid.get((gx + dx, gy + dy), ()):
                    best = min(best, _seg_dist(p, self.xy[k], self.xy[k + 1]))
        return best


def compare(old: list[Point], new: list[Point], sample_m: float, diverge_m: float, cap_m: float) -> dict:
    """How far each line runs from the other.

    max_m: the largest distance of any sample to the other line (capped at cap_m);
    diverged_old_m / diverged_new_m: length of each line farther than diverge_m from the other;
    old_spans / new_spans: [first, last] point indices of those parts, for drawing.
    """
    lat0 = (old[0][0] + new[0][0]) / 2
    oxy, nxy = [_xy(p, lat0) for p in old], [_xy(p, lat0) for p in new]
    result = {"max_m": 0}
    for side, this, other in (("new", nxy, oxy), ("old", oxy, nxy)):
        index = _Index(other, cap_m)
        spans: list[list[int]] = []
        far = 0.0
        biggest = 0.0
        for (p, seg) in _samples(this, sample_m):
            d = index.distance(p)
            biggest = max(biggest, d)
            if d > diverge_m:
                far += sample_m
                if spans and spans[-1][1] >= seg - 1:
                    spans[-1][1] = seg + 1
                else:
                    spans.append([seg, seg + 1])
        result["max_m"] = max(result["max_m"], round(biggest))
        result[f"diverged_{side}_m"] = round(far)
        result[f"{side}_spans"] = spans
    result["capped"] = result["max_m"] >= cap_m
    return result
