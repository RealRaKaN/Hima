"""Time accounting, activity states, ethogram, distance travelled.

All durations use the real time between frames (no "+= 1.0 per packet").
Frame k stands for [t_k, t_{k+1}); the last frame stands for the median frame interval.
"""
from __future__ import annotations

import math
import statistics

from hima.analytics.geometry import Camera, DronePose, ground_to_pixel, gsd_m_per_px

ACTIVITIES = ("resting", "walking", "running")


def frame_durations(ts: list[float]) -> list[float]:
    if not ts:
        return []
    if len(ts) == 1:
        return [0.0]
    diffs = [b - a for a, b in zip(ts, ts[1:])]
    return diffs + [statistics.median(diffs)]


def classify(speed_mps: float | None, walk_min_mps: float, run_min_mps: float) -> str | None:
    """resting < walk_min <= walking < run_min <= running. None if speed unknown."""
    if speed_mps is None:
        return None
    if speed_mps < walk_min_mps:
        return "resting"
    if speed_mps < run_min_mps:
        return "walking"
    return "running"


def ethogram(activities: list[str | None], dts: list[float]) -> dict[str, float]:
    """Seconds per activity. Observations with unknown activity are not counted."""
    out = {a: 0.0 for a in ACTIVITIES}
    for act, dt in zip(activities, dts, strict=True):
        if act is not None:
            out[act] += dt
    return out


def path_length(xs: list[float], ys: list[float]) -> float:
    return sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in zip(xs, ys, xs[1:], ys[1:]))


def weighted_mean(values: list[float | None], weights: list[float]) -> float | None:
    pairs = [(v, w) for v, w in zip(values, weights, strict=True) if v is not None]
    total = sum(w for _, w in pairs)
    if not pairs:
        return None
    if total == 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total


def still_alone_s(ts: list[float], dts: list[float], xs: list[float], ys: list[float], alone: list[bool],
                  radius_m: float, max_gap_s: float) -> float:
    """How long the animal has been alone AND in one place, without a break, up to its last sighting.

    Walks back from the last observation while: the observation is alone, the gap to the next one is at most
    max_gap_s, and all positions of the span still fit in a box of side 2 * radius_m (it may shuffle about
    inside that box, which also absorbs position noise; it may not travel). Returns 0.0 when the last
    observation is not alone. The span covers [t_i, t_last + dt_last), like every other duration here.
    """
    n = len(ts)
    if n == 0 or not alone[-1]:
        return 0.0
    side = 2 * radius_m
    x0 = x1 = xs[-1]
    y0 = y1 = ys[-1]
    i = n - 1
    while i > 0:
        j = i - 1
        nx0, nx1, ny0, ny1 = min(x0, xs[j]), max(x1, xs[j]), min(y0, ys[j]), max(y1, ys[j])
        if not alone[j] or ts[i] - ts[j] > max_gap_s or nx1 - nx0 > side or ny1 - ny0 > side:
            break
        x0, x1, y0, y1 = nx0, nx1, ny0, ny1
        i = j
    return ts[-1] - ts[i] + dts[-1]


def visibility(ts: list[float], dts: list[float]) -> tuple[float, float]:
    """(visible_s, visibility_ratio) over the presence window [first_seen, last_seen + dt].

    Used only when camera parameters are unknown (no footprint). Known limitation: an animal
    that leaves the view and comes back counts as occluded for the time it was away.
    """
    visible = sum(dts)
    window = ts[-1] + dts[-1] - ts[0]
    return visible, (min(1.0, visible / window) if window > 0 else 1.0)


def expected_visible_s(frame_index: list[int], obs_t: list[float], obs_x: list[float], obs_y: list[float],
                       obs_speed: list[float | None], frame_ts: list[float], frame_dts: list[float],
                       poses: list[DronePose], cam: Camera, v_floor_mps: float) -> float:
    """Seconds, between first and last sighting, in which the camera SHOULD have seen the animal.

    A frame with a sighting always counts. A frame without one counts only if the animal must
    still be inside the camera footprint: it was last seen at (x, y) and since then can have
    moved at most r = max(last speed, v_floor) * time since. If that disc is not fully inside
    the image (the animal may have walked out, or the camera moved away), the frame is not
    counted, so leaving the view is not called occlusion.
    """
    seen = {k: i for i, k in enumerate(frame_index)}
    total = 0.0
    j = 0                                      # last observation at or before frame k
    for k in range(frame_index[0], frame_index[-1] + 1):
        if k in seen:
            j = seen[k]
            total += frame_dts[k]
            continue
        r = max(obs_speed[j] or 0.0, v_floor_mps) * (frame_ts[k] - obs_t[j])
        u, v = ground_to_pixel(obs_x[j], obs_y[j], cam, poses[k])
        m = r / gsd_m_per_px(poses[k].agl_m, cam.hfov_deg, cam.width_px)
        if m <= u <= cam.width_px - m and m <= v <= cam.height_px - m:
            total += frame_dts[k]
    return total


def hidden_spans(frame_index: list[int], obs_t: list[float], obs_x: list[float], obs_y: list[float],
                 obs_speed: list[float | None], frame_ts: list[float], frame_dts: list[float],
                 poses: list[DronePose], cam: Camera, v_floor_mps: float, min_s: float,
                 margin_px: float, body_m: float = 0.0) -> list[dict]:
    """Times the animal was hidden under cover: not seen although its place was in the camera view.

    For every stretch without a sighting, count the frames in which the camera was looking at where the animal
    must have been:
      - it came back (a sighting after the stretch): its place at time t is on the straight line from where it
        was lost to where it came back; that place must lie margin_px inside the image;
      - it has not come back (the stretch runs to the last frame): its last place, grown by
        max(last speed, v_floor) x time since (it may have walked away), must lie margin_px inside the image.
    The margin also holds its whole body (body_m, an adult's length): an animal partly out of the image is
    often not detected, and that is not cover (long1: lost at the edge while the drone flew off).
    A stretch with at least min_s of such time is a hidden span. Shorter ones are missed detections (the detector
    loses a box for a frame or two in the open, and the tracker bridges up to 3 s), not hiding. Leaving the view
    is never hiding. The rule does not know what covers the animal (a tree, a dune, a shadow): from straight
    above the camera cannot tell; the span's place goes to the report.
    Limit: a detector that misses an animal in the open for longer than min_s (too small, bad light) looks the
    same as cover.
    """
    out = []
    n = len(frame_index)
    last_k = len(frame_ts) - 1
    for i in range(n):
        k0 = frame_index[i]
        back = i + 1 < n
        k1 = frame_index[i + 1] if back else last_k + 1
        if k1 - k0 <= 1:
            continue
        in_view = 0.0
        for k in range(k0 + 1, k1):
            g = gsd_m_per_px(poses[k].agl_m, cam.hfov_deg, cam.width_px)
            if back:
                f = (frame_ts[k] - obs_t[i]) / (obs_t[i + 1] - obs_t[i])
                x = obs_x[i] + f * (obs_x[i + 1] - obs_x[i])
                y = obs_y[i] + f * (obs_y[i + 1] - obs_y[i])
                m = margin_px + body_m / g
            else:
                x, y = obs_x[i], obs_y[i]
                m = margin_px + (body_m + max(obs_speed[i] or 0.0, v_floor_mps) * (frame_ts[k] - obs_t[i])) / g
            u, v = ground_to_pixel(x, y, cam, poses[k])
            if m <= u <= cam.width_px - m and m <= v <= cam.height_px - m:
                in_view += frame_dts[k]
        if in_view >= min_s - 1e-9:
            end = obs_t[i + 1] if back else frame_ts[last_k] + frame_dts[last_k]
            out.append({"start_s": round(obs_t[i], 4), "end_s": round(end, 4), "in_view_s": round(in_view, 4),
                        "x_m": round(obs_x[i], 3), "y_m": round(obs_y[i], 3), "open": not back})
    return out
