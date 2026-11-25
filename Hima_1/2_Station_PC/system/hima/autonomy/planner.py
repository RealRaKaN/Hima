"""Today's route, made on the Jetson before take-off.

1. Budget. The usable battery (1 - vehicle.reserve_frac) pays for the vertical take-off and landing
   (hover_power_ratio times dearer) and forward flight; planner.herd_share of the forward flight is kept
   free for following herds in flight. What is left, times the cruise speed, is the route length.
2. Score every cell of the grid:
       w_age  * how long since a flight crossed it (full after age_full_days; never seen = full)
     + w_risk * poaching incidents near it   (1 - exp(-sum of Gaussian kernels, halved every half-life))
     + w_herd * herds seen near it           (same form, shorter half-life: herds move)
3. Route: a closed loop from the launch point. Cells are offered best first (at least target_spacing_m
   apart); each step inserts the offered cell that adds the most score per added metre, counting every
   new cell the two new legs cross. Every leg must stay on "safe" cells (plan_margin_m inside the fence).
   2-opt shortens the loop between insertions. Stop when nothing fits the budget.

The LLM plays no part here; the result is plain numbers that the fixed rules and ArduPilot check.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hima.autonomy.history import parse_date
from hima.autonomy.reserve import Reserve, ReserveError
from hima.config import Config

DAY_S = 86400.0
SEEDS = 12                     # loops also grown from each of the 12 best offered cells


class PlanError(ValueError):
    pass


# ---- 1. budget ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Budget:
    usable_s: float           # battery that may be planned, in seconds of forward flight
    hover_cost_s: float       # take-off + landing, in seconds of forward flight
    cruise_s: float           # forward flight left
    herd_s: float             # kept for herds in flight
    patrol_s: float           # the planned route
    route_m: float
    reach_m: float            # farthest a target can be from the launch point (out and back)
    swath_m: float            # camera footprint across the track at patrol height

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self.__dict__.items()}


def budget(cfg: Config) -> Budget:
    v = cfg.vehicle
    usable = v.cruise_endurance_min * 60.0 * (1 - v.reserve_frac)
    hover = (v.takeoff_hover_s + v.landing_hover_s) * v.hover_power_ratio
    cruise = usable - hover
    if cruise <= 0:
        raise PlanError(f"no forward flight left: take-off and landing cost {hover:.0f} s of {usable:.0f} s")
    herd = cruise * cfg.planner.herd_share
    patrol = cruise - herd
    route = patrol * v.cruise_speed_mps
    swath = 2 * v.patrol_alt_m * math.tan(math.radians(cfg.camera.hfov_deg) / 2)
    return Budget(usable, hover, cruise, herd, patrol, route, route / 2, swath)


# ---- 2. scores ----------------------------------------------------------------------------------------

@dataclass
class Scores:
    total: np.ndarray
    age: np.ndarray
    risk: np.ndarray
    herd: np.ndarray
    age_days: np.ndarray      # inf = never crossed


def _kernel_sum(r: Reserve, points: list[tuple[float, float, float]], radius_m: float) -> np.ndarray:
    """sum of weight * exp(-d^2 / 2 r^2) over (x, y, weight) points, at every cell centre."""
    cx, cy = r.centres()
    s = np.zeros(cx.shape)
    cut = 4 * radius_m
    for x, y, w in points:
        near = (np.abs(cx - x) < cut) & (np.abs(cy - y) < cut)
        if near.any():
            d2 = (cx[near] - x) ** 2 + (cy[near] - y) ** 2
            s[near] += w * np.exp(-d2 / (2 * radius_m ** 2))
    return s


def last_visit_days(r: Reserve, hist: dict[str, Any], today: dt.date) -> np.ndarray:
    age = np.full(r.n_cells, np.inf)
    for f in hist.get("flights", []):
        d = (today - parse_date(f["date"])).days
        if d < 0:
            continue
        pts = [r.to_xy(a, b) for a, b in f.get("path", [])]
        for a, b in zip(pts, pts[1:]):
            for k in r.cells_on(a, b):
                age[k] = min(age[k], d)
        if len(pts) == 1:
            k = r.cell_of(*pts[0])
            if k is not None:
                age[k] = min(age[k], d)
    return age


def score(cfg: Config, r: Reserve, hist: dict[str, Any], today: dt.date) -> Scores:
    pl = cfg.planner
    age_days = last_visit_days(r, hist, today)
    age = np.minimum(1.0, age_days / pl.age_full_days)

    def decayed(items: list[dict], half: float) -> list[tuple[float, float, float]]:
        out = []
        for it in items:
            d = (today - parse_date(it["date"])).days
            if d < 0:
                continue
            x, y = r.to_xy(it["lat"], it["lon"])
            out.append((x, y, 0.5 ** (d / half)))
        return out

    risk = 1 - np.exp(-_kernel_sum(r, decayed(hist.get("incidents", []), pl.risk_half_life_days), pl.risk_radius_m))
    herd = 1 - np.exp(-_kernel_sum(r, decayed(hist.get("sightings", []), pl.herd_half_life_days), pl.herd_radius_m))
    total = pl.w_age * age + pl.w_risk * risk + pl.w_herd * herd
    total = np.where(r.safe, total, 0.0)
    return Scores(total, age, risk, herd, age_days)


# ---- 3. route -----------------------------------------------------------------------------------------

@dataclass
class Plan:
    launch: str
    launch_latlon: tuple[float, float]
    date: str
    budget: Budget
    targets_xy: list[tuple[float, float]]
    targets_latlon: list[tuple[float, float]]
    length_m: float
    cells: list[int]                       # distinct cells the route crosses
    value: float                           # sum of their scores
    never_seen_cells: int
    risk_cells: int                        # crossed cells with risk >= 0.5
    herd_cells: int                        # crossed cells with herd >= 0.5
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        b = self.budget
        return {"launch": self.launch, "date": self.date, "targets": len(self.targets_xy),
                "route_km": round(self.length_m / 1e3, 2), "route_budget_km": round(b.route_m / 1e3, 2),
                "patrol_min": round(self.length_m / b.route_m * b.patrol_s / 60, 1) if b.route_m else 0.0,
                "herd_time_min": round(b.herd_s / 60, 1), "cells_crossed": len(self.cells),
                "never_seen_cells": self.never_seen_cells, "risk_cells": self.risk_cells,
                "herd_cells": self.herd_cells, "value": round(self.value, 2),
                "camera_area_km2": round(self.length_m * b.swath_m / 1e6, 3), "notes": self.notes}


class _Route:
    def __init__(self, r: Reserve, sc: np.ndarray, base: tuple[float, float]) -> None:
        self.r, self.sc = r, sc
        self.pts = [base, base]
        self.count: dict[int, int] = {}
        self._legs: dict[tuple, list[int] | None] = {}

    def leg(self, a, b) -> list[int] | None:
        key = (a, b) if a <= b else (b, a)
        if key not in self._legs:
            cells = self.r.cells_on(a, b)
            self._legs[key] = cells if cells and all(self.r.safe[k] for k in cells) else None
        return self._legs[key]

    def value(self) -> float:
        return float(sum(self.sc[k] for k in self.count))

    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.pts, self.pts[1:]))

    def _add(self, cells, n=1):
        for k in cells:
            self.count[k] = self.count.get(k, 0) + n

    def rebuild(self) -> None:
        self.count = {}
        for a, b in zip(self.pts, self.pts[1:]):
            if a != b:
                self._add(self.leg(a, b) or [])

    def gain(self, i: int, c) -> float | None:
        """Score of the cells that inserting c after position i adds (None: a leg leaves the safe area)."""
        a, b = self.pts[i], self.pts[i + 1]
        la, lb = self.leg(a, c), self.leg(c, b)
        if la is None or lb is None:
            return None
        old = self.leg(a, b) if a != b else []
        self._add(old or [], -1)
        seen: set[int] = set()
        g = 0.0
        for k in la + lb:
            if k not in seen and self.count.get(k, 0) <= 0:
                g += self.sc[k]
            seen.add(k)
        self._add(old or [], 1)
        return g

    def insert(self, i: int, c) -> None:
        self.pts.insert(i + 1, c)
        self.rebuild()

    def two_opt(self) -> None:
        improved = True
        while improved:
            improved = False
            p = self.pts
            for i in range(1, len(p) - 2):
                for j in range(i + 1, len(p) - 1):
                    a, b, c, d = p[i - 1], p[i], p[j], p[j + 1]
                    delta = math.dist(a, c) + math.dist(b, d) - math.dist(a, b) - math.dist(c, d)
                    if delta < -1e-6 and self.leg(a, c) is not None and self.leg(b, d) is not None:
                        p[i:j + 1] = reversed(p[i:j + 1])
                        improved = True
        self.rebuild()


def candidates(cfg: Config, r: Reserve, sc: np.ndarray, base: tuple[float, float], reach_m: float,
               rng: random.Random) -> list[tuple[float, float]]:
    cx, cy = r.centres()
    d = np.hypot(cx - base[0], cy - base[1])
    ok = r.safe & (d <= reach_m) & (d >= cfg.vehicle.land_approach_m) & (sc > 0)   # room for the VTOL landing approach
    idx = np.nonzero(ok)[0]
    jitter = np.array([rng.random() * 1e-6 for _ in range(len(idx))])     # ties: a different pick each day
    order = idx[np.argsort(-(sc[idx] + jitter), kind="stable")]
    picked: list[tuple[float, float]] = []
    sp = cfg.planner.target_spacing_m
    for k in order:
        p = (float(cx[k]), float(cy[k]))
        if all(math.dist(p, q) >= sp for q in picked):
            picked.append(p)
            if len(picked) >= cfg.planner.candidates:
                break
    return picked


def _grow(r: Reserve, sc: np.ndarray, base, cand, route_m: float, seed) -> "_Route | None":
    route = _Route(r, sc, base)
    left = set(cand)
    if seed is not None:
        if 2 * math.dist(base, seed) > route_m or route.gain(0, seed) is None:
            return None
        route.insert(0, seed)
        left.discard(seed)
    L = route.length()
    for _ in range(10000):
        best = None
        for c in left:
            if 2 * math.dist(base, c) > route_m:
                continue
            for i in range(len(route.pts) - 1):
                p, q = route.pts[i], route.pts[i + 1]
                add = math.dist(p, c) + math.dist(c, q) - math.dist(p, q)
                if L + add > route_m:
                    continue
                g = route.gain(i, c)
                if g is None or g <= 0:
                    continue
                ratio = g / max(add, 1.0)
                if best is None or ratio > best[0]:
                    best = (ratio, i, c)
        if best is None:
            break
        route.insert(best[1], best[2])
        left.discard(best[2])
        route.two_opt()
        L = route.length()
    return route


def plan_route(cfg: Config, r: Reserve, sc: Scores, launch: str, today: dt.date) -> Plan:
    if launch not in cfg.reserve.launch:
        raise PlanError(f"unknown launch point {launch!r} (reserve.launch: {', '.join(cfg.reserve.launch)})")
    la, lo = cfg.reserve.launch[launch]
    base = r.to_xy(la, lo)
    k0 = r.cell_of(*base)
    if k0 is None or not r.safe[k0]:
        raise PlanError(f"launch point {launch} is not {r.plan_margin_m:g} m inside the fence")
    b = budget(cfg)
    rng = random.Random(today.toordinal())
    cand = candidates(cfg, r, sc.total, base, b.reach_m, rng)
    # Greedy insertion alone is short-sighted: near cells always pay about one new cell per cell_m, so a
    # far poaching site or herd is never worth its first long leg. So the loop is also built from each of
    # the SEEDS best offered cells placed first, and the loop with the highest value wins.
    route = None
    for seed in [None] + cand[:SEEDS]:
        rt = _grow(r, sc.total, base, cand, b.route_m, seed)
        if rt is not None and (route is None or rt.value() > route.value() + 1e-9):
            route = rt
    L = route.length()
    cells = sorted(route.count)
    notes = []
    if len(route.pts) == 2:
        notes.append("no target fits the budget from this launch point")
    t = route.pts[1:-1]
    return Plan(launch=launch, launch_latlon=(la, lo), date=today.isoformat(), budget=b, targets_xy=t,
                targets_latlon=[r.to_latlon(x, y) for x, y in t], length_m=L, cells=cells,
                value=float(sum(sc.total[k] for k in cells)),
                never_seen_cells=int(sum(1 for k in cells if math.isinf(sc.age_days[k]))),
                risk_cells=int(sum(1 for k in cells if sc.risk[k] >= 0.5)),
                herd_cells=int(sum(1 for k in cells if sc.herd[k] >= 0.5)), notes=notes)


def best_plan(cfg: Config, r: Reserve, hist: dict[str, Any], today: dt.date,
              launch: str | None = None) -> tuple[Plan, list[Plan], Scores]:
    """The plan from the given launch point, or from the one with the highest value."""
    sc = score(cfg, r, hist, today)
    names = [launch] if launch else list(cfg.reserve.launch)
    plans = []
    for n in names:
        try:
            plans.append(plan_route(cfg, r, sc, n, today))
        except PlanError:
            if launch:
                raise
    if not plans:
        raise PlanError("no launch point is inside the safe area")
    return max(plans, key=lambda p: p.value), plans, sc


# ---- launch points ------------------------------------------------------------------------------------

def propose_launch_points(cfg: Config, r: Reserve, grid_step_m: float = 2000.0, goal: float = 0.98,
                          max_points: int = 80) -> tuple[list[tuple[float, float]], float]:
    """Greedy cover: launch points (local x, y) until goal of the safe cells is within one flight's reach.

    The first point covers the most cells; among equal ones it is the one nearest the reserve's centre.
    """
    reach = budget(cfg).reach_m
    cx, cy = r.centres()
    safe = np.nonzero(r.safe)[0]
    if len(safe) == 0:
        raise ReserveError("no safe cell")
    sx, sy = cx[safe], cy[safe]
    step = max(1, int(round(grid_step_m / r.cell_m)))
    cand = [k for k in safe if ((k % r.nx) % step == 0 and (k // r.nx) % step == 0)]
    mx, my = float(sx.mean()), float(sy.mean())
    covered = np.zeros(len(safe), bool)
    out: list[tuple[float, float]] = []
    while covered.mean() < goal and len(out) < max_points:
        best = None
        for k in cand:
            x, y = float(cx[k]), float(cy[k])
            new = int((~covered & ((sx - x) ** 2 + (sy - y) ** 2 <= reach ** 2)).sum())
            key = (new, -math.hypot(x - mx, y - my))
            if best is None or key > best[0]:
                best = (key, (x, y))
        if best is None or best[0][0] == 0:
            break
        x, y = best[1]
        out.append((x, y))
        covered |= (sx - x) ** 2 + (sy - y) ** 2 <= reach ** 2
    return out, float(covered.mean())


# ---- a route edited at the station -------------------------------------------------------------

def plan_from_targets(cfg: Config, r: Reserve, launch: str, today: dt.date,
                      targets_latlon: list[tuple[float, float]], sc: Scores | None = None) -> Plan:
    """The Plan for targets chosen by people (the dashboard's route editor), checked like the planner's own routes:
    every leg (from the launch point, through the targets, back) stays in the safe area (plan_margin_m inside the
    fence) and the loop fits the route budget. PlanError says what is wrong."""
    if launch not in cfg.reserve.launch:
        raise PlanError(f"unknown launch point {launch!r}")
    if not targets_latlon:
        raise PlanError("the route has no points")
    la, lo = cfg.reserve.launch[launch]
    base = r.to_xy(la, lo)
    b = budget(cfg)
    pts = [base] + [r.to_xy(a, c) for a, c in targets_latlon] + [base]
    for i, (x, y) in enumerate(pts[1:-1], 1):
        k = r.cell_of(x, y)
        if k is None or not r.safe[k]:
            raise PlanError(f"point {i} is not {r.plan_margin_m:g} m inside the fence")
        if math.dist((x, y), base) < cfg.vehicle.land_approach_m:
            raise PlanError(f"point {i} is closer than {cfg.vehicle.land_approach_m:g} m to the launch point "
                            "(VTOL landing approach)")
    cells: list[int] = []
    for i, (a, c) in enumerate(zip(pts, pts[1:])):
        leg = r.cells_on(a, c) if a != c else []
        if a != c and (not leg or not all(r.safe[k] for k in leg)):
            raise PlanError(f"leg {i + 1} leaves the safe area ({r.plan_margin_m:g} m inside the fence)")
        cells += leg
    L = sum(math.dist(a, c) for a, c in zip(pts, pts[1:]))
    if L > b.route_m + 1.0:
        raise PlanError(f"the route is {L / 1e3:.1f} km; the battery allows {b.route_m / 1e3:.1f} km")
    cells = sorted(set(cells))
    t = pts[1:-1]
    val = float(sum(sc.total[k] for k in cells)) if sc is not None else 0.0
    return Plan(launch=launch, launch_latlon=(la, lo), date=today.isoformat(), budget=b, targets_xy=t,
                targets_latlon=[tuple(p) for p in targets_latlon], length_m=L, cells=cells, value=val,
                never_seen_cells=int(sum(1 for k in cells if sc is not None and math.isinf(sc.age_days[k]))),
                risk_cells=int(sum(1 for k in cells if sc is not None and sc.risk[k] >= 0.5)),
                herd_cells=int(sum(1 for k in cells if sc is not None and sc.herd[k] >= 0.5)),
                notes=["edited at the station"])
