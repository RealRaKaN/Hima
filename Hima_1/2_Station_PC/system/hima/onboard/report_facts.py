"""More facts for the landing report: where the animals were, in which groups,
vehicles and how far they were from the nearest group, why an animal was disturbed, how it moved.

The report must read like "5 oryx and 16 gazelle in different places, nothing new or unusual; a
vehicle 3 km from one of the herds; oryx N disturbed because the drone came close" - not only times on screen.

Everything here is computed, never guessed, from the session (drone pose + boxes, flat ground and nadir camera as in
the analysis), the analysis, and the flight log when the package has one:
  groups     tracks whose places (mean ground point of their boxes) are within link_m of each other, chained
             (single link). One place for the whole flight: a group is "the tracks found there", not a herd that
             stayed together. Distance and compass direction from the launch point (the session's x/y origin).
  vehicles   each vehicle alert of the flight log (objects.alerts), placed with its box centre and the drone's pose
             at that time; the nearest group and the distance to it. None = no flight log: unknown, not "none".
  causes     the cause the flight's alert gave (VEHICLE); a SEVERE_DRONE_DISTURBANCE without one is the drone.
  unusual    alert statuses and vehicles; nothing_unusual is true only when the flight had neither.
  together   per group and species, the most tracks seen in ONE frame: "at least N animals". A track is not an animal (one animal can get several ids), but tracks in the same frame
             are different animals, so this is an honest minimum.
  route      the drone's path from the frames: its usual height, the farthest point from the launch point (distance,
             direction, minute) and how far from the launch point the session ended.
The numbers keep their unit in the key (_m, _s, _min, _mps), so the report's fixed gate can check a number against
the facts of the part it is in. All values are JSON types (lists, string keys): the archive recomputes this
dictionary and compares it with the stored one.
"""
from __future__ import annotations

import math
from typing import Any

from hima.analytics.geometry import Camera, DronePose, pixel_to_ground
from hima.onboard.alerts import PRIORITY

COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
SPECIES_WORDS = {"arabian_oryx": "Arabian oryx", "reem_gazelle": "reem gazelle"}


def _r1(x: float | None) -> float | None:
    return None if x is None else round(float(x), 1)


def direction(x: float, y: float) -> str:
    """Compass word (8 points) of a point x east, y north of the launch point."""
    if math.hypot(x, y) < 1e-9:
        return "at the launch point"
    b = math.degrees(math.atan2(x, y)) % 360
    return COMPASS[int((b + 22.5) // 45) % 8]


def track_places(doc: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """track id -> mean ground point (x east, y north, metres) of all its boxes. {} without camera parameters."""
    meta = doc["meta"]
    if not meta["camera"]["params_known"]:
        return {}
    cam = Camera.from_meta(meta["camera"])
    acc: dict[int, list[float]] = {}
    for f in doc["frames"]:
        d = f.get("drone")
        if not d or not d.get("agl_m") or d["agl_m"] <= 0:
            continue
        pose = DronePose.from_frame(d)
        for det in f["detections"]:
            x, y, w, h = det["bbox_xywh_px"]
            g = pixel_to_ground(x + w / 2, y + h / 2, cam, pose)
            a = acc.setdefault(det["track_id"], [0.0, 0.0, 0])
            a[0] += g.x_m
            a[1] += g.y_m
            a[2] += 1
    return {tid: (a[0] / a[2], a[1] / a[2]) for tid, a in acc.items() if a[2]}


def _clusters(places: dict[int, tuple[float, float]], link_m: float) -> list[list[int]]:
    ids = sorted(places)
    parent = {i: i for i in ids}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for n, i in enumerate(ids):
        for j in ids[n + 1:]:
            if math.dist(places[i], places[j]) <= link_m:
                parent[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in ids:
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def _pose_at(doc: dict[str, Any], t: float) -> dict[str, Any] | None:
    best = None
    for f in doc["frames"]:
        if f.get("drone") and (best is None or abs(f["t"] - t) < abs(best["t"] - t)):
            best = f
    return best["drone"] if best is not None and abs(best["t"] - t) <= 2.0 else None


def _vehicles(doc: dict[str, Any], flight_log: dict[str, Any], groups: list[dict[str, Any]],
              centres: dict[str, tuple[float, float]]) -> list[dict[str, Any]]:
    meta = doc["meta"]
    cam = Camera.from_meta(meta["camera"]) if meta["camera"]["params_known"] else None
    out = []
    for n, al in enumerate((flight_log.get("objects") or {}).get("alerts") or [], 1):
        boxes = [b for b in al.get("boxes") or [] if b.get("group") == "VEHICLE" and b.get("kept", True)]
        v = {"vehicle": f"V{n}", "kind": boxes[0]["cls"] if boxes else "vehicle", "count": int(al.get("count") or 1),
             "seen_min": _r1(al["t"] / 60), "nearest_group": None, "distance_to_group_m": None,
             "group_by_species": None, "distance_from_launch_m": None, "direction_from_launch": None}
        pose = _pose_at(doc, al["t"]) if cam is not None else None
        if pose is not None and boxes:
            x, y, w, h = boxes[0]["bbox_xywh_px"]
            g = pixel_to_ground(x + w / 2, y + h / 2, cam, DronePose.from_frame(pose))
            v["distance_from_launch_m"] = int(round(math.hypot(g.x_m, g.y_m)))
            v["direction_from_launch"] = direction(g.x_m, g.y_m)
            if centres:
                name, d = min(((k, math.dist((g.x_m, g.y_m), c)) for k, c in centres.items()), key=lambda kv: kv[1])
                v["nearest_group"], v["distance_to_group_m"] = name, int(round(d))
                v["group_by_species"] = next(gr["by_species"] for gr in groups if gr["group"] == name)
        out.append(v)
    return out


def _together(doc: dict[str, Any], animals: dict[int, dict[str, Any]], group_of: dict[int, str]) -> dict[str, dict]:
    """group -> species -> the most of its tracks seen in one frame."""
    out: dict[str, dict[str, int]] = {}
    for f in doc["frames"]:
        n: dict[tuple[str, str], set[int]] = {}
        for det in f["detections"]:
            tid = det["track_id"]
            if tid in group_of:
                n.setdefault((group_of[tid], animals[tid]["species"]), set()).add(tid)
        for (g, sp), ids in n.items():
            d = out.setdefault(g, {})
            d[sp] = max(d.get(sp, 0), len(ids))
    return out


def _route(doc: dict[str, Any]) -> dict[str, Any] | None:
    pts = [(f["t"], f["drone"]) for f in doc["frames"] if f.get("drone")]
    if not pts:
        return None
    t0 = doc["frames"][0]["t"]
    tf, far = max(pts, key=lambda p: math.hypot(p[1]["x_m"], p[1]["y_m"]))
    alts = sorted(d["agl_m"] for _, d in pts)
    end = pts[-1][1]
    return {"altitude_m": int(round(alts[len(alts) // 2])),
            "farthest_m": int(round(math.hypot(far["x_m"], far["y_m"]))),
            "farthest_direction": direction(far["x_m"], far["y_m"]),
            "farthest_min": _r1((tf - t0) / 60),
            "end_from_launch_m": int(round(math.hypot(end["x_m"], end["y_m"])))}


def _causes(flight_log: dict[str, Any] | None) -> dict[int, str]:
    out: dict[int, str] = {}
    for al in (flight_log or {}).get("alerts") or []:
        if "track_id" in al and al.get("cause"):
            out[int(al["track_id"])] = str(al["cause"]).lower()
    return out


def detail(doc: dict[str, Any], analysis: dict[str, Any], flight_log: dict[str, Any] | None,
           link_m: float, near_m: float | None = None, close_m: float | None = None) -> dict[str, Any]:
    """The "detail" part of the report summary (see the module text). near_m / close_m: [behaviour]
    vehicle_near_herd_m / vehicle_close_m, kept here so the narrative (report_story) follows from the summary alone."""
    animals = {a["track_id"]: a for a in analysis["animals"]}
    places = {t: p for t, p in track_places(doc).items() if t in animals}
    groups: list[dict[str, Any]] = []
    centres: dict[str, tuple[float, float]] = {}
    group_of: dict[int, str] = {}
    clusters = sorted(_clusters(places, link_m),
                      key=lambda ids: min(animals[i]["first_seen_s"] for i in ids))
    for n, ids in enumerate(clusters, 1):
        name = f"G{n}"
        cx = sum(places[i][0] for i in ids) / len(ids)
        cy = sum(places[i][1] for i in ids) / len(ids)
        centres[name] = (cx, cy)
        by_sp: dict[str, int] = {}
        st: dict[str, int] = {}
        for i in ids:
            by_sp[animals[i]["species"]] = by_sp.get(animals[i]["species"], 0) + 1
            st[animals[i]["status"]] = st.get(animals[i]["status"], 0) + 1
            group_of[i] = name
        groups.append({"group": name, "tracks": len(ids), "by_species": dict(sorted(by_sp.items())),
                       "statuses": dict(sorted(st.items())), "track_ids": ids,
                       "distance_from_launch_m": int(round(math.hypot(cx, cy))),
                       "direction_from_launch": direction(cx, cy),
                       "first_seen_min": _r1(min(animals[i]["first_seen_s"] for i in ids) / 60),
                       "last_seen_min": _r1(max(animals[i]["last_seen_s"] for i in ids) / 60)})
    together = _together(doc, animals, group_of)
    for g in groups:
        g["seen_together"] = dict(sorted(together.get(g["group"], {}).items()))
    vehicles = None if flight_log is None else _vehicles(doc, flight_log, groups, centres)
    causes = _causes(flight_log)
    alert = {}
    for tid, a in animals.items():
        if a["status"] not in PRIORITY:
            continue
        cause = causes.get(tid)
        if a["status"] == "SEVERE_DRONE_DISTURBANCE" and cause is None and flight_log is not None:
            cause = "drone"
        eth = {k: _r1(v) for k, v in (a.get("ethogram_s") or {}).items() if v}
        alert[str(tid)] = {"group": group_of.get(tid), "cause": cause,
                           "behaviour_s": dict(sorted(eth.items())),
                           "distance_travelled_m": _r1(a.get("distance_travelled_m")),
                           "max_speed_mps": _r1(a.get("max_ground_speed_mps"))}
    drone = analysis.get("drone") or {}
    n_alert = sum(a["status"] in PRIORITY for a in animals.values())
    n_veh = None if vehicles is None else len(vehicles)
    return {"link_m": float(link_m),
            "vehicle_near_herd_m": None if near_m is None else float(near_m),
            "vehicle_close_m": None if close_m is None else float(close_m),
            "route": _route(doc),
            "flight": {"duration_min": _r1((drone.get("duration_s") or 0) / 60) if drone.get("duration_s") else None,
                       "distance_flown_m": int(round(drone["distance_flown_m"])) if drone.get("distance_flown_m")
                       is not None else None},
            "groups_count": len(groups), "groups": groups,
            "vehicles_known": vehicles is not None, "vehicle_sightings": n_veh, "vehicles": vehicles,
            "alert_animals": alert,
            "nothing_unusual": n_alert == 0 and n_veh == 0}
