"""One complete simulated mission, from an empty folder to the ground dashboard.

  1. Synthetic flight: each frame is released at its mission time (speed 1 = real time) and
     goes through the onboard loop, the live alerts and the text link. At landing the final
     analysis is compared with the batch `analyze` of the same frames, then the package is
     built (landing report by the LLM, checked by the judge and the fixed rules).
  2. Video flight: each frame of the video is read, tracked (YOLO + ByteTrack) and pushed into
     the same onboard loop as it arrives, paced at the video's frame rate. Landing: package with
     event clips and still frames of the uncertain detections.
  3. Ground station: ingest both packages, cross-mission statistics, review export.

Measured per frame (wall clock): source_s = time to get the frame (synthetic: ~0, video: read +
YOLO + tracker + record), onboard_s = loop.push (includes the analysis refresh every update_s
and the alerts), lag_s = when the frame was done minus when it was released (paced runs).
real_time_factor = busy time / mission time: below 1 means the PC keeps up.
ALL numbers are for THIS PC. They say nothing about the drone computer until measured there.
"""
from __future__ import annotations

import csv
import json
import math
import platform
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from hima.analytics.analyze import analyze
from hima.config import Config
from hima.contract.io import save_session
from hima.ground.archive import ingest, load_index, verify_archive
from hima.ground.review import export_review
from hima.ground.stats import mission_stats
from hima.onboard.flight import OnboardFlight
from hima.onboard.packager import build_package

METRICS_FILE, FRAMES_FILE = "e2e_metrics.json", "frame_times.csv"
PC_NOTE = "Measured on this PC; says nothing about the drone computer until measured there."

FrameSource = Iterable[tuple[float, Callable[[], dict[str, Any] | None]]]


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile (no interpolation): the smallest value with >= q% at or below it."""
    if not xs:
        raise ValueError("no values")
    s = sorted(xs)
    return s[max(0, math.ceil(q / 100 * len(s)) - 1)]


def dist(xs: list[float]) -> dict[str, float] | None:
    if not xs:
        return None
    return {"n": len(xs), "p50": round(pct(xs, 50), 6), "p95": round(pct(xs, 95), 6), "max": round(max(xs), 6)}


@dataclass(frozen=True)
class FrameTime:
    t: float
    source_s: float
    onboard_s: float
    lag_s: float          # done minus release (fast mode: release = start, so lag = processing time)


@dataclass
class LiveFlight:
    flight: OnboardFlight
    final: dict[str, Any]
    frames: list[FrameTime] = field(default_factory=list)
    alert_wall_s: dict[int, float] = field(default_factory=dict)   # alert seq -> frame release to queued
    wall_s: float = 0.0
    paced: bool = False


def fly_live(meta: dict[str, Any], frames: FrameSource, cfg: Config, *, speed: float | None,
             clock: Callable[[], float] = time.perf_counter,
             sleep: Callable[[float], None] = time.sleep) -> LiveFlight:
    """Release each frame at start + (t - t_first) / speed, produce it, push it onboard, time it.
    speed None/0: no waiting (as fast as possible)."""
    if speed is not None and speed < 0:
        raise ValueError("speed must be >= 0")
    paced = bool(speed)
    flight = OnboardFlight(meta, cfg, clock=clock)
    out = LiveFlight(flight, {}, paced=paced)
    start = clock()
    t_first = None
    for t, produce in frames:
        t_first = t if t_first is None else t_first
        if paced:
            release = start + (t - t_first) / speed
            wait = release - clock()
            if wait > 0:
                sleep(wait)
        else:
            release = clock()
        a = clock()
        frame = produce()
        b = clock()
        if frame is None:
            break
        if abs(frame["t"] - t) > 1e-9:
            raise ValueError(f"frame t = {frame['t']} but it was scheduled for t = {t}")
        n = len(flight.alerts)
        flight.push(frame)
        c = clock()
        for al in flight.alerts[n:]:
            out.alert_wall_s[al.seq] = c - release      # upper bound: end of this frame's processing
        out.frames.append(FrameTime(t, b - a, c - b, c - release))
    out.wall_s = clock() - start
    out.final = flight.land()
    return out


def video_frames(src: Any, max_frames: int | None = None) -> Iterator[tuple[float, Callable]]:
    k = 0
    while max_frames is None or k < max_frames:
        yield k / src.fps, src.next_frame
        k += 1


def _gpu() -> dict[str, Any] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        line = subprocess.run([exe, "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        name, used, total = (x.strip() for x in line.split(","))
        return {"name": name, "memory_used_mib": int(used), "memory_total_mib": int(total)}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _status_since(loop, track_id: int, status: str, t: float) -> float | None:
    since = None
    for c in loop.changes:
        if c.track_id == track_id and c.t <= t + 1e-9:
            since = c.t if c.new == status else None
    return since


def flight_metrics(live: LiveFlight, doc: dict[str, Any], cfg: Config, speed: float | None) -> dict[str, Any]:
    fr = live.frames
    ts = [f.t for f in fr]
    span = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    interval = statistics.median(b - a for a, b in zip(ts, ts[1:])) if len(ts) > 1 else None
    total = [f.source_s + f.onboard_s for f in fr]
    loop = live.flight.loop
    cs = [s.compute_s for s in loop.snapshots]
    deliveries = {d.alert.seq: d for d in live.flight.deliveries}
    alerts = []
    for a in live.flight.alerts:
        d = deliveries.get(a.seq)
        alerts.append({
            "text": a.text, "track_id": a.track_id, "status": a.status,
            "first_seen_s": a.first_seen_s, "status_since_s": _status_since(loop, a.track_id, a.status, a.t),
            "raised_t": a.t, "delivered_t": None if d is None else round(d.delivered_t, 3),
            "link_s": None if d is None else round(d.delivered_t - d.queued_t, 3),
            "seen_to_delivered_s": None if d is None else round(d.delivered_t - a.first_seen_s, 3),
            "bytes": None if d is None else d.bytes,
            "wall_lag_s": round(live.alert_wall_s[a.seq], 6),
        })
    batch = analyze({k: v for k, v in doc.items() if k != "analysis"}, cfg)
    return {
        "mission_id": doc["meta"]["mission_id"], "source": doc["meta"]["source"],
        "params_known": doc["meta"]["camera"]["params_known"],
        "frames": len(fr), "mission_s": round(span, 3),
        "frame_interval_s": None if interval is None else round(interval, 6),
        "speed": speed, "wall_s": round(live.wall_s, 3),
        "frame_s": dist(total), "first_frame_s": round(total[0], 6) if total else None,
        "source_s": dist([f.source_s for f in fr]), "onboard_s": dist([f.onboard_s for f in fr]),
        "real_time_factor": round(sum(total) / span, 4) if span else None,
        # without frame 0 (model warm-up) over the mission time from frame 0 to the last frame
        "real_time_factor_after_first": round(sum(total[1:]) / span, 4) if span else None,
        "max_lag_s": round(max(f.lag_s for f in fr), 6) if live.paced and fr else None,
        # late = done after the NEXT frame was released (wall gap between releases = interval / speed)
        "late_frames": (sum(f.lag_s > interval / speed for f in fr) if live.paced and interval else None),
        "refreshes": len(cs), "refresh_s": dist(cs), "last_refresh_s": round(cs[-1], 6) if cs else None,
        "status_changes": len(loop.changes),
        "alerts": alerts, "link_bytes": sum(d.bytes for d in live.flight.deliveries),
        "live_equals_batch": live.final == batch,
    }


def _landing(doc: dict[str, Any], live: LiveFlight, cfg: Config, out: Path, *, video, untracked,
             use_llm: bool, generate_fn, clock) -> dict[str, Any]:
    """Final analysis is the live one; save the session, build the package, time it."""
    doc = {**doc, "analysis": live.final}
    sess = out / "sessions" / f"{doc['meta']['mission_id']}.session.json"
    save_session(doc, sess)
    t0 = clock()
    path, m = build_package(doc, cfg, out / "packages", video=video, untracked=untracked,
                            use_llm=use_llm, generate_fn=generate_fn)
    package_s = clock() - t0
    rep = json.loads((path / "report.json").read_text(encoding="utf-8"))
    kinds: dict[str, int] = {}
    for it in m["items"]:
        key = f"{it['kind']}:{it['state']}"
        kinds[key] = kinds.get(key, 0) + 1
    return {
        "session_file": str(sess), "session_bytes": sess.stat().st_size,
        "package_dir": str(path), "package_s": round(package_s, 3),
        "package": {"bytes_on_disk": sum(f.stat().st_size for f in path.rglob("*") if f.is_file()),
                    "used_bytes": m["budget"]["used_bytes"], "limit_bytes": m["budget"]["limit_bytes"],
                    "dropped": m["budget"]["dropped_count"], "over_budget": m["budget"]["over_budget"],
                    "items": dict(sorted(kinds.items()))},
        "report": {"source": rep["source"], "model": rep["model"], "judge_model": rep.get("judge_model"),
                   "latency_s": rep["latency_s"], "writer_latency_s": rep.get("writer_latency_s"),
                   "judge_latency_s": rep.get("judge_latency_s"), "repaired": rep["gate"]["repaired"],
                   "reasons": rep["gate"]["reasons"], "headline": rep["report"]["headline"]},
    }


def run_e2e(out: str | Path, cfg: Config, *, seed: int = 42, duration_s: float = 1800.0, step_s: float = 1.0,
            speed: float | None = 1.0, video: str | Path | None = None, max_frames: int | None = None,
            weights: str | None = None, model: Any = None, use_llm: bool = True, generate_fn=None,
            clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep,
            log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    from hima.sources.synthetic import generate
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; the end-to-end run starts from an empty folder")
    out.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {
        "schema_version": "hima.e2e/1",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_sha256": cfg.sha256, "speed": speed, "use_llm": use_llm, "note": PC_NOTE,
        "machine": {"platform": platform.platform(), "python": platform.python_version(),
                    "processor": platform.processor(), "gpu_at_start": _gpu()},
        "flights": [],
    }
    rows: list[tuple[str, FrameTime]] = []

    # 1. synthetic flight
    doc = generate(cfg, duration_s=duration_s, step_s=step_s, seed=seed)
    log(f"synthetic flight {doc['meta']['mission_id']}: {len(doc['frames'])} frames, speed {speed}")
    live = fly_live(doc["meta"], [(f["t"], (lambda f=f: f)) for f in doc["frames"]], cfg,
                    speed=speed, clock=clock, sleep=sleep)
    fm = flight_metrics(live, doc, cfg, speed)
    log(f"  landed: {len(live.flight.alerts)} alerts, live == batch: {fm['live_equals_batch']}; packaging")
    fm |= _landing(doc, live, cfg, out, video=None, untracked=None, use_llm=use_llm,
                   generate_fn=generate_fn, clock=clock)
    metrics["flights"].append(fm)
    rows += [(fm["mission_id"], f) for f in live.frames]

    # 2. video flight
    if video is not None:
        from hima.sources.video import VideoSource
        gpu = _gpu()
        src = VideoSource(video, cfg, model=model, weights=weights)
        log(f"video flight {src.rec.meta['mission_id']}: {src.fps:g} fps, model load {src.load_s:.2f} s")
        try:
            vlive = fly_live(src.rec.meta, video_frames(src, max_frames), cfg, speed=speed, clock=clock, sleep=sleep)
        finally:
            src.close()
        vdoc = src.rec.to_session()
        vm = flight_metrics(vlive, vdoc, cfg, speed)
        vm |= {"video": str(video), "video_fps": src.fps, "model_load_s": round(src.load_s, 3),
               "gpu_at_flight_start": gpu, "untracked": len(src.rec.untracked)}
        log(f"  landed: {vm['frames']} frames, live == batch: {vm['live_equals_batch']}; packaging")
        vm |= _landing(vdoc, vlive, cfg, out, video=video, untracked=src.rec.untracked, use_llm=use_llm,
                       generate_fn=generate_fn, clock=clock)
        metrics["flights"].append(vm)
        rows += [(vm["mission_id"], f) for f in vlive.frames]

    # 3. ground station
    arc = out / "ground_archive"
    t0 = clock()
    statuses = {f["mission_id"]: ingest(f["package_dir"], arc)["status"] for f in metrics["flights"]}
    ingest_s = clock() - t0
    st = mission_stats(load_index(arc)["missions"])
    review = export_review(arc, out / "review_export", list(cfg.detector.class_map))
    metrics["ground"] = {
        "archive": str(arc), "ingest": statuses, "ingest_s": round(ingest_s, 3),
        "verify_problems": verify_archive(arc),
        "stats": {k: st[k] for k in ("missions", "flight_s", "tracks", "tracks_by_species", "alert_tracks",
                                     "uncertain", "uncertain_with_image", "report_sources", "note")},
        "review": {"exported": review["exported"], "skipped_without_image": review["skipped_without_image"],
                   "out": review["out"]},
    }
    with open(out / FRAMES_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mission_id", "t", "source_s", "onboard_s", "lag_s"])
        for mid, f in rows:
            w.writerow([mid, f.t, f"{f.source_s:.6f}", f"{f.onboard_s:.6f}", f"{f.lag_s:.6f}"])
    (out / METRICS_FILE).write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    return metrics


def _ms(x: float | None) -> str:
    return "-" if x is None else f"{x * 1000:.1f} ms"


def summary_lines(m: dict[str, Any]) -> list[str]:
    lines = [f"end-to-end run (speed {m['speed']}, LLM {'on' if m['use_llm'] else 'off'}); {m['note']}"]
    for f in m["flights"]:
        lines.append(f"[{f['mission_id']}] {f['source']}: {f['frames']} frames over {f['mission_s']:g} s of mission, "
                     f"wall {f['wall_s']:g} s")
        fs = f["frame_s"]
        lines.append(f"  frame time p50 {_ms(fs['p50'])}, p95 {_ms(fs['p95'])}, max {_ms(fs['max'])} "
                     f"(first {_ms(f['first_frame_s'])}); onboard p95 {_ms(f['onboard_s']['p95'])}")
        lag = "-" if f["max_lag_s"] is None else f"{f['max_lag_s']:.3f} s, late frames {f['late_frames']}"
        lines.append(f"  real-time factor {f['real_time_factor']} ({f['real_time_factor_after_first']} without "
                     f"frame 0; below 1 keeps up); max lag {lag}")
        lines.append(f"  refreshes {f['refreshes']} (last {_ms(f['last_refresh_s'])}); live == batch: "
                     f"{f['live_equals_batch']}")
        lines.append(f"  alerts {len(f['alerts'])}, link {f['link_bytes']} B")
        for a in f["alerts"]:
            lines.append(f"    {a['text']:<44} seen {a['first_seen_s']:g} s -> raised {a['raised_t']:g} s -> "
                         f"delivered {a['delivered_t']} s; wall {_ms(a['wall_lag_s'])}")
        p, r = f["package"], f["report"]
        lines.append(f"  session {f['session_bytes'] / 1e6:.2f} MB; package {p['bytes_on_disk'] / 1e6:.2f} MB on disk "
                     f"(budget {p['used_bytes'] / 1e6:.2f} of {p['limit_bytes'] / 1e6:g} MB, {p['dropped']} dropped)")
        lat = "-" if r["latency_s"] is None else f"{r['latency_s']:.1f} s"
        lines.append(f"  report {r['source']} in {lat} (package {f['package_s']:.1f} s)"
                     + (f"; template sentences: {', '.join(r['repaired'])}" if r["repaired"] else ""))
    g = m["ground"]
    lines.append(f"ground: ingest {g['ingest']} in {g['ingest_s']:.2f} s; archive problems: "
                 f"{g['verify_problems'] or 'none'}")
    s = g["stats"]
    lines.append(f"  {s['missions']} missions, {s['tracks']} tracks {s['tracks_by_species']}, "
                 f"{s['alert_tracks']} in alert; review export: {g['review']['exported']} images")
    lines.append(f"  NOTE: {s['note']}")
    return lines
