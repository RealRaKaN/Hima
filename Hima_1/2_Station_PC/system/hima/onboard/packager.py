"""Build the mission package the drone hands to the ground station.

Layout of <out>/<mission_id>/:
  package.json          manifest (contract/package.schema.json)
  session.json          the full session file (always kept, priority 0)
  report.json           the landing report (analysis only): LLM, LLM with template sentences, or fixed template (priority 0)
  clips/clip-NNN.mp4    event clips      (only when a video is given)
  uncertain/unc-NNN.jpg still frames     (only when a video is given)
  flight_log.json       flight log   (extras["flight_log"]; priority 0, always kept)
  images/img-NNN.jpg    onboard images         with boxes (extras["images"]): vehicle alert images
                        priority 1, camera-window images priority 2 (images reach the ground after landing)
  no_position/np-NNN.jpg      images saved without a position from the Pixhawk, priority 1
  vehicle_follow/vf-NNN.jpg   one image every 3 s while following a vehicle near a herd, priority 0
  raw_flight.jsonl.gz         the raw flight file, no analysis (extras["raw_flight"]), priority 0
  no_position_analysis.json   the analysis without a position (extras["no_position_analysis"]), priority 0

The package is built in a temporary folder next to the target and renamed into place only
after the manifest is valid and every file was re-hashed (verify_package_dir), so a failed
run never leaves a half package. An existing package is never overwritten.

Storage budget: if the stored files exceed [triage].storage_budget_mb, items are dropped
in this order until they fit: highest priority number first, then the largest file, then
the latest id. The session item is never dropped (over_budget = true if it alone does not
fit); neither is the report. A dropped item stays in the manifest with its size, hash and drop_reason.
"""
from __future__ import annotations

import importlib.util
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hima.analytics.analyze import analyze
from hima.config import Config
from hima.contract.io import SessionInvalid, save_session, validate_session, write_json_atomic
from hima.contract.package import MANIFEST, sha256_file, validate_package, verify_package_dir
from hima.onboard import media
from hima.onboard.report import GenerateFn, make_report, mission_summary
from hima.onboard.triage import plan_clips, plan_uncertain
from hima.sources.recorder import RawDetection

SESSION_FILE = "session.json"
REPORT_FILE = "report.json"
FLIGHT_LOG_FILE = "flight_log.json"
RAW_FLIGHT_FILE = "raw_flight.jsonl.gz"
NO_POSITION_FILE = "no_position_analysis.json"
IMAGE_PRIORITY = {"vehicle_follow": 0, "vehicle_alert": 1, "no_position": 1, "window": 2}
IMAGE_FOLDER = {"no_position": ("np", "no_position"), "vehicle_follow": ("vf", "vehicle_follow")}


def tracker_new_track_thresh(tracker: str) -> tuple[float | None, str | None]:
    """new_track_thresh from the tracker YAML the session was recorded with: a path, or a name
    shipped with Ultralytics (ultralytics/cfg/trackers/). (None, None) if there is no such file."""
    candidates = [Path(tracker)]
    spec = importlib.util.find_spec("ultralytics")
    if spec and spec.submodule_search_locations:
        candidates.append(Path(spec.submodule_search_locations[0]) / "cfg" / "trackers" / tracker)
    for p in candidates:
        if p.suffix in (".yaml", ".yml") and p.is_file():
            v = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("new_track_thresh")
            if v is not None:
                return float(v), str(p)
    return None, None


def _budget(items: list[dict[str, Any]], limit: int, folder: Path) -> tuple[int, bool]:
    def used() -> int:
        return sum(it["file"]["bytes"] for it in items if it["state"] == "stored")
    order = sorted((it for it in items if it["state"] == "stored" and it["priority"] > 0),
                   key=lambda it: (-it["priority"], -it["file"]["bytes"], [-ord(c) for c in it["id"]]))
    for it in order:
        if used() <= limit:
            break
        (folder / it["file"]["path"]).unlink()
        it["drop_reason"] = (f"storage budget {limit} bytes exceeded; priority {it['priority']} "
                             f"is among the lowest still stored")
        it["file"]["path"] = None
        it["state"] = "dropped"
    return used(), used() > limit


def _file(tmp: Path, rel: str) -> dict[str, Any]:
    return {"path": rel, "bytes": (tmp / rel).stat().st_size, "sha256": sha256_file(tmp / rel)}


def _extras(extras: dict[str, Any], tmp: Path, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    if extras.get("flight_log") is not None:
        write_json_atomic(extras["flight_log"], tmp / FLIGHT_LOG_FILE)
        out.append({"id": "flightlog", "kind": "flight_log", "priority": 0, "state": "stored",
                    "reason": "onboard flight log: alerts, object samples, periodic analyses, camera-window "
                              "access log, AI calls, link deliveries, telemetry",
                    "t_start": frames[0]["t"], "t_end": frames[-1]["t"], "track_ids": [],
                    "file": _file(tmp, FLIGHT_LOG_FILE)})
    if extras.get("raw_flight") is not None:
        shutil.copyfile(extras["raw_flight"], tmp / RAW_FLIGHT_FILE)
        out.append({"id": "raw", "kind": "raw_flight", "priority": 0, "state": "stored",
                    "reason": "raw flight file: every frame with its pose (or none), every box of both models, the "
                              "messages sent; no analysis",
                    "t_start": frames[0]["t"], "t_end": frames[-1]["t"], "track_ids": [],
                    "file": _file(tmp, RAW_FLIGHT_FILE)})
    if extras.get("no_position_analysis") is not None:
        write_json_atomic(extras["no_position_analysis"], tmp / NO_POSITION_FILE)
        out.append({"id": "nopos", "kind": "no_position_analysis", "priority": 0, "state": "stored",
                    "reason": "analysis of the frames without a position from the Pixhawk (species, tracks, how many "
                              "together, how long; no places)",
                    "t_start": frames[0]["t"], "t_end": frames[-1]["t"], "track_ids": [],
                    "file": _file(tmp, NO_POSITION_FILE)})
    imgs = extras.get("images") or []
    counter = {"img": 0, "np": 0, "vf": 0}
    for im in sorted(imgs, key=lambda i: (i["t"], i["purpose"])):
        # no-position images have their own folder: frames saved while the Pixhawk gave no position
        pre, folder = IMAGE_FOLDER.get(im["purpose"], ("img", "images"))
        counter[pre] += 1
        n = counter[pre]
        rel = f"{folder}/{pre}-{n:03d}.jpg"
        (tmp / folder).mkdir(exist_ok=True)
        if "path" in im:
            shutil.copyfile(im["path"], tmp / rel)
        else:
            (tmp / rel).write_bytes(im["jpeg"])
        why = {"vehicle_alert": lambda: "vehicle alert " + im["alert"],
               "window": lambda: f"camera window {im['window']} image (shown to the AI) for {im['alert']}",
               "no_position": lambda: "camera image without a position from the Pixhawk: " + im["alert"],
               "vehicle_follow": lambda: "camera image while " + im["alert"]}[im["purpose"]]()
        out.append({"id": f"{pre}-{n:03d}", "kind": "flight_image", "priority": IMAGE_PRIORITY[im["purpose"]],
                    "reason": why, "state": "stored", "t_start": im["t"], "t_end": im["t"],
                    "track_ids": sorted(set(im.get("track_ids") or [])), "purpose": im["purpose"],
                    "sample_t": im["t"], "alert": im["alert"], "window": im["window"],
                    "file": _file(tmp, rel)})
    return out


def build_package(doc: dict[str, Any], cfg: Config, out: str | Path, *, video: str | Path | None = None,
                  untracked: list[tuple[float, RawDetection]] | None = None,
                  created_at: datetime | None = None, use_llm: bool = False,
                  generate_fn: GenerateFn | None = None,
                  extras: dict[str, Any] | None = None, video_timing: str = "fps") -> tuple[Path, dict[str, Any]]:
    """video_timing: "fps" (a video file: frame k is at k / fps) or "frames" (a live flight: one video frame per
    session frame, at the session's own times; media.check_video_is_session_frames).
    extras (optional): {"flight_log": dict, "images": [{"purpose": "vehicle_alert"|"window",
    "t": float, "jpeg": bytes, "alert": str, "window": int|None, "track_ids": [int]}],
    "raw_flight": path of the compressed raw file, "no_position_analysis": dict}."""
    errors = validate_session(doc)
    if errors:
        raise SessionInvalid(errors)
    if "analysis" in doc:
        if doc["analysis"]["config_used"]["sha256"] != cfg.sha256:
            raise ValueError("the session's analysis was made with a different hima.toml; "
                             "run analyze again with this config first")
    else:
        doc = {**doc, "analysis": analyze(doc, cfg)}
    analysis = doc["analysis"]
    mission = doc["meta"]["mission_id"]
    final = Path(out) / mission
    if final.exists():
        raise FileExistsError(f"{final} already exists; a package is never overwritten")
    final.parent.mkdir(parents=True, exist_ok=True)

    info = None
    if video is not None:
        info = media.video_info(video)
        if video_timing == "frames":
            media.check_video_is_session_frames(doc, video, info)
        elif video_timing == "fps":
            media.check_session_matches_video(doc, info)
        else:
            raise ValueError(f"video_timing must be 'fps' or 'frames', got {video_timing!r}")
    thresh, thresh_src = tracker_new_track_thresh(doc["meta"]["detector"]["tracker"])

    tmp = Path(tempfile.mkdtemp(prefix=f".{mission}.", suffix=".tmp", dir=final.parent))
    try:
        save_session(doc, tmp / SESSION_FILE)
        frames = doc["frames"]
        items: list[dict[str, Any]] = [{
            "id": "session", "kind": "session", "priority": 0, "reason": "full session file",
            "state": "stored", "t_start": frames[0]["t"], "t_end": frames[-1]["t"],
            "track_ids": sorted({d["track_id"] for f in frames for d in f["detections"]}),
            "n_frames": len(frames), "file": None}]

        clips = plan_clips(doc, analysis, cfg)
        unc, skipped = plan_uncertain(doc, cfg, new_track_thresh=thresh, untracked=untracked)
        cut_clips, cut_stills = {}, {}
        for n, it in enumerate(clips, 1):
            it["id"] = f"clip-{n:03d}"
            it["frame_first"] = it["frame_last"] = None
            if info:
                a, b = (media.frame_range_by_times([f["t"] for f in frames], it["t_start"], it["t_end"])
                        if video_timing == "frames" else media.frame_range(it["t_start"], it["t_end"], info.fps))
                it["frame_first"], it["frame_last"] = a, b
                cut_clips[f"clips/{it['id']}.mp4"] = (a, b)
        for n, it in enumerate(unc, 1):
            it["id"] = f"unc-{n:03d}"
            if info:
                cut_stills[f"uncertain/{it['id']}.jpg"] = it["sample_frame"]
        if info:
            media.extract(video, tmp, clips=cut_clips, stills=cut_stills, fps=info.fps,
                          fourcc=cfg.triage.clip_fourcc, jpeg_quality=cfg.triage.jpeg_quality)

        for it in clips + unc:
            ext = "mp4" if it["kind"] == "event_clip" else "jpg"
            folder = "clips" if it["kind"] == "event_clip" else "uncertain"
            it["file"], it["state"] = None, "no_media"
            if info:
                rel = f"{folder}/{it['id']}.{ext}"
                it["file"], it["state"] = {"path": rel, "bytes": (tmp / rel).stat().st_size,
                                           "sha256": sha256_file(tmp / rel)}, "stored"
        items[0]["file"] = {"path": SESSION_FILE, "bytes": (tmp / SESSION_FILE).stat().st_size,
                            "sha256": sha256_file(tmp / SESSION_FILE)}
        summary = mission_summary(doc, analysis, (extras or {}).get("flight_log"), cfg.behaviour.herd_link_m,
                                  cfg.behaviour.vehicle_near_herd_m, cfg.behaviour.vehicle_close_m)
        report = make_report(summary, cfg, use_llm=use_llm, generate_fn=generate_fn)
        write_json_atomic(report, tmp / REPORT_FILE)
        items.append({"id": "report", "kind": "report", "priority": 0, "source": report["source"],
                      "reason": "landing report" + {
                          "llm": "",
                          "llm_repaired": " (template sentences for: " + ", ".join(report["gate"]["repaired"]) + ")",
                          "template": " (fixed template: " + "; ".join(report["gate"]["reasons"])[:200] + ")"}[report["source"]],
                      "state": "stored", "t_start": frames[0]["t"], "t_end": frames[-1]["t"],
                      "track_ids": summary["alert_track_ids"],
                      "file": {"path": REPORT_FILE, "bytes": (tmp / REPORT_FILE).stat().st_size,
                               "sha256": sha256_file(tmp / REPORT_FILE)}})
        items += clips + unc
        items += _extras(extras or {}, tmp, frames)

        limit = int(cfg.triage.storage_budget_mb * 1_000_000)
        used, over = _budget(items, limit, tmp)
        manifest = {
            "schema_version": "hima.package/1", "mission_id": mission,
            "created_at": (created_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_sha256": items[0]["file"]["sha256"],
            "config_used": {"sha256": cfg.sha256, "values": cfg.values()},
            "criteria": {"new_track_thresh": thresh, "new_track_thresh_source": thresh_src,
                         "untracked_available": untracked is not None, "skipped": skipped},
            "media": None if info is None else {"video": Path(video).name, "fps": info.fps,
                                                "width_px": info.width_px, "height_px": info.height_px,
                                                "clip_fourcc": cfg.triage.clip_fourcc},
            "budget": {"limit_bytes": limit, "used_bytes": used,
                       "dropped_count": sum(it["state"] == "dropped" for it in items), "over_budget": over},
            "items": items,
        }
        errs = validate_package(manifest)
        if errs:
            raise ValueError("package manifest is invalid:\n  " + "\n  ".join(map(str, errs[:5])))
        write_json_atomic(manifest, tmp / MANIFEST)
        errs = verify_package_dir(tmp)
        if errs:
            raise ValueError("package folder does not match its manifest:\n  " + "\n  ".join(map(str, errs[:5])))
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final, manifest
