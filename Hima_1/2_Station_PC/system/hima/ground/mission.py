"""One archived mission for the ground dashboard's Mission page. Reads only that mission's folder in
the archive (package.json, report.json, session.json, stored media) and computes nothing new:
- the animals table is the report's summary (the facts the landing report was checked against);
- the event timeline, herd and map come from the archived session through hima.dashboard.data;
- a stored event clip is shown as its middle frame (clips are written with OpenCV's mp4v, which
  browsers do not play), with the boxes of that frame from the archived session.
- When the package has a flight log, what happened in flight (alerts, vehicle alerts,
  camera windows, periodic analyses, by mission time with their link delivery time), the movement
  summary per track and the herd path, and the onboard images. All read from flight_log.json as written
  on the drone; nothing is recomputed here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from hima.contract.package import MANIFEST
from hima.ground.archive import REPORT_FILE, SESSION_FILE

MEDIA_KINDS = ("event_clip", "uncertain_detection")


# ---- flight log ----------------------------------------------------------------------------------

def flight_log(folder: str | Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    it = next((i for i in manifest["items"] if i["kind"] == "flight_log"), None)
    if it is None or it["state"] != "stored":
        return None
    return json.loads((Path(folder) / it["file"]["path"]).read_text(encoding="utf-8"))


def flight_event_rows(log: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything sent from the drone in flight, by mission time: animal alerts, vehicle alerts, camera-window
    results and periodic analyses, each with when its last part reached the ground (None = never)."""
    single: dict[str, float] = {}
    by_msg: dict[int, float] = {}
    for d in log["link"]["deliveries"]:
        if d["msg_id"]:
            by_msg[d["msg_id"]] = max(by_msg.get(d["msg_id"], 0.0), d["delivered_t"])
        else:
            single.setdefault(d["text"], d["delivered_t"])
    rows = [{"t": a["t"], "what": "animal alert", "text": a["text"], "delivered_t": single.get(a["text"])}
            for a in log["alerts"]]
    rows += [{"t": a["t"], "what": "vehicle alert", "text": a["text"], "delivered_t": single.get(a["text"])}
             for a in (log.get("objects") or {}).get("alerts", [])]
    for tx in log.get("texts", []):
        what = {"window": "AI camera window", "periodic": "periodic analysis"}.get(tx["source"], tx["source"])
        rows.append({"t": tx["release_t"], "what": what, "text": tx["text"],
                     "delivered_t": by_msg.get(tx["msg_id"]) if tx["msg_id"] else single.get(tx["text"])})
    return sorted(rows, key=lambda r: (r["t"], r["what"]))


def window_rows(log: dict[str, Any]) -> list[dict[str, Any]]:
    w = log.get("windows") or {}
    rows = [{"window": e["window"], "trigger": e["trigger"]["kind"], "alert": e["trigger"]["alert"],
             "opened_t": e["opened_t"], "closed_t": e["closed_t"], "images": e["images"], "model": e["model"],
             "latency_s": e["latency_s"], "verdict": e["verdict"], "sent": " | ".join(e["sent"]),
             "note_rejected": "; ".join(e["note_errors"]) or "-"} for e in w.get("access_log", [])]
    rows += [{"window": None, "trigger": r["kind"], "alert": r["alert"], "opened_t": r["t"], "closed_t": None,
              "images": 0, "model": None, "latency_s": None, "verdict": "refused: " + r["why"], "sent": "-",
              "note_rejected": "-"} for r in w.get("refused", [])]
    return sorted(rows, key=lambda r: r["opened_t"])


def movement_rows(log: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per animal: where it was found. Older packages have start and end instead."""
    out = []
    for t in (log.get("movement") or {}).get("tracks", []):
        s = t.get("sighting") or t.get("start")
        found = None
        if s and s.get("x_m") is not None:
            found = f"({s['x_m']:.0f}, {s['y_m']:.0f}) m at {s['t']:g} s"
        gps = f"{s['lat_deg']:.6f}, {s['lon_deg']:.6f}" if s and s.get("lat_deg") is not None else None
        out.append({"track_id": t["track_id"], "species": t["species"], "status": t["status"],
                    "found": found, "gps": gps, "distance_m": t.get("distance_travelled_m"),
                    "mean_speed_mps": t.get("mean_ground_speed_mps"),
                    **{f"{k}_s": v for k, v in (t.get("ethogram_s") or {}).items()}})
    return out


def flight_image_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": it["id"], "purpose": it["purpose"], "t": it["sample_t"], "window": it["window"],
             "alert": it["alert"], "state": it["state"], "file": it["file"]["path"] if it["file"] else None}
            for it in manifest["items"] if it["kind"] == "flight_image"]


def mission_files(archive: str | Path, mission_id: str) -> dict[str, Any]:
    folder = Path(archive) / mission_id
    return {"folder": folder,
            "manifest": json.loads((folder / MANIFEST).read_text(encoding="utf-8")),
            "report": json.loads((folder / REPORT_FILE).read_text(encoding="utf-8")),
            "session_path": folder / SESSION_FILE}


def findings_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"track_id": f["track_id"], "status": f["status"], "note": f["note"]}
            for f in report["report"].get("findings", [])]


def animals_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"track_id": a["track_id"], "species": a["species"], "status": a["status"],
             "visible_s": a["visible_s"], "first_seen_s": a["first_seen_s"], "last_seen_s": a["last_seen_s"],
             "events": ", ".join(f"{k} x{v}" for k, v in sorted(a["events"].items())) or "-",
             "min_distance_to_drone_m": a["min_distance_to_drone_m"]}
            for a in report["summary"]["animals"]]


def media_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Event clips and uncertain stills, in package order, with their state (stored / no_media / dropped)."""
    out = []
    for it in manifest["items"]:
        if it["kind"] not in MEDIA_KINDS:
            continue
        out.append({"id": it["id"], "kind": it["kind"], "state": it["state"],
                    "t_start": it["t_start"], "t_end": it["t_end"],
                    "tracks": " ".join(map(str, it["track_ids"])) or "-",
                    "why": it["status"] if it["kind"] == "event_clip" else it["criterion"],
                    "frame": (it["frame_first"] + it["frame_last"]) // 2
                    if it["kind"] == "event_clip" and it["frame_first"] is not None else it.get("sample_frame"),
                    "file": it["file"]["path"] if it["file"] else None})
    return out


def clip_middle_frame(path: str | Path) -> tuple[np.ndarray, int] | None:
    """(RGB image, index within the clip) of the clip's middle frame, read in order; None if unreadable."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        frames = []
        while True:
            ok, img = cap.read()
            if not ok:
                break
            frames.append(img)
        if not frames:
            return None
        k = (len(frames) - 1) // 2
        return cv2.cvtColor(frames[k], cv2.COLOR_BGR2RGB), k
    finally:
        cap.release()


def draw_boxes(rgb: np.ndarray, boxes: list[tuple[float, float, float, float]], color=(255, 0, 0)) -> np.ndarray:
    """Copy of the image with centre-x, centre-y, width, height boxes drawn (px)."""
    out = rgb.copy()
    for cx, cy, w, h in boxes:
        cv2.rectangle(out, (int(cx - w / 2), int(cy - h / 2)), (int(cx + w / 2), int(cy + h / 2)), color, 2)
    return out
