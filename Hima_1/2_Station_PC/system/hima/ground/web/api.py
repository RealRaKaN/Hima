"""JSON for the station dashboard, from the ground archive only (index.json + the archived packages).

Every cross-mission number comes from hima.ground.stats (through hima.ground.compare), every number about one
mission from that mission's own archived files (hima.ground.mission and the archived session). Each mission folder is
verified again on every Overview load (SHA-256), so a changed file in the archive is shown, not hidden.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from hima.analytics.status import STATUSES
from hima.contract.io import SessionInvalid, load_session
from hima.contract.package import MANIFEST, sha256_file
from hima.ground.archive import INDEX, check_package, load_index
from hima.ground.compare import MIN_RATE_S, choices, compare, label, status_order, trend
from hima.ground.demo import demo_id, demo_ids
from hima.ground.mission import (animals_rows, findings_rows, flight_event_rows, flight_image_rows, flight_log,
                                 media_rows, mission_files, movement_rows, window_rows)
from hima.ground.review import LABEL_NOTE, review_items
from hima.onboard.report_story import story
from hima.ground.stats import CENSUS_NOTE, mission_stats

MAX_PATH_POINTS = 1500
MAX_COMPARE = 6
_SESSIONS: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_SLOCK = threading.Lock()
_VERIFIED: dict[str, tuple[tuple, list[str]]] = {}


class NotFound(Exception):
    pass


def stamp(archive: Path) -> int:
    p = archive / INDEX
    return p.stat().st_mtime_ns if p.exists() else 0


def missions(archive: Path) -> list[dict[str, Any]]:
    """Index rows, oldest flight first (flight date, then when the package was made)."""
    ms = load_index(archive)["missions"]
    return sorted(ms, key=lambda m: (m["flight_date"], m.get("package_created_at") or "", m["mission_id"]))


def _signature(d: Path) -> tuple:
    return tuple(sorted((str(p.relative_to(d)), p.stat().st_size, p.stat().st_mtime_ns)
                        for p in d.rglob("*") if p.is_file()))


def verify(archive: Path) -> dict[str, list[str]]:
    """hima.ground.archive.verify_archive, with each folder's result kept until one of its files changes (size or
    time): the full check (SHA-256 of every file, the session against its report) runs again only then."""
    idx = {e["mission_id"]: e for e in load_index(archive)["missions"]}
    out: dict[str, list[str]] = {}
    for mid, e in idx.items():
        d = archive / mid
        if not d.is_dir():
            out[mid] = ["folder is missing"]
            continue
        sig = (_signature(d), e["manifest_sha256"])
        key = str(d.resolve())
        if key not in _VERIFIED or _VERIFIED[key][0] != sig:
            errs = check_package(d)
            if not errs and sha256_file(d / MANIFEST) != e["manifest_sha256"]:
                errs = ["package.json differs from the indexed one"]
            _VERIFIED[key] = (sig, errs)
        if _VERIFIED[key][1]:
            out[mid] = _VERIFIED[key][1]
    for d in archive.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.name not in idx:
            out[d.name] = ["folder is not in the index (run rebuild_index)"]
    return out


def _virtual(archive: Path) -> set[str]:
    return set(demo_ids(archive))


def pick(ms: list[dict[str, Any]], virtual: set[str], include_virtual: bool | None) -> tuple[list[dict], bool]:
    """The missions the totals count. include_virtual None = only while no real flight has arrived."""
    real = [m for m in ms if m["mission_id"] not in virtual]
    inc = (not real) if include_virtual is None else include_virtual
    return (ms if inc else real), inc


def state(archive: Path, plans_pending: str | None) -> dict[str, Any]:
    ms = missions(archive) if archive.is_dir() else []
    virtual = _virtual(archive)
    real = [m["mission_id"] for m in ms if m["mission_id"] not in virtual]
    return {"archive": str(archive), "archive_ok": archive.is_dir(), "stamp": stamp(archive),
            "missions": len(ms), "real": len(real), "virtual": len(ms) - len(real),
            "newest": (real[-1] if real else demo_id(archive) if demo_id(archive) in {m["mission_id"] for m in ms}
                       else ms[-1]["mission_id"] if ms else None),
            "ids": [m["mission_id"] for m in ms], "route_pending": plans_pending}


def _row(m: dict[str, Any], virtual: set[str], problems: dict[str, list[str]]) -> dict[str, Any]:
    return {"mission_id": m["mission_id"], "label": label([m], "flight", m["mission_id"]),
            "flight_date": m["flight_date"], "source": m["source"], "virtual": m["mission_id"] in virtual,
            "duration_s": m["duration_s"], "tracks": m["tracks_seen"], "tracks_by_species": m["tracks_by_species"],
            "alert_tracks": len(m["alert_track_ids"]), "statuses": m["statuses"],
            "uncertain": sum(m["uncertain"].values()), "uncertain_with_image": m["uncertain_with_image"],
            "clips": m.get("clips", {}), "report_source": m["report_source"], "headline": m["report_headline"],
            "verified": m["mission_id"] not in problems, "problems": problems.get(m["mission_id"], [])[:3]}


def mission_list(archive: Path) -> dict[str, Any]:
    """Every mission, newest first, for the Mission page's picker (no verification: the Overview does that)."""
    virtual = _virtual(archive)
    return {"rows": [_row(m, virtual, {}) for m in reversed(missions(archive))], "main_virtual": demo_id(archive)}


def overview(archive: Path, include_virtual: bool | None = None, start: str | None = None,
             end: str | None = None) -> dict[str, Any]:
    ms = missions(archive)
    virtual = _virtual(archive)
    problems = verify(archive)
    counted, inc = pick(ms, virtual, include_virtual)
    days_all = sorted({m["flight_date"] for m in counted})
    if start or end:
        counted = [m for m in counted if (not start or m["flight_date"] >= start) and (not end or m["flight_date"] <= end)]
    s = mission_stats(counted) if counted else None
    return {
        "include_virtual": inc, "virtual_count": sum(m["mission_id"] in virtual for m in ms),
        "real_count": sum(m["mission_id"] not in virtual for m in ms), "days": days_all,
        "rows": [_row(m, virtual, problems) for m in reversed(ms)],          # newest first; all of them
        "counted": [m["mission_id"] for m in counted],
        "problems": problems, "note": CENSUS_NOTE,
        "totals": None if s is None else {k: s[k] for k in ("missions", "flight_s", "tracks", "tracks_by_species",
                                                           "alert_tracks", "statuses", "events", "uncertain",
                                                           "uncertain_with_image", "report_sources")},
        "status_order": status_order(s["statuses"]) if s else [],
        "trend": trend(counted) if counted else {"species": [], "days": []},
    }


# ---- one mission --------------------------------------------------------------------------------------------------

def _session(path: Path, sha: str) -> dict[str, Any]:
    key = (str(path), sha)
    with _SLOCK:
        if key in _SESSIONS:
            _SESSIONS.move_to_end(key)
            return _SESSIONS[key]
    doc = load_session(path)
    with _SLOCK:
        _SESSIONS[key] = doc
        while len(_SESSIONS) > 4:
            _SESSIONS.popitem(last=False)
    return doc


def session_for(archive: Path, mid: str) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    f = _files(archive, mid)
    try:
        return f, _session(f["session_path"], f["manifest"]["session_sha256"]), None
    except (SessionInvalid, OSError, ValueError) as e:
        return f, None, f"{type(e).__name__}: {e}"


def _files(archive: Path, mid: str) -> dict[str, Any]:
    if mid not in {m["mission_id"] for m in load_index(archive)["missions"]}:
        raise NotFound(f"no mission {mid!r} in the archive")
    return mission_files(archive, mid)


def _narrative(rep: dict[str, Any]) -> dict[str, Any]:
    """The narrative report: stage texts in order, incidents with the titles the code gave them."""
    r = rep["report"]
    if "flight" not in r:
        return {"narrative": False}
    try:
        facts = {i["facts"]["incident"]: i["facts"] for i in story(rep["summary"])["incidents"]}
    except Exception:                                 # noqa: BLE001 - a title is a convenience, never a crash
        facts = {}
    return {"narrative": True, "flight": [s["text"] for s in r["flight"]], "closing": r["closing"],
            "incidents": [{"incident": i["incident"], "title": facts.get(i["incident"], {}).get("title", i["incident"]),
                           "at_min": facts.get(i["incident"], {}).get("at_min"), "text": i["text"],
                           "follow_up": i["follow_up"]} for i in r["incidents"]]}

def _judge(rep: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for j in rep.get("judge") or []:
        verdict = problem = None
        try:
            d = json.loads(j.get("raw") or "")
            verdict, problem = d.get("verdict"), d.get("problem") or None
        except (ValueError, AttributeError):
            pass
        out.append({"part": j.get("part"), "verdict": verdict, "problem": problem, "reasons": j.get("reasons") or []})
    return out


def _thin(seq: list, n: int) -> list:
    if len(seq) <= n:
        return seq
    step = len(seq) / n
    return [seq[int(i * step)] for i in range(n)] + [seq[-1]]


def _map(doc: dict[str, Any], log: dict[str, Any] | None) -> dict[str, Any]:
    from hima.dashboard import data as sdata
    if not doc["meta"]["camera"]["params_known"]:
        return {"known": False}
    path = [[round(f["t"], 1), round(f["drone"]["x_m"], 1), round(f["drone"]["y_m"], 1), round(f["drone"]["agl_m"], 1)]
            for f in doc["frames"]]
    pos = sdata.animal_positions(doc)
    pts = [{k: (None if v != v else v) for k, v in r.items()} for r in pos.to_dict("records")]   # NaN -> None
    home = ((log or {}).get("telemetry") or {}).get("home")
    return {"known": True, "path": _thin(path, MAX_PATH_POINTS), "points": pts,
            "home": home if isinstance(home, list) and len(home) == 2 else None}


def _boxes_for_alert(log: dict[str, Any] | None, alert: str | None) -> list[dict[str, Any]]:
    if not log or not alert:
        return []
    for a in (log.get("objects") or {}).get("alerts", []):
        if a.get("text") == alert:
            return [{"label": b.get("cls"), "conf": b.get("conf"), "bbox_xywh_px": b["bbox_xywh_px"]}
                    for b in a.get("boxes", []) if b.get("kept", True)]
    return []


def mission(archive: Path, mid: str) -> dict[str, Any]:
    f, doc, session_error = session_for(archive, mid)
    entry = next(m for m in load_index(archive)["missions"] if m["mission_id"] == mid)
    rep, man, folder = f["report"], f["manifest"], f["folder"]
    sm = rep["summary"]
    log, log_error = None, None
    try:
        log = flight_log(folder, man)
    except (OSError, ValueError) as e:
        log_error = f"{type(e).__name__}: {e}"
    extra = {a["track_id"]: a for a in (doc["analysis"]["animals"] if doc and doc.get("analysis") else [])}
    animals = []
    for a in animals_rows(rep):
        x = extra.get(a["track_id"], {})
        animals.append({**a, "events": next((b["events"] for b in sm["animals"] if b["track_id"] == a["track_id"]), {}),
                        "ethogram_s": x.get("ethogram_s"), "distance_m": x.get("distance_travelled_m"),
                        "max_speed_mps": x.get("max_ground_speed_mps"), "visibility_ratio": x.get("visibility_ratio"),
                        "sighting": x.get("sighting")})
    events = ([{"track_id": a["track_id"], "species": a["species"], "status": a["status"], **{k: e[k] for k in (
        "type", "start_s", "end_s", "duration_s")}} for a in doc["analysis"]["animals"] for e in a["events"]]
        if doc and doc.get("analysis") else [])
    herd = ([{"t": h["t"], "count": h["count"], "isolated": len(h["isolated_ids"]), "cohesion_m": h["cohesion_m"]}
             for h in doc["analysis"]["herd_timeline"]] if doc and doc.get("analysis") else [])
    media = man.get("media") or None
    clips, stills = [], []
    for r in media_rows(man):
        it = next(i for i in man["items"] if i["id"] == r["id"])
        url = f"/media/{mid}/{r['file']}" if r["file"] and r["state"] == "stored" else None
        if r["kind"] == "event_clip":
            clips.append({"id": r["id"], "state": r["state"], "why": r["why"], "t_start": r["t_start"],
                          "t_end": r["t_end"], "tracks": it["track_ids"], "events": it.get("event_types", []),
                          "frames": (it["frame_last"] - it["frame_first"] + 1) if it.get("frame_first") is not None
                          else None, "url": url, "playable": bool(url and doc)})
        else:
            stills.append({"id": r["id"], "state": r["state"], "criterion": r["why"], "reason": it["reason"],
                           "t": it.get("sample_t"), "frame": it.get("sample_frame"), "tracks": it["track_ids"],
                           "boxes": [{"label": b["class_raw"], "conf": b["conf"], "track_id": b.get("track_id"),
                                      "bbox_xywh_px": b["bbox_xywh_px"]} for b in it.get("boxes", [])], "url": url})
    images = [{**r, "url": f"/media/{mid}/{r['file']}" if r["file"] and r["state"] == "stored" else None,
               "boxes": _boxes_for_alert(log, r["alert"])} for r in flight_image_rows(man)]
    flight = None
    if log is not None:
        flight = {"events": flight_event_rows(log), "windows": window_rows(log), "movement": movement_rows(log),
                  "movement_note": (log.get("movement") or {}).get("note", ""),
                  "end": (log.get("flight") or {}).get("end"),
                  "camera": (log.get("flight") or {}).get("camera"),
                  "preflight": (log.get("flight") or {}).get("preflight") or [],
                  "periodic": [{"n": p["n"], "t": p["t"], "kind": p["kind"], "text": p["text"]}
                               for p in (log.get("periodic") or {}).get("results", [])],
                  "dwell": (log.get("dwell") or {}).get("alerts", [])}
    kinds: dict[str, int] = {}
    for it in man["items"]:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    files = [{"id": it["id"], "kind": it["kind"], "state": it["state"], "bytes": it["file"]["bytes"],
              "path": it["file"]["path"], "url": f"/media/{mid}/{it['file']['path']}"}
             for it in man["items"] if it.get("file") and it["state"] == "stored"
             and it["kind"] in ("session", "report", "flight_log", "raw_flight", "no_position_analysis")]
    return {
        "mission_id": mid, "label": label([entry], "flight", mid), "virtual": mid in _virtual(archive),
        "main_virtual": mid == demo_id(archive), "entry": entry,
        "report": {"source": rep["source"], "model": rep.get("model"), "headline": rep["report"]["headline"],
                   **_narrative(rep),
                   "groups": (sm.get("detail") or {}).get("groups"),
                   "vehicle_list": (sm.get("detail") or {}).get("vehicles"),
                   "vehicles_known": (sm.get("detail") or {}).get("vehicles_known"), "link_m": (sm.get("detail") or {}).get("link_m"),
                   "nothing_unusual": (sm.get("detail") or {}).get("nothing_unusual"),
                   "findings": findings_rows(rep), "gate": rep.get("gate"), "judge": _judge(rep),
                   "duration_s": sm["duration_s"], "tracks_seen": sm["tracks_seen"],
                   "tracks_by_species": sm["tracks_by_species"], "alert_track_ids": sm["alert_track_ids"],
                   "metrics_known": sm["metrics_known"]},
        "animals": animals, "status_order": list(STATUSES), "events": events, "herd": herd,
        "map": _map(doc, log) if doc else {"known": False},
        "session_error": session_error, "log_error": log_error,
        "media": {"frame": None if not media else {"w": media["width_px"], "h": media["height_px"],
                                                    "fps": media["fps"]},
                  "clips": clips, "stills": stills, "images": images},
        "flight": flight, "package": {"items": kinds, "budget": man.get("budget"), "created_at": man["created_at"],
                                      "files": files},
        "census_note": CENSUS_NOTE,
    }


def clip_boxes(archive: Path, mid: str, item_id: str) -> dict[str, Any]:
    """Per clip frame, the session's boxes on it (the clip's frames are session frames frame_first..frame_last)."""
    f, doc, err = session_for(archive, mid)
    it = next((i for i in f["manifest"]["items"] if i["id"] == item_id and i["kind"] == "event_clip"), None)
    if it is None:
        raise NotFound(f"no clip {item_id!r} in {mid}")
    frames = doc["frames"] if doc else []
    lo, hi = it.get("frame_first"), it.get("frame_last")
    boxes = []
    if lo is not None and frames:
        for k in range(lo, min(hi, len(frames) - 1) + 1):
            boxes.append([{"track_id": d["track_id"], "species": d["species"],
                           "bbox_xywh_px": [round(v, 1) for v in d["bbox_xywh_px"]]} for d in frames[k]["detections"]])
    return {"id": item_id, "frame_first": lo, "t_start": it["t_start"], "boxes": boxes, "tracks": it["track_ids"],
            "session_error": err}


# ---- compare and review -------------------------------------------------------------------------------------------

def compare_view(archive: Path, by: str, keys: list[str] | None, include_virtual: bool | None) -> dict[str, Any]:
    ms = missions(archive)
    virtual = _virtual(archive)
    counted, inc = pick(ms, virtual, include_virtual)
    opts = choices(counted, by)
    out: dict[str, Any] = {"by": by, "include_virtual": inc, "max": MAX_COMPARE, "min_rate_s": MIN_RATE_S,
                           "options": [{"key": k, "label": label(counted, by, k),
                                        "virtual": by == "flight" and k in virtual} for k in reversed(opts)]}
    keys = [k for k in opts if k in (keys or [])][:MAX_COMPARE] if keys else opts[-min(4, len(opts)):]
    out["keys"] = keys
    if len(keys) >= 2:
        r = compare(counted, by, keys)
        out["result"] = {**r, "labels": {k: label(counted, by, k) for k in keys}}
    return out


def review(archive: Path, include_virtual: bool | None) -> dict[str, Any]:
    ms = missions(archive)
    virtual = _virtual(archive)
    counted, inc = pick(ms, virtual, include_virtual)
    ids = {m["mission_id"] for m in counted}
    items = [r for r in review_items(archive) if r["mission_id"] in ids]
    out = []
    for r in items:
        it = r["item"]
        out.append({"mission_id": r["mission_id"], "id": it["id"], "criterion": it["criterion"], "reason": it["reason"],
                    "t": it.get("sample_t"), "frame": it.get("sample_frame"), "tracks": it["track_ids"],
                    "size": r["size"], "virtual": r["mission_id"] in virtual,
                    "boxes": [{"label": b["class_raw"], "conf": b["conf"], "track_id": b.get("track_id"),
                               "bbox_xywh_px": b["bbox_xywh_px"]} for b in it.get("boxes", [])],
                    "url": f"/media/{r['mission_id']}/{it['file']['path']}" if r["image"] is not None else None})
    return {"include_virtual": inc, "note": LABEL_NOTE, "total": len(out),
            "with_image": sum(o["url"] is not None for o in out), "items": out, "archive": str(archive)}
