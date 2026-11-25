"""Command line: python -m hima <command> ...

Commands: generate, record-video, analyze, evaluate, advise, onboard-replay, package,
ingest, ground-stats, export-review, e2e, preflight, fly, keygen, deliver, receive,
reserve, plan, history, fieldops.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from hima.analytics.analyze import analyze
from hima.config import ConfigError, load_config
from hima.contract.io import SessionInvalid, load_session, save_session


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hima")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write a synthetic session (raw frames + ground_truth)")
    g.add_argument("--out", required=True, help="output *.session.json path")
    g.add_argument("--duration", type=float, default=1800.0, help="seconds (default 1800)")
    g.add_argument("--step", type=float, default=1.0, help="seconds between frames (default 1)")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--config", help="path to hima.toml")

    a = sub.add_parser("analyze", help="compute the analysis section of a session file")
    a.add_argument("session", help="path to a *.session.json file")
    a.add_argument("--config", help="path to hima.toml (default: the one next to the hima package)")
    a.add_argument("--out", help="write here instead of updating the input file")

    e = sub.add_parser("evaluate", help="compare analysis statuses with ground_truth (synthetic only)")
    e.add_argument("session")

    v = sub.add_parser("advise", help="ask the local LLM for advice per animal, gate it, write 'advice'")
    v.add_argument("session")
    v.add_argument("--config", help="path to hima.toml")
    v.add_argument("--out", help="write here instead of updating the input file")

    r = sub.add_parser("record-video", help="track animals in a video file and write a raw session")
    r.add_argument("video", help="video file (e.g. 1.mp4)")
    r.add_argument("--out", required=True, help="output *.session.json path")
    r.add_argument("--max-frames", type=int, help="stop after this many frames")
    r.add_argument("--weights", help="weights file (default: [detector].weights in hima.toml)")
    r.add_argument("--config", help="path to hima.toml")
    r.add_argument("--package", metavar="DIR", help="also build the mission package in DIR/<mission_id> "
                   "(includes the detections without a track id, which the session file cannot hold)")
    r.add_argument("--no-llm", action="store_true", help="with --package: fixed report template, no LLM call")

    k = sub.add_parser("package", help="build the mission package for the ground station from a session")
    k.add_argument("session")
    k.add_argument("--out", required=True, help="folder; the package goes to OUT/<mission_id>")
    k.add_argument("--video", help="the video this session was recorded from (to cut clips and still frames)")
    k.add_argument("--config", help="path to hima.toml")
    k.add_argument("--no-llm", action="store_true", help="use the fixed report template, do not call the LLM")

    o = sub.add_parser("onboard-replay", help="feed a session through the onboard loop frame by frame")
    o.add_argument("session")
    o.add_argument("--update-s", type=float, help="refresh period in mission seconds (default: [onboard].update_s)")
    o.add_argument("--speed", type=float, help="1 = real time, 10 = ten times faster (default: as fast as possible)")
    o.add_argument("--config", help="path to hima.toml")

    g = sub.add_parser("ingest", help="ground station: check mission package(s) and add them to the archive")
    g.add_argument("packages", nargs="+", help="package folder(s), e.g. packages/<mission_id>")
    g.add_argument("--archive", default="ground_archive", help="archive folder (default: ground_archive)")

    s = sub.add_parser("ground-stats", help="ground station: statistics across the archived missions")
    s.add_argument("--archive", default="ground_archive")
    s.add_argument("--json", action="store_true", help="print the statistics as JSON")

    e = sub.add_parser("export-review", help="ground station: export uncertain detections for labelling (YOLO)")
    e.add_argument("--archive", default="ground_archive")
    e.add_argument("--out", required=True, help="new or empty folder")
    e.add_argument("--config", help="path to hima.toml (class names in the weight's order)")

    x = sub.add_parser("e2e", help="one complete simulated mission: synthetic flight + video flight -> "
                       "packages -> ground archive")
    x.add_argument("--out", required=True, help="new or empty folder for everything the run makes")
    x.add_argument("--video", help="video for the second flight (e.g. 1.mp4); without it only the synthetic flight")
    x.add_argument("--speed", type=float, default=1.0, help="1 = real time (default), 0 = as fast as possible")
    x.add_argument("--seed", type=int, default=42)
    x.add_argument("--duration", type=float, default=1800.0, help="synthetic flight length in seconds")
    x.add_argument("--max-frames", type=int, help="video: stop after this many frames")
    x.add_argument("--weights", help="weights file (default: [detector].weights in hima.toml)")
    x.add_argument("--config", help="path to hima.toml")
    x.add_argument("--no-llm", action="store_true", help="fixed report template, no LLM call")

    for name, helptext in (("fly", "the live flight on the drone: preflight, wait for ARM, YOLO + alerts to the "
                                   "Pixhawk until DISARM, then the mission package"),
                           ("preflight", "only the checks before a flight (camera, Pixhawk, GPS, weights, disk, AI)")):
        f = sub.add_parser(name, help=helptext)
        f.add_argument("--config", help="path to hima.toml")
        f.add_argument("--mavlink", help="override [mavlink].url, e.g. /dev/ttyTHS1 or tcp:127.0.0.1:5763 (SITL)")
        f.add_argument("--baud", type=int, help="override [mavlink].baud")
        f.add_argument("--camera", help="override [camera].device: index, device path, GStreamer pipeline or video file")
        f.add_argument("--no-llm", action="store_true", help="no periodic analysis by the AI, template landing report")
        f.add_argument("--no-vlm", action="store_true", help="no camera window for the AI")
        if name == "fly":
            f.add_argument("--out", help="new or empty folder (default: [flight].out_dir/flight-<UTC time>)")
            f.add_argument("--no-arm-wait", action="store_true",
                           help="record at once, until Ctrl+C (bench tests; default: from ARM to DISARM)")
            f.add_argument("--max-frames", type=int, help="stop after this many frames (bench tests)")

    kg = sub.add_parser("keygen", help="Wi-Fi delivery: make the shared key, then copy the same file to the drone "
                        "and the station")
    kg.add_argument("--config", help="path to hima.toml")
    kg.add_argument("--out", help="key file (default: [delivery].key_file)")
    kg.add_argument("--force", action="store_true", help="replace an existing key (copy it to both machines again)")

    dv = sub.add_parser("deliver", help="drone: send finished mission packages to the ground station over Wi-Fi")
    dv.add_argument("packages", nargs="*", help="package folder(s), sent whatever their state (default: every "
                    "waiting package under [flight].out_dir)")
    dv.add_argument("--config", help="path to hima.toml")
    dv.add_argument("--station", help="override [delivery].station_url, e.g. http://192.168.1.10:8765")
    dv.add_argument("--watch", action="store_true", help="keep running: send each new package as soon as the station "
                    "answers (the hima-deliver service)")
    dv.add_argument("--ping", action="store_true", help="only check that the station answers with the same key")

    rv = sub.add_parser("receive", help="ground station: receive mission packages from the drone over Wi-Fi, "
                        "check them and add them to the archive")
    rv.add_argument("--config", help="path to hima.toml")
    rv.add_argument("--listen", help="HOST:PORT (default: [delivery].listen)")
    rv.add_argument("--archive", help="archive folder (default: [delivery].archive_dir)")
    rv.add_argument("--inbox", help="inbox folder (default: [delivery].inbox_dir)")
    rv.add_argument("--radio", help="route offers over the telemetry radio: MAVLink URL of the ground radio (COM5, "
                    "udpin:0.0.0.0:14445; default: [route_review].radio_url)")

    rs = sub.add_parser("reserve", help="autonomy: the reserve boundary, the ArduPilot fence and the grid")
    rs.add_argument("--config", help="path to hima.toml")
    rs.add_argument("--propose", action="store_true", help="also propose launch points (greedy cover by the reach "
                    "of one flight) and print them as [reserve.launch] lines")

    pn = sub.add_parser("plan", help="autonomy: today's route before take-off (mission + fence files for ArduPilot)")
    pn.add_argument("--config", help="path to hima.toml")
    pn.add_argument("--launch", help="launch point name from [reserve.launch] (default: the one with the best plan)")
    pn.add_argument("--date", help="plan for this day, YYYY-MM-DD (default: today, UTC)")
    pn.add_argument("--out", help="folder (default: [flight].out_dir/plan-<date>-<launch>)")

    hs = sub.add_parser("history", help="autonomy: what past flights left (paths, herds, poaching incidents)")
    hs.add_argument("--config", help="path to hima.toml")
    hsub = hs.add_subparsers(dest="hcmd", required=True)
    h1 = hsub.add_parser("add-session", help="add a flown session (its path and the animals' sighting points)")
    h1.add_argument("session")
    h2 = hsub.add_parser("add-incident", help="add an incident reported by the rangers")
    h2.add_argument("--date", required=True, help="YYYY-MM-DD")
    h2.add_argument("--lat", type=float, required=True)
    h2.add_argument("--lon", type=float, required=True)
    h2.add_argument("--kind", required=True, help="poaching, vehicle, carcass, fence_cut or other")
    h2.add_argument("--note", default="")
    hsub.add_parser("show", help="counts and dates")

    fo = sub.add_parser("fieldops", help="autonomy: planned field work of the reserve's own vehicles (not followed)")
    fo.add_argument("--config", help="path to hima.toml")
    fsub = fo.add_subparsers(dest="fcmd", required=True)
    f1 = fsub.add_parser("add", help="rangers work here, then: their vehicles near a herd are not followed")
    f1.add_argument("--lat", type=float, required=True)
    f1.add_argument("--lon", type=float, required=True)
    f1.add_argument("--radius-m", type=float, default=1500.0)
    f1.add_argument("--start", required=True, help="UTC, YYYY-MM-DDTHH:MM:SSZ")
    f1.add_argument("--end", required=True, help="UTC, YYYY-MM-DDTHH:MM:SSZ")
    f1.add_argument("--note", default="", help="team or vehicle (sent in the drone's text, first 12 letters)")
    fsub.add_parser("show", help="the entries")

    im = sub.add_parser("imagery", help="station: satellite images under the dashboard's route map (offline)")
    im.add_argument("--config", help="path to hima.toml")
    isub = im.add_subparsers(dest="icmd", required=True)
    i1 = isub.add_parser("add", help="store an image with its place (bounds or a world file)")
    i1.add_argument("image")
    i1.add_argument("--bounds", help="SOUTH,WEST,NORTH,EAST in degrees")
    i1.add_argument("--world", help="world file (.jgw/.pgw/.tfw) instead of --bounds")
    i1.add_argument("--crs", type=int, default=3857, choices=(3857, 4326),
                    help="3857 = Web Mercator (map tiles), 4326 = degrees (most GeoTIFF exports); default 3857")
    i1.add_argument("--source", required=True, help="where the image comes from (kept next to it)")
    i1.add_argument("--license", required=True, help="its terms (kept next to it)")
    i1.add_argument("--name", help="stored name (default: the file name)")
    isub.add_parser("list", help="the stored images")

    dm = sub.add_parser("demo", help="station: put the virtual flight ([demo]) into the archive (the station's start "
                                     "does it by itself)")
    dm.add_argument("--config", help="path to hima.toml")
    dm.add_argument("--archive", help="archive folder (default [delivery].archive_dir)")

    db = sub.add_parser("dashboard", help="station: the dashboard in the browser (this PC only; no internet needed)")
    db.add_argument("--config", help="path to hima.toml")
    db.add_argument("--archive", help="archive folder (default [delivery].archive_dir)")
    db.add_argument("--port", type=int, default=8501, help="port on localhost (default 8501)")
    db.add_argument("--open", action="store_true", help="open the browser")

    args = p.parse_args(argv)
    try:
        return {"generate": _generate, "record-video": _record_video, "analyze": _analyze,
                "evaluate": _evaluate, "advise": _advise, "onboard-replay": _onboard_replay, "package": _package,
                "ingest": _ingest, "ground-stats": _ground_stats, "export-review": _export_review,
                "e2e": _e2e, "fly": _fly, "preflight": _preflight, "keygen": _keygen, "deliver": _deliver,
                "receive": _receive, "reserve": _reserve, "plan": _plan, "history": _history,
                "fieldops": _fieldops, "imagery": _imagery, "demo": _demo,
                "dashboard": _dashboard}[args.cmd](args)
    except (ConfigError, SessionInvalid, OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


def _generate(args) -> int:
    from hima.sources.synthetic import generate
    cfg = load_config(args.config)
    t0 = time.perf_counter()
    doc = generate(cfg, duration_s=args.duration, step_s=args.step, seed=args.seed)
    elapsed = time.perf_counter() - t0
    save_session(doc, args.out)
    n_det = sum(len(f["detections"]) for f in doc["frames"])
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"generated {len(doc['frames'])} frames, {n_det} detections, "
          f"{len(doc['ground_truth'])} animals in {elapsed:.3f} s -> {args.out} ({size_mb:.2f} MB)")
    return 0


def _record_video(args) -> int:
    from hima.sources.video import record_video
    if args.max_frames is not None and args.max_frames < 1:
        raise ValueError("--max-frames must be at least 1")
    cfg = load_config(args.config)
    _, st, rec = record_video(args.video, cfg, weights=args.weights, max_frames=args.max_frames)
    rec.save(args.out)
    ids = {d["track_id"] for f in rec.frames for d in f["detections"]}
    after = "-" if st.fps_after_first is None else f"{st.fps_after_first:.1f}"
    print(f"recorded {st.frames} frames ({st.video_fps:g} fps video), {st.detections} tracked detections, "
          f"{len(ids)} track ids, {st.untracked} untracked -> {args.out}")
    print(f"model load {st.load_s:.2f} s, first frame {st.first_frame_s:.2f} s, "
          f"loop {st.loop_s:.2f} s, processing rate after first frame {after} frames/s")
    print("note: params_known=false (no telemetry): drone pose and hfov are placeholders; "
          "analyze will compute visibility only")
    if args.package:
        from hima.onboard.packager import build_package
        path, m = build_package(rec.to_session(), cfg, args.package, video=args.video, untracked=rec.untracked,
                                use_llm=not args.no_llm)
        _print_package(path, m)
    return 0


def _package(args) -> int:
    from hima.onboard.packager import build_package
    cfg = load_config(args.config)
    path, m = build_package(load_session(args.session), cfg, args.out, video=args.video, use_llm=not args.no_llm)
    _print_package(path, m)
    return 0


def _print_package(path, m) -> int:
    items, b = m["items"], m["budget"]
    print(f"package -> {path}")
    print(f"  media: {m['media']['video'] if m['media'] else 'none (clips are time references only)'}; "
          f"stored {b['used_bytes'] / 1e6:.2f} MB of {b['limit_bytes'] / 1e6:g} MB, "
          f"{b['dropped_count']} dropped{', OVER BUDGET' if b['over_budget'] else ''}")
    c = m["criteria"]
    thr = "unknown" if c["new_track_thresh"] is None else f"{c['new_track_thresh']:g}"
    print(f"  new_track_thresh {thr}; skipped: " + (", ".join(s["criterion"] for s in c["skipped"]) or "none"))
    import json
    rep = json.loads((path / "report.json").read_text(encoding="utf-8"))
    lat = "" if rep["latency_s"] is None else f", models {rep['latency_s']:.1f} s"
    judge = f", judge {rep['judge_model']}" if rep.get("judge_model") else ""
    fixed = f"; template sentences for: {', '.join(rep['gate']['repaired'])}" if rep["gate"]["repaired"] else ""
    print(f"  report: {rep['source']}{lat}{judge}{fixed}" + ("" if not rep["gate"]["reasons"] else
                                                "; gate: " + "; ".join(rep["gate"]["reasons"])))
    print(f"    {rep['report']['headline']}")
    for it in items:
        size = f"{it['file']['bytes'] / 1e3:8.1f} kB" if it["file"] else "       -   "
        print(f"  {it['id']:<9} p{it['priority']} {it['state']:<8} {size}  "
              f"t {it['t_start']:8.2f}-{it['t_end']:8.2f}  {it['reason']}")
    return 0


def _analyze(args) -> int:
    cfg = load_config(args.config)
    doc = load_session(args.session)
    t0 = time.perf_counter()
    doc["analysis"] = analyze(doc, cfg)
    elapsed = time.perf_counter() - t0
    stale_advice = doc.pop("advice", None) is not None
    out = args.out or args.session
    save_session(doc, out)

    animals = doc["analysis"]["animals"]
    print(f"analyzed {len(doc['frames'])} frames, {len(animals)} animals in {elapsed:.3f} s -> {out}")
    if stale_advice:
        print("note: removed the old 'advice' section because the analysis changed; run advise again")
    for an in animals:
        print(f"  track {an['track_id']:>4}  {an['species']:<13} visible {an['visible_s']:>8.1f} s  {an['status']}")
    return 0


def _evaluate(args) -> int:
    from hima.evaluate import evaluate
    res = evaluate(load_session(args.session))
    print(f"{'track':>5}  {'scenario':<26} {'expected':<28} {'got':<28} ok")
    for r in res["rows"]:
        print(f"{r['track_id']:>5}  {r['scenario']:<26} {r['expected']:<28} {str(r['got']):<28} "
              f"{'yes' if r['ok'] else 'NO'}")
    print(f"animals {res['animals_ok']}/{res['animals_total']}  "
          f"scenarios {res['scenarios_ok']}/{res['scenarios_total']}")
    return 0


def _advise(args) -> int:
    from hima.advisor.advise import advise
    cfg = load_config(args.config)
    doc = load_session(args.session)
    t0 = time.perf_counter()
    doc["advice"] = advise(doc, cfg)
    elapsed = time.perf_counter() - t0
    out = args.out or args.session
    save_session(doc, out)

    rows = doc["advice"]["animals"]
    status = {a["track_id"]: a["status"] for a in doc["analysis"]["animals"]}
    print(f"advised {len(rows)} animals with {cfg.llm.model} in {elapsed:.1f} s -> {out}")
    for r in rows:
        f = r["final"]
        verdict = "ACCEPTED" if r["gate"]["accepted"] else "rejected"
        lat = f"{r['latency_s']:.1f}s" if r["latency_s"] is not None else "-"
        print(f"  track {r['track_id']:>4} {status[r['track_id']]:<28} {verdict:<8} {lat:>6}  "
              f"final {f['proposed_command']} @ {f['proposed_altitude_m']} m ({f['source']})")
        for reason in r["gate"]["reasons"]:
            print(f"        - {reason}")
    acc = sum(r["gate"]["accepted"] for r in rows)
    print(f"accepted {acc}/{len(rows)}")
    return 0



def _onboard_replay(args) -> int:
    import statistics
    from hima.config import override
    from hima.onboard.flight import fly
    from hima.onboard.loop import time_to_stable
    cfg = load_config(args.config)
    if args.update_s is not None:
        cfg = override(cfg, "onboard", update_s=args.update_s)      # validated: must be > 0
    doc = load_session(args.session)
    t0 = time.perf_counter()
    flight = fly(doc, cfg, speed=args.speed)
    wall = time.perf_counter() - t0
    res, loop = flight.replay, flight.loop
    cs = [s.compute_s for s in loop.snapshots]
    print(f"replayed {len(loop.frames)} frames, {len(loop.snapshots)} refreshes every "
          f"{cfg.onboard.update_s:g} s of mission time, wall {wall:.2f} s")
    print(f"refresh compute: median {statistics.median(cs) * 1000:.1f} ms, max {max(cs) * 1000:.1f} ms, "
          f"last (all {len(loop.frames)} frames) {cs[-1] * 1000:.1f} ms")
    if res.lag_s:
        print(f"lag behind real time: max {max(res.lag_s):.2f} s")
    print("status changes (mission time):")
    for c in loop.changes:
        print(f"  t {c.t:>8.1f} s  track {c.track_id:>4}  {c.old or '(new)':<28} -> {c.new}")
    print(f"live alerts (min_visible_s {cfg.alerts.min_visible_s:g}, cooldown_s {cfg.alerts.cooldown_s:g}, "
          f"link {cfg.link.bytes_per_s:g} B/s): {len(flight.alerts)}; dwell (max {cfg.dwell.max_over_animal_s:g} s "
          f"over one animal): {len(flight.dwell)}")
    for d in flight.deliveries:
        a = d.alert
        seen = f"seen {a.visible_s:.0f} s" if hasattr(a, "visible_s") else f"stay {a.stay_s:.0f} s"
        print(f"  raised t {a.t:>8.1f}  delivered t {d.delivered_t:>8.2f}  ({d.bytes} B, {seen})  {a.text}")
    if not doc["meta"]["camera"]["params_known"]:
        print("  note: params_known=false: only visibility statuses exist, so no alert status can occur")
    stable = time_to_stable(loop)
    print("final status and the mission time from which it no longer changed:")
    for a in res.final["animals"]:
        n = sum(1 for c in loop.changes if c.track_id == a["track_id"])
        print(f"  track {a['track_id']:>4}  {a['status']:<28} stable from t {stable[a['track_id']]:>8.1f} s  "
              f"({n} status change{'s' if n != 1 else ''})")
    return 0



def _ingest(args) -> int:
    from hima.ground.archive import IngestRejected, ingest
    bad = 0
    for pkg in args.packages:
        try:
            r = ingest(pkg, args.archive)
        except IngestRejected as err:
            bad += 1
            print(f"REJECTED {pkg}")
            for reason in err.errors:
                print(f"  - {reason}")
            continue
        e = r["entry"]
        print(f"{r['status']:<16} {e['mission_id']}  ({e['tracks_seen']} tracks, "
              f"{len(e['alert_track_ids'])} in alert, {sum(e['uncertain'].values())} uncertain, report {e['report_source']})")
    print(f"archive: {args.archive}  ({len(args.packages) - bad} accepted, {bad} rejected)")
    return 1 if bad else 0


def _ground_stats(args) -> int:
    import json
    from hima.ground.archive import load_index
    from hima.ground.stats import mission_stats
    st = mission_stats(load_index(args.archive)["missions"])
    if args.json:
        print(json.dumps(st, indent=1))
        return 0
    print(f"{st['missions']} missions, {st['flight_s'] / 60:.1f} min of flight")
    print(f"tracks: {st['tracks']} {st['tracks_by_species']}; in alert: {st['alert_tracks']}")
    print(f"NOTE: {st['note']}")
    print(f"statuses: {st['statuses']}")
    print(f"events:   {st['events']}")
    print(f"uncertain detections: {st['uncertain']} ({st['uncertain_with_image']} with an image)")
    print(f"reports:  {st['report_sources']}")
    for day, d in st["by_day"].items():
        print(f"  {day}: {d['missions']} missions, {d['tracks']} tracks {d['tracks_by_species']}, "
              f"{d['alert_tracks']} in alert")
    return 0


def _export_review(args) -> int:
    from hima.ground.review import LABEL_NOTE, export_review
    cfg = load_config(args.config)
    r = export_review(args.archive, args.out, list(cfg.detector.class_map))
    print(f"exported {r['exported']} images with labels -> {r['out']}")
    if r["skipped_without_image"]:
        print(f"  not exported (no image): {r['skipped_without_image']}")
    print(f"  {LABEL_NOTE}")
    return 0


def _e2e(args) -> int:
    from hima.e2e import METRICS_FILE, run_e2e, summary_lines
    if args.max_frames is not None and args.max_frames < 1:
        raise ValueError("--max-frames must be at least 1")
    cfg = load_config(args.config)
    m = run_e2e(args.out, cfg, seed=args.seed, duration_s=args.duration, speed=args.speed or None,
                video=args.video, max_frames=args.max_frames, weights=args.weights, use_llm=not args.no_llm,
                log=lambda s: print(s, flush=True))
    for line in summary_lines(m):
        print(line)
    print(f"metrics -> {Path(args.out) / METRICS_FILE}")
    print(f"dashboard: python -m hima dashboard --archive {Path(args.out) / 'ground_archive'} --open")
    bad = [f["mission_id"] for f in m["flights"] if not f["live_equals_batch"]] + list(m["ground"]["verify_problems"])
    if bad:
        print(f"PROBLEM: {bad}")
    return 1 if bad else 0


def _fly_args(args) -> dict:
    return {"url": args.mavlink, "baud": args.baud, "device": args.camera, "use_llm": not args.no_llm,
            "use_vlm": not args.no_vlm}


def _preflight(args) -> int:
    from hima.fly import fly
    m = fly(load_config(args.config), preflight_only=True, **_fly_args(args))
    return 0 if m["ok"] else 1


def _fly(args) -> int:
    from hima.fly import fly
    if args.max_frames is not None and args.max_frames < 1:
        raise ValueError("--max-frames must be at least 1")
    m = fly(load_config(args.config), out=args.out, wait_arm=False if args.no_arm_wait else None,
            max_frames=args.max_frames, **_fly_args(args))
    return 0 if m["ok"] else 1


def _keygen(args) -> int:
    from hima.contract.delivery import fingerprint, write_key
    from hima.onboard.deliver import config_path
    cfg = load_config(args.config)
    path = Path(args.out) if args.out else config_path(cfg, cfg.delivery.key_file)
    key = write_key(path, force=args.force)
    print(f"key -> {path}")
    print(f"  fingerprint {fingerprint(key)}")
    print("  copy this same file to the other machine (drone and station), at its [delivery].key_file;")
    print("  never into git, a chat or an e-mail: whoever holds it can send packages to the station")
    return 0


def _deliver(args) -> int:
    from hima.config import override
    from hima.onboard.deliver import config_path, deliver_pending, ping, watch
    cfg = load_config(args.config)
    if args.station:
        cfg = override(cfg, "delivery", station_url=args.station)
    if args.ping:
        ok, detail = ping(cfg)
        print(("OK    " if ok else "FAIL  ") + detail)
        return 0 if ok else 1
    if args.watch:
        if args.packages:
            raise ValueError("--watch takes no package folders (it watches [flight].out_dir)")
        try:
            watch(cfg)
        except KeyboardInterrupt:
            print("stopped (Ctrl+C)")
        return 0
    r = deliver_pending(cfg, args.packages or None)
    if not any(r[k] for k in ("delivered", "rejected", "broken", "pending")):
        where = config_path(cfg, cfg.flight.out_dir)
        print(f"no package waiting in {where}" + (f" ({len(r['done_before'])} delivered or refused before)"
                                                 if r["done_before"] else ""))
        return 0
    for k, title in (("delivered", "delivered"), ("rejected", "REJECTED by the station"),
                     ("broken", "NOT SENT, damaged on the drone"), ("pending", "waiting")):
        if r[k]:
            print(f"{title}: {', '.join(r[k])}")
    if r["pending"] and r["error"]:
        print(f"  why waiting: {r['error']}")
    return 0 if not (r["pending"] or r["rejected"] or r["broken"]) else 1


def _receive(args) -> int:
    from hima.contract.delivery import load_key, parse_listen
    from hima.ground.receiver import serve
    from hima.onboard.deliver import config_path
    cfg = load_config(args.config)
    host, port = parse_listen(args.listen or cfg.delivery.listen)
    key = load_key(config_path(cfg, cfg.delivery.key_file))
    plans = config_path(cfg, cfg.route_review.plans_dir)
    radio = None
    url = args.radio or cfg.route_review.radio_url
    if url and url != "off":
        from hima.ground.route_radio import RadioBridge
        radio = RadioBridge(cfg, plans, url)
        radio.start()
    archive = args.archive or config_path(cfg, cfg.delivery.archive_dir)
    from hima.ground.demo import ensure_demo
    Path(archive).mkdir(parents=True, exist_ok=True)
    ensure_demo(cfg, archive, plans)                              # the dashboard never opens empty
    serve(key, host, port, args.inbox or config_path(cfg, cfg.delivery.inbox_dir), archive, plans=plans, radio=radio)
    return 0


def _demo(args) -> int:
    from hima.ground.demo import ensure_demo
    from hima.onboard.deliver import config_path
    cfg = load_config(args.config)
    archive = Path(args.archive) if args.archive else config_path(cfg, cfg.delivery.archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    r = ensure_demo(cfg, archive, config_path(cfg, cfg.route_review.plans_dir))
    return 0 if r["flight"] and not str(r["flight"]).startswith(("missing", "not added")) else 1


def _dashboard(args) -> int:
    from hima.ground.web.server import serve
    from hima.onboard.deliver import config_path
    cfg = load_config(args.config)
    archive = Path(args.archive) if args.archive else config_path(cfg, cfg.delivery.archive_dir)
    serve(archive, cfg, args.port, open_browser=args.open)
    return 0


def _reserve(args) -> int:
    from hima.autonomy.planner import budget, propose_launch_points
    from hima.autonomy.reserve import Reserve, fence_inside_boundary
    cfg = load_config(args.config)
    r = Reserve.load(cfg)
    for k, v in r.summary().items():
        print(f"{k:24s} {v}")
    print(f"{'fence inside boundary':24s} {fence_inside_boundary(r)}")
    b = budget(cfg)
    print(f"{'route per flight':24s} {b.route_m / 1e3:.1f} km (reach {b.reach_m / 1e3:.1f} km, swath {b.swath_m:.1f} m)")
    if args.propose:
        pts, cov = propose_launch_points(cfg, r)
        print()
        print(f"{len(pts)} launch points put {cov * 100:.1f} % of the safe cells within reach of one flight:")
        print("[reserve.launch]")
        for i, (x, y) in enumerate(pts, start=1):
            la, lo = r.to_latlon(x, y)
            print(f"{'main' if i == 1 else f'L{i:02d}'} = [{la:.5f}, {lo:.5f}]")
    return 0


def _plan(args) -> int:
    import datetime as dt
    from hima.autonomy import history
    from hima.autonomy.export import write_plan
    from hima.autonomy.planner import best_plan
    from hima.autonomy.reserve import Reserve, fence_inside_boundary, resolve
    cfg = load_config(args.config)
    today = history.parse_date(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    r = Reserve.load(cfg)
    if not fence_inside_boundary(r):
        print("error: the fence is not inside the reserve boundary", file=sys.stderr)
        return 2
    hist = history.load(resolve(cfg, cfg.reserve.history))
    plan, plans, sc = best_plan(cfg, r, hist, today, args.launch)
    out = Path(args.out) if args.out else resolve(cfg, cfg.flight.out_dir) / f"plan-{today}-{plan.launch}"
    files = write_plan(out, cfg, r, plan, sc, plans)
    s = plan.summary()
    print(f"plan {today}, launch {plan.launch}: {s['targets']} targets, {s['route_km']} km of "
          f"{s['route_budget_km']} km, {s['cells_crossed']} cells ({s['never_seen_cells']} never seen, "
          f"{s['risk_cells']} near incidents, {s['herd_cells']} near herds); {s['herd_time_min']} min kept for herds")
    for n in s["notes"]:
        print(f"  note: {n}")
    for name, path in files.items():
        print(f"  {name:18s} {path}")
    return 0


def _history(args) -> int:
    from hima.autonomy import history
    from hima.autonomy.reserve import resolve
    cfg = load_config(args.config)
    path = resolve(cfg, cfg.reserve.history)
    doc = history.load(path)
    if args.hcmd == "show":
        print(f"{path}: {len(doc['flights'])} flights, {len(doc['sightings'])} sightings, "
              f"{len(doc['incidents'])} incidents")
        dates = sorted({f["date"] for f in doc["flights"]})
        if dates:
            print(f"  flights from {dates[0]} to {dates[-1]}")
        return 0
    if args.hcmd == "add-session":
        got = history.add_session(doc, load_session(args.session))
    else:
        got = history.add_incident(doc, date=args.date, lat=args.lat, lon=args.lon, kind=args.kind, note=args.note)
    history.save(doc, path)
    print(f"added to {path}: {got}")
    return 0


def _fieldops(args) -> int:
    from hima.autonomy import rangers
    from hima.autonomy.reserve import resolve
    cfg = load_config(args.config)
    path = resolve(cfg, cfg.rangers.field_ops)
    if args.fcmd == "show":
        ops = rangers.load_ops(path)
        print(f"{path}: {len(ops)} entries")
        for op in ops:
            print(f"  {op['start_utc']} .. {op['end_utc']}  r {op['radius_m']:g} m  {op['note']}")
        return 0
    got = rangers.add_op(path, lat=args.lat, lon=args.lon, radius_m=args.radius_m, start=args.start, end=args.end,
                         note=args.note)
    print(f"added to {path}: {got}")
    return 0




def _imagery(args) -> int:
    from hima.ground.imagery import add_image, list_images, read_world
    from hima.onboard.deliver import config_path
    cfg = load_config(args.config)
    d = config_path(cfg, cfg.route_review.imagery_dir)
    if args.icmd == "list":
        ims = list_images(d)
        for m in ims:
            print(f"{m['name']}: {m['width_px']}x{m['height_px']} px, about {m['m_per_px']} m/px, bounds "
                  f"{m['bounds_deg']}, {m['source']} ({m['licence']})")
        print(f"{len(ims)} image(s) in {d}")
        return 0
    if (args.bounds is None) == (args.world is None):
        raise ValueError("give --bounds or --world")
    if args.bounds:
        b = tuple(float(x) for x in args.bounds.split(","))
        if len(b) != 4:
            raise ValueError("--bounds SOUTH,WEST,NORTH,EAST")
    else:
        import cv2
        img = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"{args.image}: not an image OpenCV can read")
        b = read_world(args.world, img.shape[1], img.shape[0], args.crs)
    m = add_image(args.image, d, b, args.crs, args.source, args.license, args.name)
    print(f"stored {m['name']}: {m['width_px']}x{m['height_px']} px, about {m['m_per_px']} m/px, bounds "
          f"{[round(v, 5) for v in m['bounds_deg']]} -> {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
