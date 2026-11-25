"""The reserve: its true boundary, the ArduPilot fence inside it, the grid of cells, the launch points.

The fence is the boundary simplified to at most reserve.fence_max_vertices points (Douglas-Peucker with the
smallest tolerance that fits) and then pulled in by that tolerance, so it lies inside the true boundary;
fence_inside_boundary() checks that, and the plan refuses a fence that fails it. The route then keeps
reserve.plan_margin_m inside the fence: a cell is "safe" when its centre is that far inside.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from hima.autonomy import polygon as P
from hima.autonomy.geodesy import LocalFrame
from hima.config import Config

FENCE_TOLERANCES_M = (10.0, 20.0, 50.0, 100.0, 150.0, 200.0, 300.0, 500.0, 800.0, 1200.0, 2000.0)


class ReserveError(ValueError):
    pass


def read_boundary(path: str | Path) -> tuple[list[tuple[float, float]], dict]:
    """The outer ring (lat, lon) of the largest polygon in a GeoJSON file, and that feature's properties."""
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReserveError(f"cannot read the reserve boundary {p}: {e}") from e
    feats = doc.get("features") if doc.get("type") == "FeatureCollection" else [doc]
    best, props, best_area = None, {}, -1.0
    for f in feats or []:
        g = f.get("geometry") or {}
        polys = [g.get("coordinates")] if g.get("type") == "Polygon" else (
            g.get("coordinates") if g.get("type") == "MultiPolygon" else [])
        for poly in polys or []:
            outer = poly[0]
            lat0 = sum(c[1] for c in outer) / len(outer)
            k = math.cos(math.radians(lat0))
            a = abs(P.signed_area([(lon * k, lat) for lon, lat, *_ in outer]))
            if a > best_area:
                best, props, best_area = outer, f.get("properties") or {}, a
    if not best or len(best) < 4:
        raise ReserveError(f"{p}: no polygon found")
    return [(float(c[1]), float(c[0])) for c in best], props


@dataclass
class Reserve:
    name: str
    frame: LocalFrame
    ring: list[tuple[float, float]]           # true boundary, local metres, counter-clockwise
    fence: list[tuple[float, float]]          # inside the true boundary, <= fence_max_vertices points
    fence_tol_m: float
    cell_m: float
    plan_margin_m: float
    x0: float = 0.0
    y0: float = 0.0
    nx: int = 0
    ny: int = 0
    inside: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))   # centre inside the fence
    safe: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))     # and >= plan_margin_m from it
    props: dict = field(default_factory=dict)

    # ---- building --------------------------------------------------------------------------------------
    @classmethod
    def from_latlon(cls, ring_latlon: Sequence[tuple[float, float]], *, name: str, fence_max_vertices: int,
                    cell_m: float, plan_margin_m: float, props: dict | None = None) -> "Reserve":
        lats = [a for a, _ in ring_latlon]
        lons = [b for _, b in ring_latlon]
        frame = LocalFrame((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
        ring = P.ccw([frame.to_xy(a, b) for a, b in ring_latlon])
        fence, tol = make_fence(ring, fence_max_vertices)
        r = cls(name=name, frame=frame, ring=ring, fence=fence, fence_tol_m=tol, cell_m=cell_m,
                plan_margin_m=plan_margin_m, props=props or {})
        r._grid()
        return r

    @classmethod
    def load(cls, cfg: Config) -> "Reserve":
        ring, props = read_boundary(resolve(cfg, cfg.reserve.boundary))
        return cls.from_latlon(ring, name=cfg.reserve.name, fence_max_vertices=cfg.reserve.fence_max_vertices,
                               cell_m=cfg.reserve.cell_m, plan_margin_m=cfg.reserve.plan_margin_m, props=props)

    def _grid(self) -> None:
        xs = [p[0] for p in self.fence]
        ys = [p[1] for p in self.fence]
        c = self.cell_m
        self.x0, self.y0 = math.floor(min(xs) / c) * c, math.floor(min(ys) / c) * c
        self.nx = int(math.ceil((max(xs) - self.x0) / c))
        self.ny = int(math.ceil((max(ys) - self.y0) / c))
        cx, cy = self.centres()
        self.inside = points_in(self.fence, cx, cy)
        d = np.full(cx.shape, np.inf)
        n = len(self.fence)
        for i in range(n):
            ax, ay = self.fence[i]
            bx, by = self.fence[(i + 1) % n]
            d = np.minimum(d, seg_dist_np(cx, cy, ax, ay, bx, by))
        self.edge_dist = d
        self.safe = self.inside & (d >= self.plan_margin_m)

    # ---- grid helpers ----------------------------------------------------------------------------------
    @property
    def n_cells(self) -> int:
        return self.nx * self.ny

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        k = np.arange(self.nx * self.ny)
        return self.x0 + (k % self.nx + 0.5) * self.cell_m, self.y0 + (k // self.nx + 0.5) * self.cell_m

    def cell_of(self, x: float, y: float) -> int | None:
        i = int(math.floor((x - self.x0) / self.cell_m))
        j = int(math.floor((y - self.y0) / self.cell_m))
        return j * self.nx + i if 0 <= i < self.nx and 0 <= j < self.ny else None

    def cells_on(self, a: tuple[float, float], b: tuple[float, float]) -> list[int]:
        """Grid cells a straight leg passes through (sampled every quarter cell), in order, no repeats."""
        L = math.dist(a, b)
        n = max(1, int(math.ceil(L / (self.cell_m / 4))))
        out: list[int] = []
        for s in range(n + 1):
            t = s / n
            k = self.cell_of(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            if k is not None and (not out or out[-1] != k):
                out.append(k)
        return out

    def leg_safe(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        cells = self.cells_on(a, b)
        return bool(cells) and all(self.safe[k] for k in cells)

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return self.frame.to_latlon(x, y)

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return self.frame.to_xy(lat, lon)

    def fence_latlon(self) -> list[tuple[float, float]]:
        return [self.to_latlon(x, y) for x, y in self.fence]

    def summary(self) -> dict:
        return {"name": self.name, "boundary_points": len(self.ring),
                "boundary_area_km2": round(P.signed_area(self.ring) / 1e6, 1),
                "boundary_perimeter_km": round(P.perimeter(self.ring) / 1e3, 1),
                "fence_points": len(self.fence), "fence_tolerance_m": self.fence_tol_m,
                "fence_area_km2": round(P.signed_area(self.fence) / 1e6, 1),
                "cell_m": self.cell_m, "cells_inside_fence": int(self.inside.sum()),
                "cells_safe": int(self.safe.sum()), "plan_margin_m": self.plan_margin_m,
                "wdpa_area_km2": self.props.get("gis_area"), "wdpa_site_id": self.props.get("site_id")}


def make_fence(ring: Sequence[tuple[float, float]], max_vertices: int) -> tuple[list[tuple[float, float]], float]:
    """Smallest tolerance from FENCE_TOLERANCES_M whose simplified-and-pulled-in ring fits and stays inside."""
    for tol in FENCE_TOLERANCES_M:
        s = P.simplify(ring, tol)
        if len(s) > max_vertices:
            continue
        f = P.offset_inward(s, tol)
        if P.signed_area(f) > 0 and P.is_simple(f) and P.inside_of(f, ring):
            return f, tol
    raise ReserveError(f"no fence of <= {max_vertices} points fits inside the boundary")


def fence_inside_boundary(r: Reserve) -> bool:
    return P.is_simple(r.fence) and P.inside_of(r.fence, r.ring)


def points_in(ring: Sequence[tuple[float, float]], x: np.ndarray, y: np.ndarray) -> np.ndarray:
    inside = np.zeros(x.shape, bool)
    n = len(ring)
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[i - 1]
        if yi == yj:
            continue
        cross = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi) + xi)
        inside ^= cross
    return inside


def seg_dist_np(px: np.ndarray, py: np.ndarray, ax: float, ay: float, bx: float, by: float) -> np.ndarray:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = np.zeros(px.shape) if L2 == 0 else np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
    return np.hypot(px - ax - t * dx, py - ay - t * dy)


def resolve(cfg: Config, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() or not cfg.source else Path(cfg.source).parent / q
