"""Build the session's "analysis" section from meta + frames only.

ground_truth is never read here: analyze() receives only meta and frames.
"""
from __future__ import annotations

import bisect
import math
import statistics
from typing import Any

from hima.analytics import behavior, events, herd, identity, tracks
from hima.analytics.geometry import Camera, DronePose, ground_to_pixel
from hima.analytics.status import StatusInputs, classify_status
from hima.config import Config
from hima.telemetry.geo import to_latlon

DECIMALS = 4
EPS = 1e-9


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, DECIMALS)


def _ratio(part: float, whole: float) -> float:
    return min(1.0, part / whole) if whole > 0 else 0.0


def analyze(doc: dict[str, Any], cfg: Config) -> dict[str, Any]:
    return analyze_frames(doc["meta"], doc["frames"], cfg)


def analyze_frames(meta: dict[str, Any], frames: list[dict[str, Any]], cfg: Config) -> dict[str, Any]:
    metric = bool(meta["camera"]["params_known"])
    cam = Camera.from_meta(meta["camera"])
    ts = [f["t"] for f in frames]
    dts = behavior.frame_durations(ts)
    # Only registered animals (seen >= identity.confirm_s, part boxes dropped, lost tracks joined).
    resolved, ident = identity.resolve(meta, frames, cfg)
    track_map, per_frame = tracks.build_tracks(resolved, dts, cam, metric)

    # Herd per frame: isolation flags and the sampled timeline.
    isolated_at: set[tuple[int, int]] = set()          # (track_id, frame_index)
    timeline = []
    next_sample = ts[0] if ts else 0.0
    memory = cfg.herd.neighbour_memory_s
    last_pos: dict[int, tuple[float, float, float]] = {}   # track -> (t, x, y) when last seen
    was_iso: dict[int, bool] = {}                            # track -> isolated when last judged
    for k, row in enumerate(per_frame):
        pts = {tid: (o.x_m, o.y_m) for tid, o in row} if metric else {}
        if metric and memory > 0:
            known = pts | _remembered(last_pos, pts, ts[k], memory, cam, frames[k])
            if len(known) < 2:
                # Nobody to measure against: an animal that was isolated stays isolated until a neighbour
                # is seen or remembered; one never judged isolated stays unknown (not isolated).
                iso = {tid for tid in pts if was_iso.get(tid, False)}
            else:
                iso = herd.isolated_ids(known, cfg.events.isolation_m) & set(pts)
            was_iso.update((tid, tid in iso) for tid in pts)
            last_pos.update((tid, (ts[k], o.x_m, o.y_m)) for tid, o in row)
        else:
            iso = herd.isolated_ids(pts, cfg.events.isolation_m)
        isolated_at.update((tid, k) for tid in iso)
        if ts[k] >= next_sample - EPS:
            timeline.append({"t": _r(ts[k]), "count": len(row),
                             "cohesion_m": _r(herd.cohesion_m(pts)) if metric else None,
                             "isolated_ids": sorted(iso)})
            while next_sample <= ts[k] + EPS:
                next_sample += cfg.herd.timeline_step_s

    poses = [DronePose.from_frame(f["drone"]) for f in frames]
    scene = (ts, dts, poses, cam)
    animals = [_animal(tid, track_map[tid], isolated_at, metric, cfg, scene) for tid in sorted(track_map)]
    reg = ident.registered()
    home = meta.get("home")
    for a in animals:
        an = reg[a["track_id"]]
        sg = dict(an.sighting)
        sg["lat_deg"] = sg["lon_deg"] = None
        if home is not None and sg["x_m"] is not None:
            lat, lon = to_latlon(sg["x_m"], sg["y_m"], home["lat_deg"], home["lon_deg"])
            sg["lat_deg"], sg["lon_deg"] = round(lat, 7), round(lon, 7)
        a["sighting"] = sg
        a["segments"] = [{"tracker_id": s[0], "start_s": _r(s[1]), "end_s": _r(s[2])} for s in an.segments]
    horiz, d3 = herd.drone_distance_m([(f["drone"]["x_m"], f["drone"]["y_m"], f["drone"]["agl_m"])
                                       for f in frames])
    return {
        "config_used": {"sha256": cfg.sha256, "values": cfg.values()},
        # Without known parameters the drone pose is a placeholder: no distance is claimed.
        "drone": {"duration_s": _r(sum(dts)), "distance_flown_m": _r(horiz) if metric else None,
                  "distance_flown_3d_m": _r(d3) if metric else None},
        "animals": animals,
        "herd_timeline": timeline,
        "identity": ident.summary(),
    }


def _remembered(last_pos: dict[int, tuple[float, float, float]], here: dict[int, tuple[float, float]], t: float,
                memory_s: float, cam: Camera, frame: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Neighbours the camera cannot see now: tracks seen in the last memory_s at a last position that
    is OUTSIDE the current image. A remembered position inside the image is ignored: what is there is what
    the camera sees now (the animal left, or it is the same animal under a new track id)."""
    pose = DronePose.from_frame(frame["drone"])
    out = {}
    for tid, (tl, x, y) in last_pos.items():
        if tid in here or t - tl > memory_s + EPS:
            continue
        u, v = ground_to_pixel(x, y, cam, pose)
        if not (0 <= u <= cam.width_px and 0 <= v <= cam.height_px):
            out[tid] = (x, y)
    return out


def _drone_closing_m(obs: list[tracks.Observation], start: float, frame_ts: list[float],
                     poses: list[DronePose], approach_s: float, speed_window_s: float) -> float:
    """How much closer (slant, metres) the drone came, over the approach_s before the run started, to the place
    where the animal stood before it ran. Positive: the drone approached. The run is flagged once the speed
    over speed_window_s is high, so the animal had already moved: "where it stood" is its first sighting at or after
    start - speed_window_s. The window is shorter when the flight began later than start - approach_s."""
    o = next(o for o in obs if o.t >= start - speed_window_s - EPS)
    before = poses[bisect.bisect_left(frame_ts, start - approach_s - EPS)]
    now = poses[bisect.bisect_left(frame_ts, start - EPS)]

    def slant(p: DronePose) -> float:
        return math.sqrt((p.x_m - o.x_m) ** 2 + (p.y_m - o.y_m) ** 2 + p.agl_m ** 2)
    return slant(before) - slant(now)


def _animal(tid: int, tr: tracks.Track, isolated_at: set[tuple[int, int]], metric: bool,
            cfg: Config, scene: tuple[list[float], list[float], list[DronePose], Camera]) -> dict[str, Any]:
    obs = tr.observations
    ts = [o.t for o in obs]
    dts = [o.dt for o in obs]
    visible, vis_ratio = behavior.visibility(ts, dts)
    out: dict[str, Any] = {
        "track_id": tid, "species": tr.species,
        "first_seen_s": _r(ts[0]), "last_seen_s": _r(ts[-1]),
        "visible_s": _r(visible), "visibility_ratio": _r(vis_ratio),
    }

    if not metric:
        out.update(ethogram_s=None, distance_travelled_m=None, max_ground_speed_mps=None,
                   mean_ground_speed_mps=None, min_slant_distance_to_drone_m=None, events=[],
                   isolation_ratio=None, still_alone_s=None, body_length_m=None,
                   stress_start_s=None, drone_closing_m=None, hidden=[], hidden_s=None,
                   ratios={k: None for k in ("stress", "proximity", "resting", "walking", "running")})
        out["status"] = classify_status(StatusInputs(False, vis_ratio), cfg.status)
        out["metrics_valid"] = False
        return out

    xs = [o.x_m for o in obs]
    ys = [o.y_m for o in obs]
    slants = [o.slant_m for o in obs]
    speeds = tracks.ground_speeds(ts, xs, ys, cfg.geometry.speed_window_s)
    frame_ts, frame_dts, poses, cam = scene
    expected = behavior.expected_visible_s([o.frame_index for o in obs], ts, xs, ys, speeds,
                                           frame_ts, frame_dts, poses, cam, cfg.activity.walk_min_mps)
    vis_ratio = _ratio(visible, expected) if expected > 0 else 1.0
    out["visibility_ratio"] = _r(vis_ratio)
    acts = [behavior.classify(s, cfg.activity.walk_min_mps, cfg.activity.run_min_mps) for s in speeds]
    eth = behavior.ethogram(acts, dts)

    ev_cfg = dict(min_event_s=cfg.events.min_event_s, merge_gap_s=cfg.events.merge_gap_s)
    flags = {
        "proximity": [s < cfg.events.proximity_m for s in slants],
        "stress": [a == "running" and s < cfg.events.stress_m for a, s in zip(acts, slants)],
        "isolation": [(tid, o.frame_index) in isolated_at for o in obs],
    }
    intervals = {name: events.flag_intervals(ts, dts, f, **ev_cfg) for name, f in flags.items()}
    ev_list = sorted(
        ({"type": name, "start_s": _r(iv.start), "end_s": _r(iv.end), "duration_s": _r(iv.duration)}
         for name, ivs in intervals.items() for iv in ivs),
        key=lambda e: (e["start_s"], e["type"]))

    ratios = {
        "stress": _ratio(events.total_duration(intervals["stress"]), visible),
        "proximity": _ratio(events.total_duration(intervals["proximity"]), visible),
        "resting": _ratio(eth["resting"], visible),
        "walking": _ratio(eth["walking"], visible),
        "running": _ratio(eth["running"], visible),
    }
    iso_ratio = _ratio(events.total_duration(intervals["isolation"]), visible)
    known = [s for s in speeds if s is not None]
    hidden = behavior.hidden_spans([o.frame_index for o in obs], ts, xs, ys, speeds, frame_ts, frame_dts, poses,
                                   cam, cfg.activity.walk_min_mps, cfg.hiding.span_min_s, cfg.hiding.margin_px,
                                   cfg.young.adult_m(tr.species))
    hidden_s = sum(h["in_view_s"] for h in hidden)
    still = behavior.still_alone_s(ts, dts, xs, ys, flags["isolation"], cfg.injury.still_radius_m,
                                   cfg.injury.max_gap_s)
    lengths = [o.length_m for o in obs if o.length_m is not None]
    body = statistics.median(lengths) if len(lengths) >= cfg.young.min_samples else None
    small = body is not None and body <= cfg.young.max_ratio * cfg.young.adult_m(tr.species) + EPS
    stress_start = closing = None
    if intervals["stress"]:
        stress_start = intervals["stress"][-1].start
        closing = _drone_closing_m(obs, stress_start, frame_ts, poses, cfg.cause.approach_s,
                                   cfg.geometry.speed_window_s)

    out.update(
        ethogram_s={k: _r(v) for k, v in eth.items()},
        distance_travelled_m=_r(behavior.path_length(xs, ys)),
        max_ground_speed_mps=_r(max(known)) if known else None,
        mean_ground_speed_mps=_r(behavior.weighted_mean(speeds, dts)),
        min_slant_distance_to_drone_m=_r(min(slants)),
        events=ev_list,
        isolation_ratio=_r(iso_ratio),
        still_alone_s=_r(still),
        body_length_m=_r(body),
        stress_start_s=_r(stress_start),
        drone_closing_m=_r(closing),
        hidden=hidden,
        hidden_s=_r(hidden_s),
        ratios={k: _r(v) for k, v in ratios.items()},
        status=classify_status(StatusInputs(True, vis_ratio, ratios["stress"], iso_ratio,
                                            ratios["resting"], ratios["running"], ratios["proximity"],
                                            still, small, hidden_s),
                               cfg.status, cfg.injury.stage1_s, cfg.hiding.min_s),
        metrics_valid=True,
    )
    return out
