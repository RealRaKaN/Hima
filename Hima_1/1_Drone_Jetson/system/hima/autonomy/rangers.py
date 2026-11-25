"""The reserve's own vehicles near a herd (field work) must not be followed as poachers.

Two independent signs, either is enough:
  1. Planned field work. Before take-off the station enters where and when the rangers work (a circle and a UTC
     window); the file goes to the Jetson with the plan (reserve.field_ops / rangers.field_ops). A vehicle inside an
     active circle is the reserve's.
  2. A roof marker. Every reserve vehicle carries an ArUco marker (OpenCV aruco, rangers.marker_dict) on its roof,
     about 1.2 x 1.2 m: at 60 m with the 24 deg lens at 1280 px a pixel is about 2 cm, so the marker is about
     60 px wide, enough for its 6 x 6 cells. A marker id in rangers.marker_ids inside the vehicle box makes it ours.

Neither sign: the vehicle is unknown, and near a herd it is followed. The station can still clear it after the
flight (the vehicle alert and its image are in the package). A poacher could copy a marker; the field-work list,
the marker id per vehicle and the station's own radio contact with its teams are the checks against that.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from hima.autonomy.history import HistoryError, _check_latlon
from hima.config import Config
from hima.contract.io import write_json_atomic

SCHEMA = "hima.field_ops/1"


def _utc(s: str) -> dt.datetime:
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError as e:
        raise HistoryError(f"bad UTC time {s!r} (want YYYY-MM-DDTHH:MM:SSZ)") from e
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def load_ops(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("ops"), list):
        raise HistoryError(f"{p} is not a {SCHEMA} file")
    return doc["ops"]


def add_op(path: str | Path, *, lat: float, lon: float, radius_m: float, start: str, end: str, note: str) -> dict:
    la, lo = _check_latlon(lat, lon)
    a, b = _utc(start), _utc(end)
    if not b > a:
        raise HistoryError("the field work must end after it starts")
    if not radius_m > 0:
        raise HistoryError("radius_m must be > 0")
    ops = load_ops(path)
    item = {"lat": la, "lon": lo, "radius_m": float(radius_m), "start_utc": a.isoformat().replace("+00:00", "Z"),
            "end_utc": b.isoformat().replace("+00:00", "Z"), "note": note}
    ops.append(item)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic({"schema": SCHEMA, "ops": ops}, path)
    return item


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def field_op_at(ops: list[dict[str, Any]], lat: float, lon: float, when: dt.datetime) -> dict | None:
    for op in ops:
        if _utc(op["start_utc"]) <= when <= _utc(op["end_utc"]) and \
                haversine_m((lat, lon), (op["lat"], op["lon"])) <= op["radius_m"]:
            return op
    return None


class MarkerReader:
    """Reserve roof markers in a vehicle box."""

    def __init__(self, cfg: Config):
        import cv2
        self.cv2 = cv2
        self.ids = {int(x) for x in cfg.rangers.marker_ids.split(",") if x.strip()}
        self.pad = cfg.rangers.marker_pad
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.rangers.marker_dict))
        self.detector = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())

    def find(self, image: np.ndarray, bbox_xywh: tuple[float, float, float, float]) -> list[int]:
        """Marker ids of the reserve inside the (padded) box; all ids seen there if none is ours: []."""
        h, w = image.shape[:2]
        cx, cy, bw, bh = bbox_xywh
        bw, bh = bw * (1 + self.pad), bh * (1 + self.pad)
        x0, x1 = max(0, int(cx - bw / 2)), min(w, int(math.ceil(cx + bw / 2)))
        y0, y1 = max(0, int(cy - bh / 2)), min(h, int(math.ceil(cy + bh / 2)))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return []
        crop = image[y0:y1, x0:x1]
        gray = crop if crop.ndim == 2 else self.cv2.cvtColor(crop, self.cv2.COLOR_BGR2GRAY)
        _, ids, _ = self.detector.detectMarkers(gray)
        if ids is None:
            return []
        return sorted({int(i) for i in ids.flatten()} & self.ids)


def owner(cfg: Config, ops: list[dict[str, Any]], markers: MarkerReader | None, image: np.ndarray | None,
          bbox_xywh, lat: float | None, lon: float | None, when: dt.datetime) -> str | None:
    """Why this vehicle is the reserve's, or None."""
    if markers is not None and image is not None:
        found = markers.find(image, bbox_xywh)
        if found:
            return f"MARK{found[0]}"
    if lat is not None and lon is not None:
        op = field_op_at(ops, lat, lon, when)
        if op is not None:
            return "OPS " + (op.get("note") or "")[:12]
    return None
