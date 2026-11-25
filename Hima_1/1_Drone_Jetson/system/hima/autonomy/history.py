"""What past flights left, for the planner: their paths, the herds they saw, and poaching incidents.

One JSON file (reserve.history), kept on the Jetson and on the station:

    {"schema": "hima.reserve_history/1",
     "flights":   [{"mission_id", "date", "path": [[lat, lon], ...]}],
     "sightings": [{"mission_id", "date", "lat", "lon", "species"}],
     "incidents": [{"date", "lat", "lon", "kind", "note"}]}

Flights and sightings come from a session file after a flight (add_session); incidents are typed in by the
rangers (add_incident). Dates are UTC days, YYYY-MM-DD.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

from hima.contract.io import write_json_atomic
from hima.telemetry.geo import to_latlon

SCHEMA = "hima.reserve_history/1"
INCIDENT_KINDS = ("poaching", "vehicle", "carcass", "fence_cut", "other")


class HistoryError(ValueError):
    pass


def empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "flights": [], "sightings": [], "incidents": []}


def load(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return empty()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HistoryError(f"cannot read {p}: {e}") from e
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise HistoryError(f"{p} is not a {SCHEMA} file")
    for k in ("flights", "sightings", "incidents"):
        if not isinstance(doc.get(k), list):
            raise HistoryError(f"{p}: '{k}' must be a list")
    return doc


def save(doc: dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(doc, path)


def parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError as e:
        raise HistoryError(f"bad date {s!r} (want YYYY-MM-DD)") from e


def _check_latlon(lat: Any, lon: Any) -> tuple[float, float]:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError) as e:
        raise HistoryError(f"bad position {lat!r}, {lon!r}") from e
    if not (math.isfinite(la) and math.isfinite(lo) and -90 <= la <= 90 and -180 <= lo <= 180):
        raise HistoryError(f"bad position {lat!r}, {lon!r}")
    return la, lo


def add_incident(doc: dict[str, Any], *, date: str, lat: float, lon: float, kind: str, note: str = "") -> dict:
    if kind not in INCIDENT_KINDS:
        raise HistoryError(f"kind must be one of {', '.join(INCIDENT_KINDS)}")
    la, lo = _check_latlon(lat, lon)
    item = {"date": parse_date(date).isoformat(), "lat": la, "lon": lo, "kind": kind, "note": note}
    doc["incidents"].append(item)
    return item


def path_from_session(session: dict[str, Any], step_m: float = 50.0) -> list[list[float]]:
    """The drone's path as [lat, lon] points about step_m apart (needs meta.home)."""
    home = session.get("meta", {}).get("home")
    if not home:
        raise HistoryError("the session has no meta.home: its path cannot be placed on the map")
    lat0, lon0 = home["lat_deg"], home["lon_deg"]
    out: list[list[float]] = []
    last = None
    for fr in session.get("frames", []):
        d = fr.get("drone") or {}
        if "x_m" not in d or "y_m" not in d:
            continue
        xy = (d["x_m"], d["y_m"])
        if last is None or math.dist(last, xy) >= step_m:
            la, lo = to_latlon(xy[0], xy[1], lat0, lon0)
            out.append([round(la, 7), round(lo, 7)])
            last = xy
    return out


def add_session(doc: dict[str, Any], session: dict[str, Any], step_m: float = 50.0) -> dict:
    """Add one flown session: its path, and the place where each animal was registered."""
    meta = session.get("meta", {})
    mid = meta.get("mission_id")
    if not mid:
        raise HistoryError("the session has no mission_id")
    if any(f.get("mission_id") == mid for f in doc["flights"]):
        raise HistoryError(f"mission {mid} is already in the history")
    date = parse_date(meta.get("created_at", "")).isoformat()
    path = path_from_session(session, step_m)
    doc["flights"].append({"mission_id": mid, "date": date, "path": path})
    n = 0
    for a in (session.get("analysis") or {}).get("animals", []):
        s = a.get("sighting") or {}
        if s.get("lat_deg") is None or s.get("lon_deg") is None:
            continue
        doc["sightings"].append({"mission_id": mid, "date": date, "lat": s["lat_deg"], "lon": s["lon_deg"],
                                 "species": a.get("species")})
        n += 1
    return {"mission_id": mid, "date": date, "path_points": len(path), "sightings": n}


def add_plan_flown(doc: dict[str, Any], *, mission_id: str, date: str, path: list[list[float]]) -> dict:
    """A path known some other way (e.g. a flight log): [[lat, lon], ...]."""
    if any(f.get("mission_id") == mission_id for f in doc["flights"]):
        raise HistoryError(f"mission {mission_id} is already in the history")
    pts = [list(_check_latlon(a, b)) for a, b in path]
    item = {"mission_id": mission_id, "date": parse_date(date).isoformat(), "path": pts}
    doc["flights"].append(item)
    return item
