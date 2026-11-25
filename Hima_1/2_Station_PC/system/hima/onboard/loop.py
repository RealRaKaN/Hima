"""Onboard loop: frames arrive one at a time; the analysis is refreshed every
[onboard].update_s seconds of MISSION time.

Each refresh re-runs the tested batch analysis (analyze_frames) on all frames received so
far. There is no second, incremental algorithm that could drift from the tested one: the
snapshot at time t is, by construction, exactly what `analyze` gives for the session cut at
t, and the analysis at landing is exactly the batch analysis of the whole flight. The cost
of this choice (it grows with the number of frames) was measured during development.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from hima.analytics.analyze import analyze_frames
from hima.config import Config

EPS = 1e-9


@dataclass(frozen=True)
class Snapshot:
    t: float                         # mission time of the last frame included
    frames: int                      # frames included
    compute_s: float                 # wall time of this refresh
    statuses: dict[int, str]         # track_id -> status at this moment
    analysis_sha256: str             # fingerprint of the full analysis (for causality checks)


@dataclass(frozen=True)
class StatusChange:
    t: float
    track_id: int
    old: str | None                  # None: the track appears for the first time
    new: str


def _fingerprint(analysis: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(analysis, sort_keys=True).encode()).hexdigest()


class OnboardLoop:
    def __init__(self, meta: dict[str, Any], cfg: Config, *,
                 clock: Callable[[], float] = time.perf_counter,
                 on_refresh: Callable[[Snapshot, dict[str, Any], dict[str, Any]], None] | None = None):
        """on_refresh(snapshot, analysis, last_frame) runs after every refresh (e.g. alerts)."""
        self.meta, self.cfg, self._clock, self._on_refresh = meta, cfg, clock, on_refresh
        self.frames: list[dict[str, Any]] = []
        self.snapshots: list[Snapshot] = []
        self.changes: list[StatusChange] = []
        self.latest: dict[str, Any] | None = None     # full analysis of the latest refresh
        self._next_update: float | None = None
        self._landed = False

    def push(self, frame: dict[str, Any]) -> Snapshot | None:
        """Add one frame. Returns the new Snapshot when this frame triggers a refresh."""
        if self._landed:
            raise ValueError("the loop has landed; no more frames")
        t = frame["t"]
        if self.frames and t <= self.frames[-1]["t"]:
            raise ValueError(f"frame t = {t} is not after the previous frame t = {self.frames[-1]['t']}")
        self.frames.append(frame)
        if self._next_update is None:
            self._next_update = t + self.cfg.onboard.update_s
            return None
        if t < self._next_update - EPS:
            return None
        while self._next_update <= t + EPS:              # skip schedule slots lost in a gap
            self._next_update += self.cfg.onboard.update_s
        return self._refresh()

    def land(self) -> dict[str, Any]:
        """End of flight: the final analysis over every frame (equal to batch analyze)."""
        if not self.frames:
            raise ValueError("no frames received")
        if not self._landed:
            if not self.snapshots or self.snapshots[-1].frames != len(self.frames):
                self._refresh()
            self._landed = True
        return self.latest

    def _refresh(self) -> Snapshot:
        t0 = self._clock()
        analysis = analyze_frames(self.meta, self.frames, self.cfg)
        compute_s = self._clock() - t0
        statuses = {a["track_id"]: a["status"] for a in analysis["animals"]}
        before = self.snapshots[-1].statuses if self.snapshots else {}
        t = self.frames[-1]["t"]
        for tid in sorted(statuses):
            if before.get(tid) != statuses[tid]:
                self.changes.append(StatusChange(t, tid, before.get(tid), statuses[tid]))
        snap = Snapshot(t, len(self.frames), compute_s, statuses, _fingerprint(analysis))
        self.snapshots.append(snap)
        self.latest = analysis
        if self._on_refresh is not None:
            self._on_refresh(snap, analysis, self.frames[-1])
        return snap


@dataclass
class ReplayResult:
    loop: OnboardLoop
    final: dict[str, Any]
    lag_s: list[float] = field(default_factory=list)    # per refresh: wall time behind schedule


def replay(doc: dict[str, Any], cfg: Config, *, speed: float | None = None,
           clock: Callable[[], float] = time.perf_counter,
           sleep: Callable[[float], None] = time.sleep,
           on_refresh: Callable[[Snapshot, dict[str, Any], dict[str, Any]], None] | None = None,
           on_frame: Callable[[dict[str, Any]], None] | None = None,
           ) -> ReplayResult:
    """Feed a recorded session through the loop.

    speed None (or 0): as fast as possible. speed 1.0: frames are released at their mission
    time (real time); 10.0: ten times faster. In paced mode lag_s says how far behind its
    release time each refresh finished; a growing lag means the loop cannot keep up.
    """
    if speed is not None and speed < 0:
        raise ValueError("speed must be >= 0")
    frames: Iterable[dict[str, Any]] = doc["frames"]
    loop = OnboardLoop(doc["meta"], cfg, clock=clock, on_refresh=on_refresh)
    res = ReplayResult(loop, {})
    paced = bool(speed)
    start_wall = clock()
    t_first = doc["frames"][0]["t"] if doc["frames"] else 0.0
    for f in frames:
        if paced:
            release = start_wall + (f["t"] - t_first) / speed
            wait = release - clock()
            if wait > 0:
                sleep(wait)
        snap = loop.push(f)
        if on_frame is not None:             # after the frame (and its refresh, if any)
            on_frame(f)
        if snap is not None and paced:
            res.lag_s.append(max(0.0, clock() - (start_wall + (f["t"] - t_first) / speed)))
    res.final = loop.land()
    return res


def time_to_stable(loop: OnboardLoop) -> dict[int, float | None]:
    """Per track: mission time from which every later snapshot shows the final status."""
    if not loop.snapshots:
        return {}
    final = loop.snapshots[-1].statuses
    out: dict[int, float | None] = {}
    for tid, status in final.items():
        since = None
        for s in loop.snapshots:
            if s.statuses.get(tid) == status:
                if since is None:
                    since = s.t
            else:
                since = None
        out[tid] = since
    return out
