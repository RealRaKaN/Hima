"""Herd cohesion, isolation, and drone distance flown (all in metres).

cohesion_m: mean distance of the animals to their centroid.
isolation:  an animal is isolated when its NEAREST NEIGHBOUR is farther than
            isolation_m. (A centroid rule was rejected: one far animal drags the
            centroid and makes healthy herd members look isolated; see test_herd.py.)
            With exactly two animals far apart, both are isolated (cannot tell who left).
"""
from __future__ import annotations

import math

Point = tuple[float, float]


def cohesion_m(points: dict[int, Point]) -> float | None:
    if len(points) < 2:
        return None
    cx = sum(p[0] for p in points.values()) / len(points)
    cy = sum(p[1] for p in points.values()) / len(points)
    return sum(math.hypot(x - cx, y - cy) for x, y in points.values()) / len(points)


def nearest_neighbour_m(points: dict[int, Point]) -> dict[int, float]:
    out = {}
    for i, (xi, yi) in points.items():
        out[i] = min(math.hypot(xi - xj, yi - yj) for j, (xj, yj) in points.items() if j != i)
    return out


def isolated_ids(points: dict[int, Point], isolation_m: float) -> set[int]:
    if len(points) < 2:
        return set()
    return {i for i, d in nearest_neighbour_m(points).items() if d > isolation_m}


def drone_distance_m(poses: list[tuple[float, float, float]]) -> tuple[float, float]:
    """(horizontal, 3D) path length of the drone from (x, y, agl) samples."""
    horiz = d3 = 0.0
    for (x1, y1, z1), (x2, y2, z2) in zip(poses, poses[1:]):
        h = math.hypot(x2 - x1, y2 - y1)
        horiz += h
        d3 += math.hypot(h, z2 - z1)
    return horiz, d3
