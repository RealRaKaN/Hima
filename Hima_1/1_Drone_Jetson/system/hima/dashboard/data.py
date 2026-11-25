"""Turn a session document into display tables. No Streamlit here, so it is easy to test.

Rules:
- Only reads the session (meta, frames, analysis, advice). Never reads ground_truth.
- Never re-runs the analysis. The one derived thing is each detection's ground position,
  done with the same geometry function the analysis uses (pixel_to_ground), for the map.
- Missing sections give empty tables, never exceptions.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hima.analytics.geometry import Camera, DronePose, pixel_to_ground

ACTIVITIES = ("resting", "walking", "running")


def has_analysis(doc: dict[str, Any]) -> bool:
    return "analysis" in doc


def has_advice(doc: dict[str, Any]) -> bool:
    return "advice" in doc


def mission_summary(doc: dict[str, Any]) -> dict[str, Any]:
    meta, frames = doc["meta"], doc["frames"]
    cam = meta["camera"]
    out = {
        "mission_id": meta["mission_id"],
        "source": meta["source"],
        "created_at": meta["created_at"],
        "frames": len(frames),
        "detections": sum(len(f["detections"]) for f in frames),
        # with params_known = false, hfov and the drone pose are placeholders: do not show them
        "camera": (f"{cam['width_px']}x{cam['height_px']} px, hfov {cam['hfov_deg']} deg, {cam['mount']}"
                   if cam["params_known"] else
                   f"{cam['width_px']}x{cam['height_px']} px, camera parameters and drone pose unknown"),
        "params_known": cam["params_known"],
        "detector": meta["detector"]["weights"],
        "duration_s": None, "distance_flown_m": None, "animals": None, "config_sha256": None,
        "advice_model": None, "advice_accepted": None, "advice_total": None,
    }
    if has_analysis(doc):
        an = doc["analysis"]
        out.update(duration_s=an["drone"]["duration_s"], distance_flown_m=an["drone"]["distance_flown_m"],
                   animals=len(an["animals"]), config_sha256=an["config_used"]["sha256"])
    if has_advice(doc):
        rows = doc["advice"]["animals"]
        out.update(advice_model=doc["advice"]["model"], advice_total=len(rows),
                   advice_accepted=sum(r["gate"]["accepted"] for r in rows))
    return out


def animals_table(doc: dict[str, Any]) -> pd.DataFrame:
    if not has_analysis(doc):
        return pd.DataFrame()
    final = {r["track_id"]: r for r in doc["advice"]["animals"]} if has_advice(doc) else {}
    rows = []
    for a in doc["analysis"]["animals"]:
        r = {
            "track_id": a["track_id"], "species": a["species"], "status": a["status"],
            "visible_s": a["visible_s"], "visibility_ratio": a["visibility_ratio"],
            "isolation_ratio": a["isolation_ratio"],
            **{f"{k}_ratio": v for k, v in a["ratios"].items()},
            "max_speed_mps": a["max_ground_speed_mps"], "mean_speed_mps": a["mean_ground_speed_mps"],
            "distance_m": a["distance_travelled_m"],
            "min_distance_to_drone_m": a["min_slant_distance_to_drone_m"],
            "events": len(a["events"]), "metrics_valid": a["metrics_valid"],
        }
        if a["track_id"] in final:
            f = final[a["track_id"]]
            r["command"] = f["final"]["proposed_command"]
            r["altitude_m"] = f["final"]["proposed_altitude_m"]
            r["decided_by"] = f["final"]["source"]
        rows.append(r)
    return pd.DataFrame(rows)


def ethogram(doc: dict[str, Any]) -> pd.DataFrame:
    """Long table: one row per (animal, activity) with seconds. Animals without metrics are left out."""
    cols = ["track_id", "activity", "seconds"]
    if not has_analysis(doc):
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"track_id": a["track_id"], "activity": act, "seconds": a["ethogram_s"][act]}
                         for a in doc["analysis"]["animals"] if a["ethogram_s"] is not None
                         for act in ACTIVITIES], columns=cols)


def events(doc: dict[str, Any]) -> pd.DataFrame:
    cols = ["track_id", "type", "start_s", "end_s", "duration_s"]
    if not has_analysis(doc):
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"track_id": a["track_id"], **{k: e[k] for k in cols[1:]}}
                         for a in doc["analysis"]["animals"] for e in a["events"]], columns=cols)


def herd(doc: dict[str, Any]) -> pd.DataFrame:
    cols = ["t", "count", "cohesion_m", "isolated"]
    if not has_analysis(doc):
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{"t": h["t"], "count": h["count"], "cohesion_m": h["cohesion_m"],
                          "isolated": len(h["isolated_ids"])} for h in doc["analysis"]["herd_timeline"]],
                        columns=cols)


def drone_path(doc: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame([{"t": f["t"], "x_m": f["drone"]["x_m"], "y_m": f["drone"]["y_m"],
                          "agl_m": f["drone"]["agl_m"]} for f in doc["frames"]])


def animal_positions(doc: dict[str, Any]) -> pd.DataFrame:
    """One point per animal: where it was registered (analysis sighting: no animal
    paths on the map, only the place it was found). An older session has no sighting: the first
    detection of each analysed track is used. Empty when camera parameters are unknown."""
    cols = ["t", "track_id", "species", "status", "x_m", "y_m", "lat_deg", "lon_deg"]
    cam_meta = doc["meta"]["camera"]
    if not cam_meta["params_known"]:
        return pd.DataFrame(columns=cols)
    animals = doc["analysis"]["animals"] if has_analysis(doc) else []
    if animals and all("sighting" in a for a in animals):
        rows = [{"t": a["sighting"]["t"], "track_id": a["track_id"], "species": a["species"], "status": a["status"],
                 "x_m": a["sighting"]["x_m"], "y_m": a["sighting"]["y_m"], "lat_deg": a["sighting"]["lat_deg"],
                 "lon_deg": a["sighting"]["lon_deg"]} for a in animals if a["sighting"]["x_m"] is not None]
        return pd.DataFrame(rows, columns=cols)
    cam = Camera.from_meta(cam_meta)
    status = {a["track_id"]: a["status"] for a in animals}
    rows, seen = [], set()
    for f in doc["frames"]:
        pose = DronePose.from_frame(f["drone"])
        for d in f["detections"]:
            if d["track_id"] in seen or (status and d["track_id"] not in status):
                continue
            seen.add(d["track_id"])
            g = pixel_to_ground(d["bbox_xywh_px"][0], d["bbox_xywh_px"][1], cam, pose)
            rows.append({"t": f["t"], "track_id": d["track_id"], "species": d["species"],
                         "status": status.get(d["track_id"]), "x_m": g.x_m, "y_m": g.y_m,
                         "lat_deg": None, "lon_deg": None})
    return pd.DataFrame(rows, columns=cols)


def square_domain(*frames: pd.DataFrame, pad: float = 0.05) -> tuple[list[float], list[float]]:
    """Equal-span x and y ranges so one metre looks the same on both axes."""
    xs = pd.concat([f["x_m"] for f in frames if len(f)])
    ys = pd.concat([f["y_m"] for f in frames if len(f)])
    span = max(xs.max() - xs.min(), ys.max() - ys.min(), 1.0) * (1 + 2 * pad)
    cx, cy = (xs.max() + xs.min()) / 2, (ys.max() + ys.min()) / 2
    return [cx - span / 2, cx + span / 2], [cy - span / 2, cy + span / 2]


def advice_cards(doc: dict[str, Any]) -> list[dict[str, Any]]:
    if not has_advice(doc):
        return []
    status = {a["track_id"]: a["status"] for a in doc["analysis"]["animals"]} if has_analysis(doc) else {}
    cards = []
    for r in doc["advice"]["animals"]:
        p = r["parsed"] or {}
        cards.append({
            "track_id": r["track_id"], "status": status.get(r["track_id"], "unknown"),
            "accepted": r["gate"]["accepted"], "reasons": list(r["gate"]["reasons"]),
            "command": r["final"]["proposed_command"], "altitude_m": r["final"]["proposed_altitude_m"],
            "decided_by": r["final"]["source"],
            "llm_command": p.get("proposed_command"), "llm_altitude_m": p.get("proposed_altitude_m"),
            "interpretation": p.get("interpretation"),
            "latency_s": r["latency_s"],
        })
    return cards
