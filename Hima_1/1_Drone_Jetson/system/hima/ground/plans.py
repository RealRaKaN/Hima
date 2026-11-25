"""Route offers at the station: the drone's proposed route before take-off, the station's answer, the result.

One folder per plan in [route_review].plans_dir (station_inbox/plans/<plan_id>/):
  offer.json    the drone's route (hima.route_offer/1): written by the receiver (Wi-Fi) or the radio bridge (radio)
  answer.json   the station's answer (hima.route_answer/1): written by the dashboard; "approve" (fly the drone's
                route now, no more waiting) or "edit" with the new list of points
  result.json   what the drone flies (hima.route_result/1): from the drone (Wi-Fi) or the radio bridge
Files are written whole (temporary file + rename), so a reader never sees half a file. These folders contain points
near the reserve boundary (WDPA terms: never redistribute): they stay on the station, not in git.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hima.contract.io import write_json_atomic

PLAN_ID = re.compile(r"^plan-\d{8}T\d{6}Z$")
OFFER, ANSWER, RESULT = "offer.json", "answer.json", "result.json"
DECISIONS = ("approve", "edit")
MAX_POINTS = 200


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_id(plan_id: Any) -> bool:
    return isinstance(plan_id, str) and bool(PLAN_ID.match(plan_id))


def _points(v: Any) -> str | None:
    if not isinstance(v, list) or not 1 <= len(v) <= MAX_POINTS:
        return f"points: a list of 1..{MAX_POINTS} [lat, lon]"
    for p in v:
        if not (isinstance(p, list) and len(p) == 2 and all(isinstance(c, (int, float)) for c in p)
                and -90 <= p[0] <= 90 and -180 <= p[1] <= 180):
            return f"bad point {p!r}: [lat, lon] in degrees"
    return None


def check_offer(doc: Any) -> str | None:
    if not isinstance(doc, dict) or doc.get("schema_version") != "hima.route_offer/1":
        return "not a hima.route_offer/1"
    if not valid_id(doc.get("plan_id")):
        return "plan_id missing or not plan-YYYYMMDDTHHMMSSZ"
    err = _points(doc.get("targets"))
    if err:
        return "targets " + err
    if doc.get("fence") is not None and _points(doc["fence"]) and doc["fence"] != []:
        return "fence: a list of [lat, lon]"
    la = doc.get("launch") or {}
    if not all(isinstance(la.get(k), (int, float)) for k in ("lat_deg", "lon_deg")):
        return "launch lat_deg / lon_deg missing"
    return None


def check_answer(doc: Any) -> str | None:
    if not isinstance(doc, dict) or not valid_id(doc.get("plan_id")) or doc.get("decision") not in DECISIONS:
        return "an answer needs plan_id and decision approve | edit"
    if doc["decision"] == "edit":
        err = _points(doc.get("targets"))
        if err:
            return "targets " + err
    return None


class PlanStore:
    def __init__(self, folder: str | Path):
        self.root = Path(folder)

    def dir(self, plan_id: str) -> Path:
        if not valid_id(plan_id):
            raise ValueError(f"not a plan id: {plan_id!r}")
        return self.root / plan_id

    def _read(self, plan_id: str, name: str) -> dict[str, Any] | None:
        p = self.dir(plan_id) / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save_offer(self, doc: dict[str, Any], source: str) -> dict[str, Any]:
        """Keeps the first offer of a plan (the Wi-Fi one has the fence; the radio text may come too)."""
        d = self.dir(doc["plan_id"])
        d.mkdir(parents=True, exist_ok=True)
        old = self.offer(doc["plan_id"])
        if old is not None:
            if source not in old.get("seen_via", []):
                old["seen_via"] = old.get("seen_via", []) + [source]
                write_json_atomic(old, d / OFFER)
            return old
        doc = {**doc, "source": source, "seen_via": [source], "received_utc": now_utc()}
        write_json_atomic(doc, d / OFFER)
        return doc

    def offer(self, plan_id: str) -> dict[str, Any] | None:
        return self._read(plan_id, OFFER)

    def answer(self, plan_id: str) -> dict[str, Any] | None:
        return self._read(plan_id, ANSWER)

    def result(self, plan_id: str) -> dict[str, Any] | None:
        return self._read(plan_id, RESULT)

    def write_answer(self, plan_id: str, decision: str, targets: list[list[float]] | None = None,
                     by: str = "dashboard") -> dict[str, Any]:
        """The first answer counts (the drone may already have read it); a second one is refused."""
        if self.answer(plan_id) is not None:
            raise FileExistsError(f"{plan_id} already has an answer")
        doc = {"schema_version": "hima.route_answer/1", "plan_id": plan_id, "decision": decision, "by": by,
               "utc": now_utc()}
        if decision == "edit":
            doc["targets"] = [[round(float(a), 7), round(float(b), 7)] for a, b in targets or []]
        err = check_answer(doc)
        if err:
            raise ValueError(err)
        write_json_atomic(doc, self.dir(plan_id) / ANSWER)
        return doc

    def write_result(self, plan_id: str, doc: dict[str, Any]) -> None:
        write_json_atomic({**doc, "schema_version": "hima.route_result/1", "plan_id": plan_id,
                           "received_utc": now_utc()}, self.dir(plan_id) / RESULT)

    def ids(self) -> list[str]:
        """Newest first."""
        if not self.root.is_dir():
            return []
        return sorted((p.name for p in self.root.iterdir() if p.is_dir() and valid_id(p.name)
                       and (p / OFFER).is_file()), reverse=True)
