"""Synthetic mission generator v2.

Animals move in the WORLD (metres, m/s). The drone flies a scripted patrol. Each frame,
every animal whose position projects inside the image becomes a pixel detection
(with detector jitter and random drop-outs). Nothing computed (activity, stress,
herd state) is written to frames. The scenario of each animal goes to ground_truth,
which the analytics layer never reads.

Patrol (default 1800 s, camera 1280x720, hfov 80 deg; synthetic, not a specific drone):
  A   0-600 s   hover over herd site H (0, 0) at 80 m AGL
  B 600-660 s   fly east to site P (250, 0), descending to 60 m
  C 660-1080 s  hover over P, descend to 30 m (below alt_min on purpose)
  D 1080-~1138  low pass east toward site F (500, 0) at 30 m
  E ~1138-1300  follow the fleeing gazelle at 30 m, 10 m behind
  F 1300-1800   climb to 90 m, return home along y = 150 m (away from the sites)

Animals (track_id: species, scenario):
  101 reem_gazelle  NORMAL_GRAZING            herd at H
  105 reem_gazelle  NORMAL_GRAZING            herd at H (extra herd mate)
  203 arabian_oryx  NORMAL_GRAZING            herd at H (extra herd mate)
  202 arabian_oryx  OCCLUDED_INTERMITTENT     herd at H, under canopy 25 s of every 75 s
  103 reem_gazelle  INJURED_ISOLATED          rests ~45 m from the herd
  104 reem_gazelle  FAST_CORRIDOR_TRANSIT     one run along a corridor 31+ m from the herd
  201 arabian_oryx  CAUTION_PROXIMITY_PACING  paces under the low-hovering drone at P
  102 reem_gazelle  DRONE_PANIC_FLEEING       flees at 6 m/s when the low drone arrives at F

The extra herd mates exist so the herd stays a herd while 202 is hidden.
Timestamps are fixed so the same seed gives byte-identical output.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable

from hima.analytics.geometry import Camera, DronePose, ground_to_pixel, gsd_m_per_px
from hima.config import Config

SCENARIOS = (
    "NORMAL_GRAZING", "DRONE_PANIC_FLEEING", "INJURED_ISOLATED",
    "FAST_CORRIDOR_TRANSIT", "CAUTION_PROXIMITY_PACING", "OCCLUDED_INTERMITTENT",
)

# For evaluation only (hima.evaluate). The analytics layer never sees this.
EXPECTED_STATUS = {
    "NORMAL_GRAZING": "BASELINE",
    "DRONE_PANIC_FLEEING": "SEVERE_DRONE_DISTURBANCE",
    "INJURED_ISOLATED": "SUSPECTED_INJURY_OR_ILLNESS",
    "FAST_CORRIDOR_TRANSIT": "RAPID_CORRIDOR_TRANSIT",
    "CAUTION_PROXIMITY_PACING": "PROXIMITY_PACING_ALERTNESS",
    "OCCLUDED_INTERMITTENT": "INTERMITTENT_OCCLUSION",
}

CAMERA = {"width_px": 1280, "height_px": 720, "hfov_deg": 80.0, "mount": "nadir", "params_known": True}
BODY_LENGTH_M = {"reem_gazelle": 1.0, "arabian_oryx": 1.6}   # rough top-view size for bbox only
CREATED_AT = "2026-01-01T00:00:00Z"
PIXEL_JITTER_SD = 1.0      # detector centre noise (px)
DROPOUT_P = 0.01           # random missed detection per frame

# Patrol geometry (metres, seconds)
SITE_P = (250.0, 0.0)
SITE_F = (500.0, 0.0)
FLEE_TRIGGER_X = 490.0               # gazelle bolts when the low drone reaches this x
TRANSIT_SPEED = (SITE_F[0] - SITE_P[0]) / 60.0
FLEE_T = 1080.0 + (FLEE_TRIGGER_X - SITE_P[0]) / TRANSIT_SPEED
FLEE_RUN_MPS, FLEE_RUN_S, FLEE_WALK_MPS = 6.0, 60.0, 1.2
FOLLOW_BEHIND_M = 10.0


def _lerp(a: float, b: float, s: float) -> float:
    return a + (b - a) * s


def flee_x(t: float) -> float:
    if t <= FLEE_T:
        return SITE_F[0]
    run = min(t - FLEE_T, FLEE_RUN_S)
    walk = max(0.0, t - FLEE_T - FLEE_RUN_S)
    return SITE_F[0] + FLEE_RUN_MPS * run + FLEE_WALK_MPS * walk


def drone_pose(t: float) -> DronePose:
    if t < 600:
        return DronePose(2 * math.sin(2 * math.pi * t / 300), 2 * math.sin(2 * math.pi * t / 200), 80.0, 0.0)
    if t < 660:
        s = (t - 600) / 60
        return DronePose(_lerp(0.0, SITE_P[0], s), 0.0, _lerp(80.0, 60.0, s), 90.0)
    if t < 1080:
        agl = 60.0 - 30.0 * min(1.0, (t - 660) / 30)
        return DronePose(SITE_P[0] + 0.5 * math.sin(2 * math.pi * (t - 660) / 120), 0.0, agl, 90.0)
    if t < FLEE_T:
        return DronePose(SITE_P[0] + TRANSIT_SPEED * (t - 1080), 0.0, 30.0, 90.0)
    if t < 1300:
        return DronePose(flee_x(t) - FOLLOW_BEHIND_M, 0.0, 30.0, 90.0)
    x_end = flee_x(1300) - FOLLOW_BEHIND_M
    if t < 1340:
        s = (t - 1300) / 40
        return DronePose(x_end, 150.0 * s, _lerp(30.0, 90.0, s), 0.0)
    s = min(1.0, (t - 1340) / 460)
    return DronePose(_lerp(x_end, 0.0, s), 150.0, 90.0, 270.0)


# ---- animal motion (stateful, advanced once per frame in fixed order) --------------------

@dataclass
class _Walker:
    """Bout-based random walk kept inside a disc (grazing / resting / slow walking)."""
    rng: random.Random
    cx: float
    cy: float
    radius: float
    modes: tuple[tuple[float, float], ...]     # (probability, speed m/s)
    x: float = 0.0
    y: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    bout_left: float = 0.0

    def __post_init__(self):
        self.x, self.y = self.cx, self.cy

    def step(self, dt: float) -> tuple[float, float]:
        if self.bout_left <= 0:
            r, acc = self.rng.random(), 0.0
            for p, v in self.modes:
                acc += p
                if r <= acc:
                    self.speed = v
                    break
            self.heading = self.rng.uniform(0, 2 * math.pi)
            self.bout_left = self.rng.uniform(20, 40)
        if math.hypot(self.x - self.cx, self.y - self.cy) > self.radius:
            self.heading = math.atan2(self.cy - self.y, self.cx - self.x)   # turn back home
        self.x += self.speed * dt * math.cos(self.heading)
        self.y += self.speed * dt * math.sin(self.heading)
        self.bout_left -= dt
        return self.x, self.y


GRAZE = ((0.7, 0.1), (0.3, 0.8))
INJURED = ((0.85, 0.05), (0.15, 0.35))
HERD_C, HERD_R = (-8.0, 4.0), 6.0


def _animals(rng: random.Random) -> list[tuple[int, str, str, Callable[[float, float], tuple[float, float] | None]]]:
    def walker_fn(w: _Walker, hidden: Callable[[float], bool] = lambda t: False):
        def f(t, dt):
            p = w.step(dt)
            return None if hidden(t) else p
        return f

    def transit(t, dt):
        x = -75.0 + 4.5 * (t - 190.0)
        return (x, -33.0) if 190.0 <= t <= 240.0 else None

    def pacer(t, dt):
        phase = (t % 24.0) / 24.0                 # 1 m/s over a 12 m line, there and back
        y = -6.0 + 12.0 * (2 * phase if phase < 0.5 else 2 - 2 * phase)
        return (SITE_P[0], y)

    graze_102 = _Walker(rng, SITE_F[0], SITE_F[1], 2.0, GRAZE)
    start: list[tuple[float, float]] = []         # grazing position at the moment it bolts

    def fleer(t, dt):
        p = graze_102.step(dt)                    # keep RNG use identical every frame
        if t <= FLEE_T:
            return p
        if not start:
            start.append(p)
        x0, y0 = start[0]
        return (x0 + flee_x(t) - SITE_F[0], y0)

    return [
        (101, "reem_gazelle", "NORMAL_GRAZING", walker_fn(_Walker(rng, *HERD_C, HERD_R, GRAZE))),
        (105, "reem_gazelle", "NORMAL_GRAZING", walker_fn(_Walker(rng, -5.0, 7.0, 3.0, GRAZE))),
        (203, "arabian_oryx", "NORMAL_GRAZING", walker_fn(_Walker(rng, -11.0, 2.0, 3.0, GRAZE))),
        (202, "arabian_oryx", "OCCLUDED_INTERMITTENT",
         walker_fn(_Walker(rng, -6.0, 1.0, 3.0, GRAZE), hidden=lambda t: (t % 75.0) >= 50.0)),
        (103, "reem_gazelle", "INJURED_ISOLATED", walker_fn(_Walker(rng, 40.0, 12.0, 3.0, INJURED))),
        (104, "reem_gazelle", "FAST_CORRIDOR_TRANSIT", transit),
        (201, "arabian_oryx", "CAUTION_PROXIMITY_PACING", pacer),
        (102, "reem_gazelle", "DRONE_PANIC_FLEEING", fleer),
    ]


def generate(cfg: Config, *, duration_s: float = 1800.0, step_s: float = 1.0, seed: int = 42) -> dict[str, Any]:
    rng = random.Random(seed)
    animals = _animals(rng)
    cam = Camera(CAMERA["width_px"], CAMERA["height_px"], CAMERA["hfov_deg"])
    raw_name = {species: raw for raw, species in cfg.detector.class_map.items()}

    frames = []
    n = int(round(duration_s / step_s))
    for i in range(n):
        t = i * step_s
        pose = drone_pose(t)
        gsd = gsd_m_per_px(pose.agl_m, cam.hfov_deg, cam.width_px)
        dets = []
        for tid, species, _scenario, motion in animals:
            pos = motion(t, step_s)
            jx, jy = rng.gauss(0, PIXEL_JITTER_SD), rng.gauss(0, PIXEL_JITTER_SD)
            dropped = rng.random() < DROPOUT_P
            conf = rng.uniform(0.55, 0.95)
            if pos is None or dropped:
                continue
            u, v = ground_to_pixel(pos[0], pos[1], cam, pose)
            u, v = u + jx, v + jy
            if not (0 <= u <= cam.width_px and 0 <= v <= cam.height_px):
                continue
            length_px = BODY_LENGTH_M[species] / gsd
            dets.append({"track_id": tid, "species": species, "class_raw": raw_name[species],
                         "conf": round(conf, 3),
                         "bbox_xywh_px": [round(u, 2), round(v, 2), round(length_px, 2), round(length_px / 2, 2)]})
        frames.append({"t": round(t, 3),
                       "drone": {"x_m": round(pose.x_m, 3), "y_m": round(pose.y_m, 3),
                                 "agl_m": round(pose.agl_m, 3), "heading_deg": pose.heading_deg},
                       "detections": dets})

    seen = {d["track_id"] for f in frames for d in f["detections"]}
    return {
        "meta": {
            "schema_version": "hima.session/1",
            "mission_id": f"synthetic-v2-seed{seed}-{int(duration_s)}s",
            "source": "synthetic",
            "created_at": CREATED_AT,
            "camera": dict(CAMERA),
            "detector": {"weights": "none (synthetic)", "class_map": dict(cfg.detector.class_map),
                         "conf": cfg.detector.conf, "tracker": "none (synthetic ids)"},
            "geometry_assumptions": ["flat_ground", "nadir_camera"],
        },
        "frames": frames,
        "ground_truth": {str(tid): sc for tid, _, sc, _ in animals if tid in seen},
    }
