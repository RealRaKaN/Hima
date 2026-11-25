"""The plan as files for ArduPilot (Plane with Q_ENABLE = 1, the VTOL quadplane) and for people.

mission.waypoints  QGC WPL 110 text (Mission Planner / QGroundControl "load from file"):
                   home, VTOL take-off, speed, the targets at patrol height, VTOL landing at the launch point
fence.waypoints    the inclusion polygon (MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION, mission type FENCE) and
                   the fence return point
fence.parm         FENCE_* and failsafe parameters to check against the autopilot (not written by this program)
plan.json          the numbers: budget, targets, cells, scores used, config hash
plan.svg           a picture: boundary, fence, scores, route

These files contain points of the reserve boundary (WDPA terms: never redistribute), so they belong in the
flight folder on the Jetson and the station, not in git or on a public page.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from hima.autonomy.planner import Plan, Scores
from hima.autonomy.reserve import Reserve
from hima.config import Config

# MAVLink common.xml command ids
NAV_WAYPOINT = 16
NAV_VTOL_TAKEOFF = 84
NAV_VTOL_LAND = 85
DO_CHANGE_SPEED = 178
NAV_FENCE_RETURN_POINT = 5000
NAV_FENCE_POLYGON_VERTEX_INCLUSION = 5001
FRAME_GLOBAL = 0
FRAME_GLOBAL_RELATIVE_ALT = 3


def _line(seq: int, frame: int, cmd: int, p: tuple = (0, 0, 0, 0), lat=0.0, lon=0.0, alt=0.0, current=0) -> str:
    return "\t".join([str(seq), str(current), str(frame), str(cmd), *(f"{v:g}" for v in p),
                      f"{lat:.8f}", f"{lon:.8f}", f"{alt:.2f}", "1"])


def mission_lines(cfg: Config, plan: Plan) -> list[str]:
    v = cfg.vehicle
    la, lo = plan.launch_latlon
    out = ["QGC WPL 110",
           _line(0, FRAME_GLOBAL, NAV_WAYPOINT, lat=la, lon=lo, alt=0.0, current=1),            # home
           _line(1, FRAME_GLOBAL_RELATIVE_ALT, NAV_VTOL_TAKEOFF, lat=la, lon=lo, alt=v.takeoff_alt_m),
           _line(2, FRAME_GLOBAL_RELATIVE_ALT, DO_CHANGE_SPEED, p=(0, v.cruise_speed_mps, -1, 0))]
    seq = 3
    for a, b in plan.targets_latlon:
        out.append(_line(seq, FRAME_GLOBAL_RELATIVE_ALT, NAV_WAYPOINT, lat=a, lon=b, alt=v.patrol_alt_m))
        seq += 1
    out.append(_line(seq, FRAME_GLOBAL_RELATIVE_ALT, NAV_VTOL_LAND, lat=la, lon=lo, alt=0.0))
    return out


def fence_lines(r: Reserve, plan: Plan) -> list[str]:
    pts = r.fence_latlon()
    out = ["QGC WPL 110"]
    la, lo = plan.launch_latlon
    out.append(_line(0, FRAME_GLOBAL, NAV_FENCE_RETURN_POINT, lat=la, lon=lo))
    for i, (a, b) in enumerate(pts, start=1):
        out.append(_line(i, FRAME_GLOBAL, NAV_FENCE_POLYGON_VERTEX_INCLUSION, p=(len(pts), 0, 0, 0), lat=a, lon=b))
    return out


JETSON_SYSID = 1                     # TelemetryReader.src_system: the Jetson speaks with the vehicle's system id


def safety_params(cfg: Config, jetson_sysid: int = JETSON_SYSID) -> dict[str, float]:
    """What the autopilot must hold for the autonomy rules: the fence, home when the Jetson stops, and hover at
    the guided point over a herd or a vehicle."""
    return {"FENCE_ENABLE": 1, "FENCE_TYPE": 5, "FENCE_ALT_MAX": cfg.safety.alt_max_m, "FENCE_ACTION": 1,
            "FENCE_MARGIN": cfg.reserve.fence_margin_m,
            "MAV_GCS_SYSID": jetson_sysid, "FS_GCS_ENABL": 1, "FS_LONG_ACTN": 1, "FS_LONG_TIMEOUT": 5,
            "Q_GUIDED_MODE": 1}


_NOTES = {"FENCE_TYPE": "1 = max altitude + 4 = polygon",
          "FENCE_ACTION": "RTL (a quadplane may change to QRTL near home, see Q_RTL_MODE)",
          "MAV_GCS_SYSID": "the Jetson is 'the GCS' for the GCS failsafe (ArduPilot 4.7 name of SYSID_MYGCS)",
          "FS_GCS_ENABL": "1 = heartbeat, in every mode (also GUIDED while following a herd)",
          "FS_LONG_ACTN": "1 = RTL when the Jetson's HEARTBEAT stops for FS_LONG_TIMEOUT s",
          "FS_LONG_TIMEOUT": "seconds",
          "Q_GUIDED_MODE": "1 = hover (VTOL) at the GUIDED point: over a herd or a vehicle"}


def fence_params(cfg: Config) -> list[str]:
    out = ["# Hima: parameters the autonomy needs on the VTOL quadplane (ArduPlane, Q_ENABLE = 1): the fence and the",
           "# Jetson-failure return home. The Jetson reads them back before arming and refuses to fly when one",
           "# differs; this program does not write parameters."]
    for k, v in safety_params(cfg).items():
        line = f"{k},{v:g}"
        out.append(f"{line:<22}# {_NOTES[k]}" if k in _NOTES else line)
    return out + [f"# Q_ASSIST_SPEED must be below the cruise speed ({cfg.vehicle.cruise_speed_mps:g} m/s): in SITL",
                  "# equal values started the VTOL assistance again 119 times in one flight"]


def plan_doc(cfg: Config, r: Reserve, plan: Plan, sc: Scores, others: list[Plan]) -> dict[str, Any]:
    return {"schema": "hima.plan/1", "reserve": r.summary(), "plan": plan.summary(),
            "budget": plan.budget.as_dict(),
            "launch": {"name": plan.launch, "lat_deg": plan.launch_latlon[0], "lon_deg": plan.launch_latlon[1]},
            "targets": [{"lat_deg": round(a, 7), "lon_deg": round(b, 7)} for a, b in plan.targets_latlon],
            "other_launch_points": [p.summary() for p in others if p is not plan],
            "vehicle": cfg.values()["vehicle"], "planner": cfg.values()["planner"],
            "config_sha256": cfg.sha256,
            "source": "WDPA: UNEP-WCMC and IUCN, Protected Planet (protectedplanet.net), site 555756385"}


def svg(r: Reserve, plan: Plan, sc: Scores, size_px: int = 900, block: int = 4) -> str:
    xs = [p[0] for p in r.ring]
    ys = [p[1] for p in r.ring]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    k = size_px / max(x1 - x0, y1 - y0)
    W, H = (x1 - x0) * k + 20, (y1 - y0) * k + 20

    def px(x, y):
        return 10 + (x - x0) * k, 10 + (y1 - y) * k

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
             f'style="background:#f6f4ee;font-family:sans-serif">']
    tot = sc.total.reshape(r.ny, r.nx)
    vmax = float(tot.max()) or 1.0
    c = r.cell_m * block
    for j in range(0, r.ny, block):
        for i in range(0, r.nx, block):
            v = float(np.max(tot[j:j + block, i:i + block]))
            if v <= 0:
                continue
            a, b = px(r.x0 + i * r.cell_m, r.y0 + (j + block) * r.cell_m)
            t = v / vmax
            parts.append(f'<rect x="{a:.1f}" y="{b:.1f}" width="{c * k:.1f}" height="{c * k:.1f}" '
                         f'fill="rgb({int(255 - 60 * t)},{int(235 - 150 * t)},{int(200 - 170 * t)})"/>')

    def poly(ring, style):
        pts = " ".join("%.1f,%.1f" % px(x, y) for x, y in ring)
        return f'<polygon points="{pts}" {style}/>'

    parts.append(poly(r.ring, 'fill="none" stroke="#555" stroke-width="1.2"'))
    parts.append(poly(r.fence, 'fill="none" stroke="#1f5fbf" stroke-width="1" stroke-dasharray="4 3"'))
    base = r.to_xy(*plan.launch_latlon)
    route = [base, *plan.targets_xy, base]
    parts.append('<polyline points="%s" fill="none" stroke="#c0262d" stroke-width="2"/>'
                 % " ".join("%.1f,%.1f" % px(x, y) for x, y in route))
    bx, by = px(*base)
    parts.append(f'<rect x="{bx - 5:.1f}" y="{by - 5:.1f}" width="10" height="10" fill="#111"/>')
    s = plan.summary()
    parts.append(f'<text x="14" y="24" font-size="14">{r.name}: route {s["route_km"]} km, '
                 f'{s["cells_crossed"]} cells, launch {plan.launch}, {plan.date}</text>')
    parts.append('<text x="14" y="42" font-size="11" fill="#555">grey: boundary (WDPA) - blue dashed: fence - '
                 'shade: cell score - red: route</text></svg>')
    return "\n".join(parts)


def write_plan(out: str | Path, cfg: Config, r: Reserve, plan: Plan, sc: Scores, others: list[Plan]) -> dict:
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    files = {"mission.waypoints": "\n".join(mission_lines(cfg, plan)) + "\n",
             "fence.waypoints": "\n".join(fence_lines(r, plan)) + "\n",
             "fence.parm": "\n".join(fence_params(cfg)) + "\n",
             "plan.json": json.dumps(plan_doc(cfg, r, plan, sc, others), indent=2, ensure_ascii=False) + "\n",
             "plan.svg": svg(r, plan, sc)}
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return {name: str(d / name) for name in files}


def read_waypoints(path: str | Path) -> list[dict[str, float]]:
    """Parse QGC WPL 110 text back (tests, and a check before upload)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "QGC WPL 110":
        raise ValueError(f"{path}: not a QGC WPL 110 file")
    out = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        f = ln.split("\t")
        out.append({"seq": int(f[0]), "current": int(f[1]), "frame": int(f[2]), "cmd": int(f[3]),
                    "p1": float(f[4]), "p2": float(f[5]), "p3": float(f[6]), "p4": float(f[7]),
                    "lat": float(f[8]), "lon": float(f[9]), "alt": float(f[10])})
    return out


def route_length_m(items: list[dict[str, float]]) -> float:
    """Ground length of the mission's position items (haversine), for checks."""
    pos = [(it["lat"], it["lon"]) for it in items if it["cmd"] in (NAV_WAYPOINT, NAV_VTOL_LAND) and it["seq"] > 0]
    pos = [(items[0]["lat"], items[0]["lon"])] + pos                     # from home
    R = 6371008.8
    L = 0.0
    for (a1, b1), (a2, b2) in zip(pos, pos[1:]):
        p1, p2 = math.radians(a1), math.radians(a2)
        h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b2 - b1) / 2) ** 2
        L += 2 * R * math.asin(math.sqrt(h))
    return L
