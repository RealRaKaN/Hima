"""The raw flight file: everything the drone saw and did, as it happened, with no analysis.

One JSON line per event, written and flushed at once (a crash loses at most the last line), into
<flight>/flight/raw.jsonl; at landing it is compressed into the mission package as raw_flight.jsonl.gz (priority 0:
never dropped by the storage budget). The analyses in the package (report, session analysis, flight log, the
no-position analysis) are made from the same frames; this file lets the ground check them or analyse again.

Lines ("k" = kind):
  start     the flight: mission id, camera, models, home, config SHA-256
  frame     one camera frame given to the model: wall time since the start, UTC, mission time t (null without the
            MAVLink clock), pose (null = no position from the Pixhawk), autopilot mode / armed / battery / GPS, and
            every animal box as [class, conf, cx, cy, w, h, track id or null]; "used" = what the frame was used for
            ("analysed", "low_agl", "no_position")
  objects   one sample of the general model: every box [class, group, conf, cx, cy, w, h, on_animal, kept]
  text      a message sent to the Pixhawk (alerts, autonomy, no position)
  event     no position / position back / the end
"""
from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path
from typing import Any

RAW_FILE = "raw.jsonl"
PACKAGE_FILE = "raw_flight.jsonl.gz"


def boxes(dets) -> list[list]:
    return [[d.class_raw, round(d.conf, 4), *[round(v, 1) for v in d.bbox_xywh_px], d.track_id] for d in dets]


class RawLog:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / RAW_FILE
        self._fh = open(self.path, "a", encoding="utf-8")
        self.lines = 0
        self.errors = 0

    def write(self, kind: str, **fields: Any) -> None:
        """Never raises: the raw file must not stop the flight (a full disk is counted, not fatal)."""
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps({"k": kind, **fields}, default=str, separators=(",", ":")) + "\n")
            self._fh.flush()
            self.lines += 1
        except (OSError, ValueError, TypeError):
            self.errors += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def compress_to(self, dest: Path) -> Path:
        self.close()
        with open(self.path, "rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        return dest

    def summary(self) -> dict[str, Any]:
        return {"file": RAW_FILE, "lines": self.lines, "write_errors": self.errors,
                "bytes": self.path.stat().st_size if self.path.is_file() else 0}
