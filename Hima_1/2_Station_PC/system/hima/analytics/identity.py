"""Which boxes are one animal. Fixed rules, no LLM.

The tracker (ByteTrack) gives every box a track id. Simulated flights showed three ways those ids mislead:
  - a box for one frame (a rock, a shadow) became a "neighbour" for 60 s and broke the injury stages;
  - a second box on part of an animal (head, legs) inside its body box became a new track;
  - an animal the tracker lost (it ran, or it went under a tree) came back under a new id, so one animal
    had many tracks, alerts repeated, and the time on the first track stopped.

The rules, applied frame by frame in time order (each decision uses only what was seen up to that frame, so the
analysis cut at time t gives the same animals as the live flight at time t):
  1. Part box: a box that lies at least part_inside of its area inside a bigger box of the same frame and the
     same species, and has at most part_area_ratio of that box's area, is a part of that animal and is dropped.
     (Same species only: from straight above a gazelle cannot stand inside an oryx. A calf lying against its
     mother can be dropped this way: it is not alone, so no young or injury rule would flag it anyway.)
  2. Same tracker id: the box continues the animal (or candidate) its id feeds.
  3. New tracker id: a new candidate. It is registered once it has been seen for confirm_s (seconds of sighting
     in a row: two sightings count as continuous when no frame between them was missed or they are at most
     confirm_gap_s apart). A box seen for less is never an animal: it is not counted, it is nobody's neighbour,
     it raises no alert, it is not on the map, and it cannot continue another animal.
  4. At that moment it may continue an animal that was lost before it appeared: a registered animal not seen
     since before the candidate's first box, for at most link_gap_s, with the same species, last seen close
     enough to the candidate's first box: within link_base_m + link_speed_margin x speed x gap (speed = its last
     speed, at least activity.walk_min_mps), never farther than link_max_m. Of several, the closest relative to
     its allowed distance. The candidate's boxes then belong to that animal (its first second too).
     Without camera parameters there is no distance, so a candidate is always a new animal.
  5. An animal's id is the tracker id of its first box (if that id is taken, an id from NEW_ID_BASE up: the
     tracker never gets that far).

The point where an animal was registered (its "sighting") is what the map shows (no animal paths).
Limits: an animal that comes back far from where it was lost, after more than link_gap_s, or with the other
species label, is counted again. Two animals that vanish side by side may swap ids when they come back. A false
box that stays for a second or more (a rock the detector keeps seeing) is still an animal.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from hima.analytics.geometry import Camera, DronePose, pixel_to_ground

NEW_ID_BASE = 1_000_000
EPS = 1e-9


@dataclass
class Animal:
    """One animal (or a candidate not yet registered): one or more tracker segments joined by the rules above."""
    raw_first: int
    driver: int                                  # tracker id that feeds it now
    species_votes: dict[str, int] = field(default_factory=dict)
    first_k: int = -1
    first_t: float = 0.0
    first_xy: tuple[float, float] | None = None
    last_k: int = -1
    last_t: float = 0.0
    last_xy: tuple[float, float] | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=64))    # (t, x, y) for the speed
    seen_s: float = 0.0
    sighting: dict[str, Any] | None = None       # where and when it was registered
    segments: list[list] = field(default_factory=list)   # [tracker id, first t, last t]
    animal_id: int | None = None                 # set when registered
    owner: "Animal | None" = None                # a candidate that continued another animal

    @property
    def species(self) -> str:
        return max(self.species_votes, key=lambda s: self.species_votes[s])

    def root(self) -> "Animal":
        a = self
        while a.owner is not None:
            a = a.owner
        return a

    def speed(self, window_s: float) -> float:
        if len(self.recent) < 2:
            return 0.0
        t1, x1, y1 = self.recent[-1]
        old = next(((t, x, y) for t, x, y in self.recent if t1 - t <= window_s + EPS), self.recent[0])
        if t1 - old[0] <= EPS:
            return 0.0
        return math.hypot(x1 - old[1], y1 - old[2]) / (t1 - old[0])


def _area(b) -> float:
    return max(0.0, b[2]) * max(0.0, b[3])


def _inside_share(small, big) -> float:
    """Share of the small box's area that lies inside the big box (boxes are centre x, centre y, w, h)."""
    ax0, ay0 = small[0] - small[2] / 2, small[1] - small[3] / 2
    ax1, ay1 = small[0] + small[2] / 2, small[1] + small[3] / 2
    bx0, by0 = big[0] - big[2] / 2, big[1] - big[3] / 2
    bx1, by1 = big[0] + big[2] / 2, big[1] + big[3] / 2
    iw, ih = max(0.0, min(ax1, bx1) - max(ax0, bx0)), max(0.0, min(ay1, by1) - max(ay0, by0))
    a = _area(small)
    return iw * ih / a if a > 0 else 0.0


def part_boxes(dets: list[dict[str, Any]], inside: float, area_ratio: float) -> set[int]:
    """Indexes of the boxes that are parts of a bigger box of the same frame (rule 1)."""
    out = set()
    for i, d in enumerate(dets):
        a = _area(d["bbox_xywh_px"])
        for j, e in enumerate(dets):
            if i == j:
                continue
            b = _area(e["bbox_xywh_px"])
            if (e["species"] == d["species"] and b > a and a <= area_ratio * b + EPS
                    and _inside_share(d["bbox_xywh_px"], e["bbox_xywh_px"]) >= inside - EPS):
                out.add(i)
                break
    return out


class Resolver:
    """Feed frames in time order with push(); live_detections() gives the registered animals of the last frame."""

    def __init__(self, meta: dict[str, Any], cfg):
        self.cfg = cfg
        self.idc = cfg.identity
        self.metric = bool(meta["camera"]["params_known"])
        self.cam = Camera.from_meta(meta["camera"])
        self.animals: list[Animal] = []
        self.by_raw: dict[int, Animal] = {}         # tracker id -> the animal or candidate it feeds
        self.used_ids: set[int] = set()
        self._next_new = NEW_ID_BASE
        self.k = -1
        self.rows: list[list[tuple[Animal, dict[str, Any]]]] = []    # per frame: (animal or candidate, detection)
        self.parts: list[int] = []                                    # part boxes dropped, per frame
        self.links = 0                                                # candidates that continued a lost animal

    def _ground(self, d: dict[str, Any], pose: DronePose | None) -> tuple[float, float] | None:
        if pose is None:
            return None
        g = pixel_to_ground(d["bbox_xywh_px"][0], d["bbox_xywh_px"][1], self.cam, pose)
        return g.x_m, g.y_m

    def push(self, frame: dict[str, Any]) -> None:
        self.k += 1
        k, t = self.k, frame["t"]
        pose = DronePose.from_frame(frame["drone"]) if self.metric else None
        dets = frame["detections"]
        drop = part_boxes(dets, self.idc.part_inside, self.idc.part_area_ratio)
        self.parts.append(len(drop))
        row: list[tuple[Animal, dict[str, Any]]] = []
        for i, d in enumerate(dets):
            if i in drop:
                continue
            rid = d["track_id"]
            an = self.by_raw.get(rid)
            if an is None or an.root().driver != rid:        # a new tracker id, or one whose animal another id took
                an = Animal(raw_first=rid, driver=rid)
                self.animals.append(an)
                self.by_raw[rid] = an
            row.append((an, d))
        for an, d in row:
            self._observe(an.root(), k, t, d, self._ground(d, pose))
        self.rows.append(row)

    def _observe(self, an: Animal, k: int, t: float, d: dict[str, Any], xy) -> None:
        c = self.idc
        if an.first_k < 0:
            an.first_k, an.first_t, an.first_xy = k, t, xy
        elif k == an.last_k + 1 or t - an.last_t <= c.confirm_gap_s + EPS:
            an.seen_s += t - an.last_t
        if not an.segments or an.segments[-1][0] != d["track_id"]:
            an.segments.append([d["track_id"], t, t])
        else:
            an.segments[-1][2] = t
        an.species_votes[d["species"]] = an.species_votes.get(d["species"], 0) + 1
        an.last_k, an.last_t = k, t
        if xy is not None:
            an.last_xy = xy
            an.recent.append((t, xy[0], xy[1]))
        if an.animal_id is None and an.seen_s >= c.confirm_s - EPS:
            self._register(an, t, xy)

    def _register(self, cand: Animal, t: float, xy) -> None:
        host = self._lost_animal(cand) if self.metric else None
        if host is not None:                                   # rule 4: it continues a lost animal
            host.segments += cand.segments
            for sp, n in cand.species_votes.items():
                host.species_votes[sp] = host.species_votes.get(sp, 0) + n
            host.seen_s += cand.seen_s
            host.last_k, host.last_t, host.last_xy = cand.last_k, cand.last_t, cand.last_xy
            host.recent.extend(cand.recent)
            host.driver = cand.driver
            cand.owner = host
            self.by_raw[cand.driver] = host
            self.links += 1
            return
        aid = cand.raw_first
        if aid in self.used_ids:
            aid = self._next_new
            self._next_new += 1
        self.used_ids.add(aid)
        cand.animal_id = aid
        cand.sighting = {"t": round(t, 4), "x_m": None if xy is None else round(xy[0], 3),
                         "y_m": None if xy is None else round(xy[1], 3)}

    def _lost_animal(self, cand: Animal) -> Animal | None:
        if cand.first_xy is None:
            return None
        c = self.idc
        sp = cand.species
        best, best_score = None, None
        for an in self.animals:
            if an.animal_id is None or an.owner is not None or an.last_xy is None or an.species != sp:
                continue
            gap = cand.first_t - an.last_t
            if gap <= EPS or gap > c.link_gap_s + EPS:              # seen while the candidate was there: not lost
                continue
            v = max(an.speed(self.cfg.geometry.speed_window_s), self.cfg.activity.walk_min_mps)
            allowed = min(c.link_max_m, c.link_base_m + c.link_speed_margin * v * gap)
            dist = math.hypot(cand.first_xy[0] - an.last_xy[0], cand.first_xy[1] - an.last_xy[1])
            if dist > allowed + EPS:
                continue
            score = dist / allowed
            if best_score is None or score < best_score:
                best, best_score = an, score
        return best

    def _label(self, row) -> list[dict[str, Any]]:
        out = []
        for an, d in row:
            r = an.root()
            if r.animal_id is not None:
                out.append({**d, "track_id": r.animal_id})
        return out

    def live_detections(self) -> list[dict[str, Any]]:
        """Detections of the last frame that belong to registered animals, with the animal id as track_id."""
        return self._label(self.rows[-1]) if self.rows else []

    def resolved_frames(self, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The frames pushed so far with only the boxes of registered animals, track_id = the animal id."""
        return [{**f, "detections": self._label(row)} for f, row in zip(frames, self.rows)]

    def registered(self) -> dict[int, Animal]:
        return {an.animal_id: an for an in self.animals if an.animal_id is not None}

    def summary(self) -> dict[str, Any]:
        return {"tracker_ids": len(self.by_raw), "animals": len(self.registered()),
                "not_registered": sum(1 for an in self.animals if an.animal_id is None and an.owner is None),
                "part_boxes_dropped": sum(self.parts)}


def resolve(meta: dict[str, Any], frames: list[dict[str, Any]], cfg) -> tuple[list[dict[str, Any]], Resolver]:
    r = Resolver(meta, cfg)
    for f in frames:
        r.push(f)
    return r.resolved_frames(frames), r
