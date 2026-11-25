"""FlightMissionRecorder: collects raw observations frame by frame and writes one session file.

Any source (video now; Webots or a real flight later) calls add_frame(t, drone, detections)
with RAW data only: drone pose and tracked pixel boxes. Nothing computed goes in here.
Problems are caught when the frame is added, with a clear message, instead of producing
a file that fails validation at the end.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hima.contract.io import save_session, validate_session


@dataclass(frozen=True)
class RawDetection:
    track_id: int | None          # None = detector saw it but the tracker gave no id
    class_raw: str
    conf: float
    bbox_xywh_px: tuple[float, float, float, float]     # centre x, centre y, width, height


class FlightMissionRecorder:
    def __init__(self, *, mission_id: str, source: str, camera: dict[str, Any],
                 detector: dict[str, Any], geometry_assumptions: tuple[str, ...] = ("flat_ground", "nadir_camera"),
                 created_at: datetime | None = None):
        created_at = created_at or datetime.now(timezone.utc)
        self.meta = {
            "schema_version": "hima.session/1", "mission_id": mission_id, "source": source,
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "camera": dict(camera), "detector": dict(detector),
            "geometry_assumptions": list(geometry_assumptions),
        }
        self._class_map: dict[str, str] = dict(detector["class_map"])
        self._w, self._h = camera["width_px"], camera["height_px"]
        self.frames: list[dict[str, Any]] = []
        self.skipped_untracked = 0          # detections without a track id (not written)
        # ... but kept in memory: the session contract has no place for them, the triage
        # step (onboard/triage.py) turns them into "uncertain" items for the ground station.
        self.untracked: list[tuple[float, RawDetection]] = []

    def add_frame(self, t: float, drone: dict[str, float], detections: list[RawDetection]) -> None:
        if self.frames and t <= self.frames[-1]["t"]:
            raise ValueError(f"frame time {t} is not after the previous frame ({self.frames[-1]['t']})")
        seen: set[int] = set()
        out, untracked = [], []
        for d in detections:
            if d.track_id is None:
                untracked.append((float(t), d))
                continue
            if d.track_id in seen:
                raise ValueError(f"t={t}: track id {d.track_id} appears twice in one frame")
            if d.class_raw not in self._class_map:
                raise ValueError(f"t={t}: class {d.class_raw!r} is not in the class_map "
                                 f"{sorted(self._class_map)}")
            cx, cy, w, h = (float(v) for v in d.bbox_xywh_px)
            if not (0 <= cx <= self._w and 0 <= cy <= self._h):
                raise ValueError(f"t={t}: box centre ({cx}, {cy}) is outside the {self._w}x{self._h} image")
            seen.add(d.track_id)
            out.append({"track_id": int(d.track_id), "species": self._class_map[d.class_raw],
                        "class_raw": d.class_raw, "conf": round(float(d.conf), 4),
                        "bbox_xywh_px": [round(cx, 2), round(cy, 2), round(w, 2), round(h, 2)]})
        self.frames.append({"t": float(t), "drone": {k: float(drone[k]) for k in
                                                       ("x_m", "y_m", "agl_m", "heading_deg")},
                            "detections": out})
        self.skipped_untracked += len(untracked)       # only once the whole frame was accepted
        self.untracked.extend(untracked)

    def to_session(self) -> dict[str, Any]:
        return {"meta": self.meta, "frames": self.frames}

    def errors(self) -> list:
        return validate_session(self.to_session())

    def save(self, path: str | Path) -> None:
        """Validate, then write atomically (see contract.io.save_session)."""
        if not self.frames:
            raise ValueError("no frames recorded")
        save_session(self.to_session(), path)
