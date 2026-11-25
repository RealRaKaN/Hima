"""Triage: decide what the drone keeps for the ground station. Pure planning, no files.

event_clip (priority = the alert priority of the animal's status, 1..3):
  for every animal whose final status raises alerts (alerts.PRIORITY), each of its events
  gives a window [start - pre_s, end + post_s] clamped to the session; overlapping windows
  of the same animal are merged; a window longer than max_clip_s is cut to max_clip_s from
  its start (truncated = true). An alert-status animal with no events gets its visible span.

uncertain_detection (priority 4), each rule on its own; one item per group, with ONE still
frame (the detection with the lowest confidence in the group, earliest on ties):
  low_conf    : tracked detections with conf < new_track_thresh (they can only extend a
                track, never start one: audit B14). One group per track.
  untracked   : detections the tracker gave no id. Grouped by time: a gap > untracked_gap_s
                starts a new group. The still frame shows every untracked box of that frame.
  short_track : a track seen for less than min_track_s (last_seen - first_seen).
  id_switch   : track B starts at most switch_gap_s after track A was last seen, at most
                switch_dist_px from A's last box centre (A ended strictly before B began).
                The closest A in time (then in distance) is named in 'prev'.
"""
from __future__ import annotations

import math
from typing import Any

from hima.config import Config
from hima.onboard.alerts import PRIORITY
from hima.sources.recorder import RawDetection

UNCERTAIN_PRIORITY = 4
EPS = 1e-9


def plan_clips(doc: dict[str, Any], analysis: dict[str, Any], cfg: Config) -> list[dict[str, Any]]:
    tr = cfg.triage
    span0, span1 = doc["frames"][0]["t"], doc["frames"][-1]["t"]
    items = []
    for a in sorted(analysis["animals"], key=lambda a: a["track_id"]):
        if a["status"] not in PRIORITY:
            continue
        events = a["events"] or [{"type": "status_span", "start_s": a["first_seen_s"], "end_s": a["last_seen_s"]}]
        wins = sorted((max(span0, e["start_s"] - tr.pre_s), min(span1, e["end_s"] + tr.post_s), e["type"])
                      for e in events)
        merged: list[list[Any]] = []
        for s, e, typ in wins:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
                if typ not in merged[-1][2]:
                    merged[-1][2].append(typ)
            else:
                merged.append([s, e, [typ]])
        for s, e, types in merged:
            truncated = e - s > tr.max_clip_s + EPS
            if truncated:
                e = s + tr.max_clip_s
            items.append({"kind": "event_clip", "priority": PRIORITY[a["status"]],
                          "reason": f"{a['status']}: {', '.join(types)}",
                          "t_start": s, "t_end": e, "track_ids": [a["track_id"]],
                          "status": a["status"], "event_types": types, "truncated": truncated})
    return items


def _box(d: dict[str, Any] | RawDetection) -> dict[str, Any]:
    if isinstance(d, RawDetection):
        return {"track_id": None, "class_raw": d.class_raw, "conf": round(float(d.conf), 4),
                "bbox_xywh_px": [round(float(v), 2) for v in d.bbox_xywh_px]}
    return {"track_id": d["track_id"], "class_raw": d["class_raw"], "conf": d["conf"],
            "bbox_xywh_px": list(d["bbox_xywh_px"])}


def _item(criterion: str, reason: str, group: list[tuple[float, int, Any]], track_ids: list[int],
          sample: tuple[float, int, Any], boxes: list[dict[str, Any]], **extra) -> dict[str, Any]:
    return {"kind": "uncertain_detection", "priority": UNCERTAIN_PRIORITY, "criterion": criterion,
            "reason": reason, "t_start": group[0][0], "t_end": group[-1][0], "track_ids": track_ids,
            "count": len(group), "sample_t": sample[0], "sample_frame": sample[1], "boxes": boxes, **extra}


def _lowest_conf(group: list[tuple[float, int, Any]]) -> tuple[float, int, Any]:
    def conf(x):
        return x[2].conf if isinstance(x[2], RawDetection) else x[2]["conf"]
    return min(group, key=lambda x: (conf(x), x[0]))


def plan_uncertain(doc: dict[str, Any], cfg: Config, *, new_track_thresh: float | None,
                   untracked: list[tuple[float, RawDetection]] | None
                   ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Returns (items, skipped criteria)."""
    tr = cfg.triage
    frame_of = {fr["t"]: k for k, fr in enumerate(doc["frames"])}
    tracks: dict[int, list[tuple[float, int, dict[str, Any]]]] = {}
    for k, fr in enumerate(doc["frames"]):
        for d in fr["detections"]:
            tracks.setdefault(d["track_id"], []).append((fr["t"], k, d))
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    if new_track_thresh is None:
        skipped.append({"criterion": "low_conf", "why": "the tracker's new_track_thresh is unknown "
                        f"(tracker {doc['meta']['detector']['tracker']!r})"})
    else:
        for tid, dets in sorted(tracks.items()):
            low = [x for x in dets if x[2]["conf"] < new_track_thresh]
            if low:
                s = _lowest_conf(low)
                items.append(_item("low_conf", f"{len(low)} detection(s) of track {tid} with conf < "
                                   f"new_track_thresh {new_track_thresh:g}", low, [tid], s, [_box(s[2])]))

    for tid, dets in sorted(tracks.items()):
        dur = dets[-1][0] - dets[0][0]
        if dur < tr.min_track_s:
            s = _lowest_conf(dets)
            items.append(_item("short_track", f"track {tid} seen for {dur:.3g} s < min_track_s {tr.min_track_s:g}",
                               dets, [tid], s, [_box(s[2])]))

    for b, bd in sorted(tracks.items()):
        tb, kb, db = bd[0]
        best = None
        for a, ad in tracks.items():
            ta, _, da = ad[-1]
            gap = tb - ta
            if a == b or not 0 < gap <= tr.switch_gap_s + EPS:
                continue
            dist = math.dist(da["bbox_xywh_px"][:2], db["bbox_xywh_px"][:2])
            if dist <= tr.switch_dist_px + EPS and (best is None or (gap, dist, a) < best[:3]):
                best = (gap, dist, a, ta, da)
        if best:
            gap, dist, a, ta, da = best
            items.append(_item("id_switch", f"track {b} starts {gap:.3g} s after track {a} ended, "
                               f"{dist:.0f} px away", [bd[0]], [a, b], bd[0], [_box(db)],
                               prev={"track_id": a, "t": ta, "bbox_xywh_px": list(da["bbox_xywh_px"])}))
            items[-1]["t_start"] = ta          # the item covers the gap between the two tracks

    if untracked is None:
        skipped.append({"criterion": "untracked", "why": "detections without a track id are not stored in "
                        "the session file; only a live recording (record-video --package) has them"})
    else:
        groups: list[list[tuple[float, int, RawDetection]]] = []
        for t, d in sorted(untracked, key=lambda x: x[0]):
            if t not in frame_of:
                raise ValueError(f"untracked detection at t = {t} has no frame in the session")
            if groups and t - groups[-1][-1][0] <= tr.untracked_gap_s + EPS:
                groups[-1].append((t, frame_of[t], d))
            else:
                groups.append([(t, frame_of[t], d)])
        for g in groups:
            s = _lowest_conf(g)
            boxes = [_box(x[2]) for x in g if x[0] == s[0]]
            items.append(_item("untracked", f"{len(g)} detection(s) without a track id", g, [], s, boxes))

    items.sort(key=lambda it: (it["t_start"], it["criterion"], it["track_ids"]))
    return items, skipped
