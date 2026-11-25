"""Autonomy, part 2: what the drone does in flight.

States
  PATROL          the planned route, ArduPilot AUTO (fixed wing)
  FOLLOW_HERD     over a herd, ArduPilot GUIDED: it flies to the herd's centre as a plane, and there ArduPilot changes
                  to VTOL and hovers (Q_GUIDED_MODE = 1, quadplane.cpp: "switch to VTOL flight when the guided
                  destination is reached and hover"). It stays over the herd and moves only when the herd's centre is
                  more than hover_move_m from where it hovers ("stop in place or move a little",
                  not passes back and forth like a plane)
  FOLLOW_VEHICLE  the same over a vehicle near a herd, until the vehicle leaves the herd
  RTL             going home: energy, or ArduPilot itself chose RTL (fence, failsafe); no more commands
  MANUAL          the pilot took over (a mode we did not ask for); back to PATROL when the pilot selects AUTO

Rules
  - In-flight following has its own time: planner.herd_share of the forward flight (Budget.herd_s, in forward-flight
    seconds). Hovering draws vehicle.hover_power_ratio times the power, so one second of hover costs that many seconds
    of the budget (and of the energy left). A drone within hover_arrive_m of its hover point counts as hovering.
  - A herd: >= herd_min_animals within herd_link_m of each other (each animal at its last place, kept
    herd_memory_s). Hovered over at most follow_herd_max_s, until lost herd_lost_s, and not again within
    herd_revisit_s / herd_revisit_m (the same for a vehicle already followed).
  - A vehicle is a danger only within vehicle_near_herd_m (200 m) of a herd (a single animal counts here); within
    vehicle_close_m (30 m) the station gets "HIMA VEH CLOSE" at once. It is followed until it is more than
    vehicle_leave_m from the herd's last known place for vehicle_leave_s, or lost vehicle_lost_s; there is no time
    limit and the herd budget does not stop it: only the energy does. A vehicle near a herd
    beats a herd. All vehicles are one class (nothing is known yet about the reserve's own
    vehicles); [rangers].enabled = false keeps rangers.py out.
  - Energy: home when the forward flight left, minus the way home and the vertical landing, falls under
    energy_margin_s, or the battery reads reserve_frac + battery_margin_pct.
  - No pose: a follow cannot go on; back to the route.
  - Targets outside the safe area (plan_margin_m inside the fence) are never sent.

All positions are local metres (east, north) from the flight's home, like the session frames. The LLM plays no part.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from hima.autonomy.planner import Budget
from hima.config import Config

PATROL, FOLLOW_HERD, FOLLOW_VEHICLE, RTL, MANUAL = "PATROL", "FOLLOW_HERD", "FOLLOW_VEHICLE", "RTL", "MANUAL"
HOME_MODES = {"RTL", "QRTL", "QLAND", "LAND"}          # ArduPilot is bringing the drone home or down on its own


VEH_STILL_MPS = 0.5      # slower than this between two sightings: the vehicle stands
VEH_MAX_MPS = 30.0       # a faster estimate is noise from the ground projection
VEH_PREDICT_S = 15.0     # out of the picture: on where it was going this long at most, then wait there (lost_s)


@dataclass(frozen=True)
class Command:
    kind: str                       # "mode" | "goto" | "say"
    mode: str | None = None
    x: float | None = None
    y: float | None = None
    alt: float | None = None
    text: str | None = None


@dataclass
class Herd:
    x: float
    y: float
    n: int
    last_t: float
    ids: tuple


@dataclass
class Vehicle:
    t: float
    x: float
    y: float
    owner: str | None               # None = unknown; else why it is the reserve's ("marker 3", "field op ...")


@dataclass
class Behaviour:
    cfg: Config
    budget: Budget
    is_safe: Callable[[float, float], bool] = lambda x, y: True
    state: str = PATROL
    animals: dict = field(default_factory=dict)          # animal id -> (t, x, y, species)
    vehicles: list = field(default_factory=list)         # recent Vehicle sightings
    visited: list = field(default_factory=list)          # (x, y, t_end) of herds already followed
    visited_veh: list = field(default_factory=list)      # (x, y, t_end) of vehicle follows (no follow again soon)
    follow_spent_s: float = 0.0                           # seconds spent following (clock time)
    follow_cost_s: float = 0.0                            # the same in forward-flight seconds (hover costs more)
    hover_s: float = 0.0                                  # seconds hovering (energy: hover_power_ratio each)
    log: list = field(default_factory=list)              # (t, state, why)
    follows: list = field(default_factory=list)          # one dict per follow
    _expect: str = "AUTO"
    _expect_since: float = 0.0
    _odd_since: float | None = None
    _last_t: float | None = None
    _last_pose: tuple | None = None
    _next_tick: float = -1e9
    _target: tuple | None = None                          # (x, y) of the guided (hover) point now
    _follow: dict | None = None
    _ranger_said: dict = field(default_factory=dict)      # owner -> t of the last "ranger" message
    _close_said: float = -1e9                             # t of the last "vehicle close" message

    # ---- what the camera saw --------------------------------------------------------------------------
    def see_animals(self, t: float, pts: list[tuple[int, str, float, float]]) -> None:
        for aid, sp, x, y in pts:
            self.animals[aid] = (t, x, y, sp)

    def see_vehicles(self, t: float, pts: list[tuple[float, float, str | None]]) -> None:
        for x, y, owner in pts:
            self.vehicles.append(Vehicle(t, x, y, owner))
        keep = max(self.cfg.behaviour.vehicle_lost_s, self.cfg.behaviour.herd_memory_s)
        self.vehicles = [v for v in self.vehicles if t - v.t <= keep]

    def herds(self, t: float, min_n: int) -> list[Herd]:
        """Single-linkage groups of the animals seen in the last herd_memory_s, at their last place."""
        bh = self.cfg.behaviour
        pts = [(aid, x, y, ts) for aid, (ts, x, y, _) in self.animals.items() if t - ts <= bh.herd_memory_s]
        groups: list[list] = []
        for p in pts:
            near = [g for g in groups if any(math.hypot(p[1] - q[1], p[2] - q[2]) <= bh.herd_link_m for q in g)]
            merged = [p]
            for g in near:
                merged += g
                groups.remove(g)
            groups.append(merged)
        out = []
        for g in groups:
            if len(g) >= min_n:
                out.append(Herd(sum(q[1] for q in g) / len(g), sum(q[2] for q in g) / len(g), len(g),
                                max(q[3] for q in g), tuple(sorted(q[0] for q in g))))
        return sorted(out, key=lambda h: (-h.n, -h.last_t))

    # ---- decisions --------------------------------------------------------------------------------------
    def energy_left_s(self, t: float, pos: tuple[float, float] | None) -> float:
        """Forward-flight seconds left after going home and landing (time model; t = mission s since arming)."""
        v = self.cfg.vehicle
        used = t + (v.takeoff_hover_s + self.hover_s) * (v.hover_power_ratio - 1)
        home = 0.0 if pos is None else math.hypot(*pos) / v.cruise_speed_mps
        return self.budget.usable_s - used - home - v.landing_hover_s * v.hover_power_ratio

    def hovering(self, pose) -> bool:
        """Following and within hover_arrive_m of the hover point (ArduPilot hovers there with Q_GUIDED_MODE)."""
        return (self.state in (FOLLOW_HERD, FOLLOW_VEHICLE) and pose is not None and self._target is not None
                and math.hypot(pose[0] - self._target[0], pose[1] - self._target[1]) <= self.cfg.behaviour.hover_arrive_m)

    def tick(self, t: float, pose: tuple[float, float, float] | None, mode: str | None,
             battery_pct: float | None = None, landing: bool = False) -> list[Command]:
        """Called every frame; decides at most every behaviour.tick_s. pose = (x, y, agl) or None."""
        bh = self.cfg.behaviour
        if self._last_t is not None and self.state in (FOLLOW_HERD, FOLLOW_VEHICLE):
            dt = max(0.0, t - self._last_t)
            self.follow_spent_s += dt
            if self.hovering(self._last_pose):
                self.hover_s += dt
                self.follow_cost_s += dt * self.cfg.vehicle.hover_power_ratio
                if self._follow is not None:
                    self._follow["_hover_s"] += dt
            else:
                self.follow_cost_s += dt
        self._last_t, self._last_pose = t, pose
        if t + 1e-9 < self._next_tick:
            return []
        self._next_tick = t + bh.tick_s
        out: list[Command] = []

        # 1. what ArduPilot is doing: its own return home, or the pilot
        if mode in HOME_MODES and self.state != RTL:
            self._to(t, RTL, f"autopilot mode {mode}")
            self._end_follow(t, "autopilot home")
            return [Command("say", text=f"HIMA AP {mode} @{t:.0f}")]
        if self.state == RTL:
            return []
        if mode is not None and mode != self._expect and t - self._expect_since > bh.manual_grace_s:
            self._odd_since = t if self._odd_since is None else self._odd_since
            if t - self._odd_since >= bh.manual_grace_s and self.state != MANUAL:
                self._end_follow(t, f"pilot mode {mode}")
                self._to(t, MANUAL, f"mode {mode} not asked for")
                return [Command("say", text=f"HIMA MANUAL {mode} @{t:.0f}")]
        else:
            self._odd_since = None
        if self.state == MANUAL:
            if mode == "AUTO":
                self._expect, self._expect_since = "AUTO", t
                self._to(t, PATROL, "pilot gave back AUTO")
                return [Command("say", text=f"HIMA AUTO BACK @{t:.0f}")]
            return []

        # 2. energy
        pos = None if pose is None else (pose[0], pose[1])
        left = self.energy_left_s(t, pos)
        low_batt = battery_pct is not None and battery_pct <= self.cfg.vehicle.reserve_frac * 100 + bh.battery_margin_pct
        if left <= bh.energy_margin_s or low_batt:
            self._end_follow(t, "energy")
            self._to(t, RTL, "battery" if low_batt else f"energy left {left:.0f} s")
            return self._mode(t, "RTL") + [Command("say", text=f"HIMA RTL ENERGY @{t:.0f}")]

        # 3. no position: no follow
        if pose is None:
            if self.state != PATROL:
                self._end_follow(t, "no position")
                out += self._mode(t, "AUTO") + [Command("say", text=f"HIMA RESUME NOPOS @{t:.0f}")]
                self._to(t, PATROL, "no position")
            return out

        budget_left = self.budget.herd_s - self.follow_cost_s
        up = not landing and pose[2] >= 0.8 * self.cfg.vehicle.patrol_alt_m
        # a herd needs at least herd_min_hover_s of hover left in the budget
        can_start = up and budget_left > bh.herd_min_hover_s * self.cfg.vehicle.hover_power_ratio

        # 4. a vehicle near a herd (beats everything but energy; the herd budget does not stop it)
        veh, d_herd = self._vehicle_near_herd(t)
        if veh is not None and veh.owner is not None:
            last = self._ranger_said.get(veh.owner)
            if last is None or t - last > 120.0:
                self._ranger_said[veh.owner] = t
                out.append(Command("say", text=f"HIMA VEH RANGER {veh.owner[:18]} @{t:.0f}"))
            veh = None
        if veh is not None and d_herd <= bh.vehicle_close_m and t - self._close_said > 120.0:
            self._close_said = t
            out.append(Command("say", text=f"HIMA VEH CLOSE {d_herd:.0f}m @{t:.0f}"))
        if self.state == FOLLOW_VEHICLE:
            return out + self._go_on_vehicle(t, pose)
        if veh is not None and up:
            self._end_follow(t, "vehicle near herd")
            return out + self._start(t, FOLLOW_VEHICLE, (veh.x, veh.y), f"vehicle {d_herd:.0f} m from a herd")

        # 5. herds
        if self.state == FOLLOW_HERD:
            return out + self._go_on_herd(t, pose, budget_left)
        if can_start:
            for h in self.herds(t, bh.herd_min_animals):
                if not self._visited(t, h) and t - h.last_t <= bh.herd_lost_s:
                    return out + self._start(t, FOLLOW_HERD, (h.x, h.y), f"herd of {h.n}", herd=h)
        return out

    # ---- follow mechanics: fly there, hover, move a little ----------------------------------------
    def _start(self, t, state, at, why, herd: Herd | None = None) -> list[Command]:
        if not self.is_safe(*at):
            return []                                     # never a target outside the safe area
        self._follow = {"kind": state, "start_t": round(t, 1), "at": [round(at[0], 1), round(at[1], 1)],
                        "why": why, "n": herd.n if herd else None, "spent_s": 0.0, "hover_s": 0.0, "moves": 0,
                        "_start_spent": self.follow_spent_s, "_hover_s": 0.0, "_last_seen": t, "_center": at,
                        "_far_since": None, "_herd": (herd.x, herd.y) if herd else None}
        self._to(t, state, why)
        word = "HERD" if state == FOLLOW_HERD else "VEH"
        extra = f" {herd.n}" if herd else ""
        return self._goto(t, at) + [Command("say", text=f"HIMA FOLLOW {word}{extra} @{t:.0f}")]

    def _go_on(self, t, pose, center, max_s, lost_s, budget_left=None) -> list[Command]:
        """Hover over center; a new hover point only when center moved more than hover_move_m."""
        f = self._follow
        f["spent_s"] = round(self.follow_spent_s - f["_start_spent"], 1)
        if t - f["_last_seen"] > lost_s:
            return self._resume(t, "lost")
        if max_s is not None and f["spent_s"] >= max_s:
            return self._resume(t, "time")
        if budget_left is not None and budget_left <= 0:
            return self._resume(t, "budget")
        f["_center"] = center
        bh = self.cfg.behaviour
        if self._target is None or math.hypot(center[0] - self._target[0], center[1] - self._target[1]) > bh.hover_move_m:
            if not self.is_safe(*center):
                return self._resume(t, "unsafe target")
            f["moves"] += 1
            return self._goto(t, center)
        return []

    def _go_on_herd(self, t, pose, budget_left) -> list[Command]:
        bh = self.cfg.behaviour
        c = self._follow["_center"]
        best = None
        for h in self.herds(t, 1):
            d = math.hypot(h.x - c[0], h.y - c[1])
            if d <= max(bh.herd_link_m * 3, 300.0) and (best is None or d < best[0]):
                best = (d, h)
        if best is not None:
            self._follow["_last_seen"] = best[1].last_t
            c = (best[1].x, best[1].y)
        return self._go_on(t, pose, c, bh.follow_herd_max_s, bh.herd_lost_s, budget_left)

    def _go_on_vehicle(self, t, pose) -> list[Command]:
        bh = self.cfg.behaviour
        f = self._follow
        c = f["_center"]
        # the herd's last known place: hovering over the vehicle, the herd (up to 200 m away) is out of the camera,
        # so it drops out of memory while it is still there (seen in SITL)
        hs = [h for h in self.herds(t, bh.vehicle_herd_min_animals)
              if math.hypot(h.x - c[0], h.y - c[1]) <= bh.vehicle_leave_m]
        if hs:
            h = min(hs, key=lambda h: math.hypot(h.x - c[0], h.y - c[1]))
            f["_herd"] = (h.x, h.y)
        near = [v for v in self.vehicles if v.owner is None and math.hypot(v.x - c[0], v.y - c[1]) <= 300.0]
        if near:
            v = max(near, key=lambda v: v.t)
            f["_last_seen"] = v.t
            # its speed from an earlier sighting 1 to 8 s before (a parked vehicle: 0)
            older = [u for u in near if 1.0 <= v.t - u.t <= 8.0]
            vel = (0.0, 0.0)
            if older:
                u = min(older, key=lambda u: abs(v.t - u.t - 3.0))     # about 3 s: less ground noise
                dt_ = v.t - u.t
                vel = ((v.x - u.x) / dt_, (v.y - u.y) / dt_)
                sp = math.hypot(*vel)
                vel = (0.0, 0.0) if sp < VEH_STILL_MPS else tuple(x * min(1.0, VEH_MAX_MPS / sp) for x in vel)
            f["_vel"], f["_seen_at"] = vel, (v.t, v.x, v.y)
            herd = f.get("_herd")
            far = herd is None or math.hypot(herd[0] - v.x, herd[1] - v.y) > bh.vehicle_leave_m
            if not far:
                f["_far_since"] = None
            elif f["_far_since"] is None:
                f["_far_since"] = t
            if far and t - f["_far_since"] >= bh.vehicle_leave_s:
                return self._resume(t, "vehicle left the herd")
        if f.get("_seen_at") is not None:
            # hover ahead of a moving vehicle; out of the picture: on where it was going, for VEH_PREDICT_S
            st, sx, sy = f["_seen_at"]
            lead = min(t - st, VEH_PREDICT_S) + bh.vehicle_lead_s
            c = (sx + f["_vel"][0] * lead, sy + f["_vel"][1] * lead)
        return self._go_on(t, pose, c, None, bh.vehicle_lost_s)

    def _vehicle_near_herd(self, t) -> tuple[Vehicle | None, float]:
        """The vehicle to act on (an unknown one first, then the newest) and its distance to the nearest herd."""
        bh = self.cfg.behaviour
        herds = self.herds(t, bh.vehicle_herd_min_animals)
        best, best_d = None, math.inf
        for v in self.vehicles:
            if t - v.t > bh.vehicle_lost_s:
                continue
            if v.owner is None and self._near_visit(t, v.x, v.y, self.visited_veh):
                continue                                  # followed it already (in SITL it restarted at once)
            d = min((math.hypot(v.x - h.x, v.y - h.y) for h in herds), default=math.inf)
            if d <= bh.vehicle_near_herd_m and (best is None or (v.owner is None, v.t) > (best.owner is None, best.t)):
                best, best_d = v, d
        return best, best_d

    def _visited(self, t, h: Herd) -> bool:
        return self._near_visit(t, h.x, h.y, self.visited)

    def _near_visit(self, t, px, py, visits) -> bool:
        bh = self.cfg.behaviour
        return any(t - te <= bh.herd_revisit_s and math.hypot(px - x, py - y) <= bh.herd_revisit_m
                   for x, y, te in visits)

    def _resume(self, t, why) -> list[Command]:
        self._end_follow(t, why)
        self._to(t, PATROL, f"resume: {why}")
        return self._mode(t, "AUTO") + [Command("say", text=f"HIMA RESUME {why.split()[0].upper()[:8]} @{t:.0f}")]

    def _end_follow(self, t, why) -> None:
        f = self._follow
        if f is None:
            return
        f["end_t"] = round(t, 1)
        f["end_why"] = why
        f["spent_s"] = round(self.follow_spent_s - f["_start_spent"], 1)
        f["hover_s"] = round(f["_hover_s"], 1)
        (self.visited if f["kind"] == FOLLOW_HERD else self.visited_veh).append((f["_center"][0], f["_center"][1], t))
        self.follows.append({k: v for k, v in f.items() if not k.startswith("_")})
        self._follow = None
        self._target = None

    # ---- commands ------------------------------------------------------------------------------------
    def _mode(self, t, mode) -> list[Command]:
        self._expect, self._expect_since = mode, t
        return [Command("mode", mode=mode)]

    def _goto(self, t, p) -> list[Command]:
        self._target = p
        self._expect, self._expect_since = "GUIDED", t
        return [Command("goto", x=p[0], y=p[1], alt=self.follow_alt())]

    def follow_alt(self) -> float:
        s = self.cfg.safety
        return min(s.alt_max_m, max(s.observe_min_m, s.alt_min_m, self.cfg.vehicle.patrol_alt_m))

    def _to(self, t, state, why) -> None:
        if state != self.state:
            self.log.append((round(t, 1), state, why))
            self.state = state

    def summary(self) -> dict[str, Any]:
        return {"state": self.state, "follow_spent_s": round(self.follow_spent_s, 1),
                "follow_cost_s": round(self.follow_cost_s, 1), "hover_s": round(self.hover_s, 1),
                "herd_budget_s": round(self.budget.herd_s, 1), "follows": self.follows,
                "states": self.log, "animals_seen": len(self.animals)}
