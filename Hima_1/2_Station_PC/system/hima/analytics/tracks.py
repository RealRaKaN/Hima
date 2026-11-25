"""Per-track ground observations and ground speed.

Speed is computed from the animal's WORLD position, so drone motion cancels out:
the drone pose is already inside pixel_to_ground. No compensation constant is needed.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hima.analytics.geometry import Camera, DronePose, gsd_m_per_px, pixel_to_ground

EDGE_PX = 2.0                  # a box this close to the image edge may be cut off: no body length from it


@dataclass(frozen=True)
class Observation:
    frame_index: int
    t: float
    dt: float                  # duration this observation stands for (frame duration)
    species: str
    x_m: float | None          # None when camera parameters are unknown
    y_m: float | None
    slant_m: float | None
    length_m: float | None = None    # long side of the box in metres; None if the box touches the edge


@dataclass
class Track:
    track_id: int
    observations: list[Observation] = field(default_factory=list)

    @property
    def species(self) -> str:
        """Most frequent species label; ties go to the label seen first."""
        counts = Counter(o.species for o in self.observations)
        first = {}
        for i, o in enumerate(self.observations):
            first.setdefault(o.species, i)
        return max(counts, key=lambda s: (counts[s], -first[s]))


def build_tracks(frames: list[dict[str, Any]], dts: list[float], cam: Camera,
                 metric: bool) -> tuple[dict[int, Track], list[list[Observation]]]:
    """Return (tracks by id, observations grouped by frame index)."""
    tracks: dict[int, Track] = {}
    per_frame: list[list[Observation]] = []
    for k, fr in enumerate(frames):
        pose = DronePose.from_frame(fr["drone"])
        row = []
        for d in fr["detections"]:
            u, v = d["bbox_xywh_px"][0], d["bbox_xywh_px"][1]
            length = None
            if metric:
                g = pixel_to_ground(u, v, cam, pose)
                x, y, slant = g.x_m, g.y_m, g.slant_m
                length = box_length_m(d["bbox_xywh_px"], cam, pose)
            else:
                x = y = slant = None
            obs = Observation(k, fr["t"], dts[k], d["species"], x, y, slant, length)
            tracks.setdefault(d["track_id"], Track(d["track_id"])).observations.append(obs)
            row.append((d["track_id"], obs))
        per_frame.append(row)
    return tracks, per_frame


def box_length_m(box: list[float], cam: Camera, pose: DronePose) -> float | None:
    """Long side of the box in metres at the drone's altitude (flat ground, nadir). None when the box is within
    EDGE_PX of the image edge: the animal may be partly outside the image, so the box is too short."""
    u, v, w, h = box
    if (u - w / 2 < EDGE_PX or v - h / 2 < EDGE_PX or u + w / 2 > cam.width_px - EDGE_PX
            or v + h / 2 > cam.height_px - EDGE_PX):
        return None
    return max(w, h) * gsd_m_per_px(pose.agl_m, cam.hfov_deg, cam.width_px)


def ground_speeds(ts: list[float], xs: list[float], ys: list[float],
                  window_s: float) -> list[float | None]:
    """Ground speed (m/s) at each observation of one track.

    At observation i, compare with the earliest observation j inside [t_i - window, t_i).
    If none is inside the window (e.g. after an occlusion gap), use the previous
    observation. The first observation looks forward instead. A single observation has
    no speed (None).
    """
    n = len(ts)
    out: list[float | None] = []
    for i in range(n):
        if i > 0:
            j = i
            while j > 0 and ts[i] - ts[j - 1] <= window_s + 1e-9:
                j -= 1
            if j == i:
                j = i - 1
            a, b = j, i
        elif n > 1:
            k = 1
            while k + 1 < n and ts[k + 1] - ts[0] <= window_s + 1e-9:
                k += 1
            a, b = 0, k
        else:
            out.append(None)
            continue
        dt = ts[b] - ts[a]
        out.append(math.hypot(xs[b] - xs[a], ys[b] - ys[a]) / dt)
    return out
