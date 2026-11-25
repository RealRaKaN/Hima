"""The live flight on the drone computer: python -m hima fly (and python -m hima preflight).

Before take-off (preflight, nothing flies if a check FAILs):
  config, weights (animal + general, class names), camera (gives a frame), Pixhawk (HEARTBEAT, then GPS fix and
  satellites, HOME position, telemetry pose), disk space; warnings only: GPU, Ollama and the flight AI models
  (the alerts are fixed rules and work without the AI; without Ollama the flight runs with the AI off).
Then (with [flight].start_on_arm) it waits for the autopilot to ARM. From arming to disarming (the return home and
landing), at [camera].fps:
  newest camera frame -> pose from MAVLink at that moment -> our animal model + ByteTrack -> session recorder ->
  OnboardFlight (analysis, fixed alerts, dwell limit, foreign objects, camera window and periodic analysis on the
  AI's own line) -> every message the link delivers goes to the Pixhawk as a STATUSTEXT (ArduPilot forwards it to
  the ground radio) -> the frame goes into the flight video.
Disarm (or Ctrl+C / SIGTERM, the camera stops, or max_flight_s): landing = final analysis, session file, flight log,
mission package for the ground station (python -m hima ingest on the ground PC), and fly_metrics.json.
No position: when the Pixhawk link is lost or gives no fresh pose,
the flight does NOT end and the Jetson does not stop analysing: it changes to the no-position mode. The animal model
and the tracker run on every frame as before, but the rules that need a place in metres (distances, dwell, the
alerts, the autonomy's follows) cannot: the no-position analysis counts what it can without one (per span: species,
tracks, how many together, how long each was seen, the last known fix) and goes into the package as
no_position_analysis.json. One frame every [flight].no_position_image_every_s is saved as a JPEG with its detections
and the last known position (flight/no_position/: np-NNNNN.jpg and index.jsonl; also in the package), so the ground
team can try to place an animal seen then.
Raw flight file: every frame (with its pose or none), every box of both models and every message sent are
written as they happen to flight/raw.jsonl (hima/onboard/rawlog.py) and go into the package as raw_flight.jsonl.gz:
the analyses and the raw data together. "HIMA NO POSITION" / "HIMA POSITION BACK" go to the Pixhawk when the link
can still carry them. Without the link the disarm is not seen: the flight then ends when the camera stops, on
Ctrl+C / SIGTERM, or at max_flight_s.

The goal is full autonomy, manual control optional. With an ArduPlane VTOL and
[behaviour].enabled (hima/autonomy/onflight.py): before arming, today's route is planned on the Jetson from the
launch point at HOME, written to <flight>/plan/ and uploaded (fence + mission), and the safety parameters are read
back (a difference is a FAIL). In flight, after each analysed frame, the behaviour decides: patrol (AUTO), passes over
a herd or over an unknown vehicle near a herd (GUIDED), back to AUTO, home on energy; a mode the pilot chose wins. If
the Jetson stops, its HEARTBEAT stops and ArduPilot's GCS failsafe flies home (RTL). A copter flies as before (no
commands).
Mission time t = autopilot time since arming (from the MAVLink clock), so frames and poses share one clock.
"""
from __future__ import annotations

import json
import platform
import shutil
import signal
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2

from hima.analytics.movement import movement_summary
from hima.autonomy.onflight import FlightAutonomy
from hima.config import Config
from hima.contract.io import save_session
from hima.e2e import _gpu, dist
from hima.onboard.ai import AiGateway, flight_models, ollama_generate, prepare_flight_models, release_flight_models
from hima.onboard.flight import OnboardFlight
from hima.onboard.packager import build_package
from hima.onboard.rawlog import PACKAGE_FILE as RAW_PACKAGE_FILE, RawLog, boxes as raw_boxes
from hima.sources.camera import Camera, CameraError
from hima.sources.recorder import FlightMissionRecorder
from hima.sources.video import _detections, check_class_names, model_imgsz, weights_path
from hima.telemetry.mavlink import TelemetryReader

METRICS_FILE = "fly_metrics.json"
NO_POSITION_DIR = "no_position"


def _fix_dict(fix, age_s) -> dict[str, Any] | None:
    if fix is None:
        return None
    return {"lat_deg": fix.lat_deg, "lon_deg": fix.lon_deg, "rel_alt_m": round(fix.rel_alt_m, 2),
            "hdg_deg": fix.hdg_deg, "age_s": None if age_s is None else round(age_s, 2)}


class NoPosition:
    """Frames without a pose: spans, the no-position analysis of each span (every frame is tracked), and
    one saved image every every_s with its detections."""

    NOTE = ("No position from the Pixhawk: no places, distances, dwell or alerts (each needs a place in metres). Times "
            "are seconds from the start of the span on the Jetson's clock. A track id is the tracker's: one animal can "
            "get two ids if it leaves the camera and comes back, so 'tracks' is an upper bound of the animals seen.")

    def __init__(self, folder: Path, every_s: float, jpeg_quality: int, class_map: dict[str, str]):
        self.folder, self.every_s, self.quality, self.class_map = folder, every_s, jpeg_quality, class_map
        self.spans: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self._since: float | None = None
        self._last_img = -1e9
        self._acc: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        return self._since is not None

    def start(self, now: float, t_s: float | None, wall_utc: str, last_fix: dict[str, Any] | None = None) -> None:
        self._since = now
        self._acc = {"frames": 0, "tracks": {}, "untracked": 0, "max_all": 0, "max_sp": {}, "boxes_sp": {}}
        self.spans.append({"start_t_s": t_s, "start_utc": wall_utc, "end_t_s": None, "end_utc": None,
                           "duration_s": None, "images": 0, "last_fix_at_start": last_fix, "analysis": None})

    def observe(self, now: float, dets) -> None:
        """One tracked frame of the span: what the analysis counts."""
        a = self._acc
        s = round(now - self._since, 2)
        a["frames"] += 1
        per: dict[str, int] = {}
        for d in dets:
            sp = self.class_map.get(d.class_raw, d.class_raw)
            per[sp] = per.get(sp, 0) + 1
            a["boxes_sp"][sp] = a["boxes_sp"].get(sp, 0) + 1
            if d.track_id is None:
                a["untracked"] += 1
                continue
            tr = a["tracks"].setdefault(d.track_id, {"track_id": d.track_id, "species": {}, "first_s": s, "last_s": s,
                                                     "frames": 0, "conf_sum": 0.0, "w_sum": 0.0, "h_sum": 0.0})
            tr["species"][sp] = tr["species"].get(sp, 0) + 1
            tr["last_s"] = s
            tr["frames"] += 1
            tr["conf_sum"] += d.conf
            tr["w_sum"] += d.bbox_xywh_px[2]
            tr["h_sum"] += d.bbox_xywh_px[3]
        a["max_all"] = max(a["max_all"], sum(per.values()))
        for sp, n in per.items():
            a["max_sp"][sp] = max(a["max_sp"].get(sp, 0), n)

    def _analysis(self) -> dict[str, Any]:
        a = self._acc
        tracks = []
        for tr in a["tracks"].values():
            sp = max(tr["species"], key=tr["species"].get)
            n = tr["frames"]
            tracks.append({"track_id": tr["track_id"], "species": sp, "first_s": tr["first_s"], "last_s": tr["last_s"],
                           "seen_s": round(tr["last_s"] - tr["first_s"], 2), "frames": n,
                           "mean_conf": round(tr["conf_sum"] / n, 3),
                           "mean_box_px": [round(tr["w_sum"] / n, 1), round(tr["h_sum"] / n, 1)]})
        species = {}
        for sp in sorted(a["boxes_sp"]):
            species[sp] = {"tracks": sum(1 for t in tracks if t["species"] == sp), "max_in_one_frame": a["max_sp"][sp],
                           "boxes": a["boxes_sp"][sp]}
        return {"frames_tracked": a["frames"], "max_animals_in_one_frame": a["max_all"], "species": species,
                "untracked_boxes": a["untracked"], "tracks": sorted(tracks, key=lambda t: t["first_s"])}

    def end(self, now: float, t_s: float | None, wall_utc: str) -> float:
        gap = now - self._since
        self.spans[-1].update(end_t_s=t_s, end_utc=wall_utc, duration_s=round(gap, 2), analysis=self._analysis())
        self._since = None
        return gap

    def analysis(self) -> dict[str, Any] | None:
        """The package's no_position_analysis.json (None without a span)."""
        if not self.spans:
            return None
        tot: dict[str, Any] = {"spans": len(self.spans), "duration_s": round(sum(sp["duration_s"] or 0 for sp in
                                                                               self.spans), 2),
                               "frames_tracked": sum(sp["analysis"]["frames_tracked"] for sp in self.spans),
                               "images": len(self.images), "species": {}}
        for sp in self.spans:
            for k, v in sp["analysis"]["species"].items():
                t = tot["species"].setdefault(k, {"tracks": 0, "max_in_one_frame": 0})
                t["tracks"] += v["tracks"]
                t["max_in_one_frame"] = max(t["max_in_one_frame"], v["max_in_one_frame"])
        return {"schema_version": "hima.no_position/1", "note": self.NOTE, "total": tot, "spans": self.spans,
                "images": self.images}

    def due(self, now: float) -> bool:
        return now - self._last_img >= self.every_s - 1e-9

    def save(self, now: float, image, dets, t_s: float | None, wall_utc: str, last_fix, fix_age_s) -> None:
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise OSError("cannot encode the no-position image")
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"np-{len(self.images) + 1:05d}.jpg"
        (self.folder / name).write_bytes(buf.tobytes())
        entry = {"file": name, "t_s": t_s, "utc": wall_utc, "span_s": round(now - self._since, 2),
                 "last_fix": _fix_dict(last_fix, fix_age_s),
                 "detections": [{"species": self.class_map.get(d.class_raw, d.class_raw), "conf": round(d.conf, 3),
                                 "track_id": d.track_id, "bbox_xywh_px": [round(v, 1) for v in d.bbox_xywh_px]}
                                for d in dets]}
        with open(self.folder / "index.jsonl", "a", encoding="utf-8") as fh:   # one line per image: crash-safe
            fh.write(json.dumps(entry) + "\n")
        self.images.append(entry)
        self.spans[-1]["images"] += 1
        self._last_img = now

    def package_images(self) -> list[dict[str, Any]]:
        """For the package (packager extras): mission time from the first analysed frame, never below 0."""
        out = []
        for e in self.images:
            t = max(0.0, e["t_s"] or 0.0)
            fx = e["last_fix"]
            where = ("no fix before" if fx is None else
                     f"last fix {fx['lat_deg']:.6f}, {fx['lon_deg']:.6f}, {fx['rel_alt_m']:g} m, heading "
                     f"{fx['hdg_deg']}, {fx['age_s']} s old")
            out.append({"purpose": "no_position", "t": t, "path": self.folder / e["file"],
                        "alert": f"no position ({where}); {len(e['detections'])} animal box(es); {e['utc']}",
                        "window": None, "track_ids": []})
        return out

    def summary(self) -> dict[str, Any]:
        return {"spans": self.spans, "images": len(self.images), "every_s": self.every_s,
                "folder": NO_POSITION_DIR, "index": self.images}

    def short(self) -> dict[str, Any]:
        """For fly_metrics: the spans without the per-track lists."""
        spans = [{**sp, "analysis": None if sp["analysis"] is None else
                  {k: v for k, v in sp["analysis"].items() if k != "tracks"}} for sp in self.spans]
        return {"spans": spans, "images": len(self.images), "every_s": self.every_s, "folder": NO_POSITION_DIR}


@dataclass
class Check:
    name: str
    level: str                     # OK | WARN | FAIL
    detail: str


def _resolve(cfg: Config, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() or not cfg.source else Path(cfg.source).parent / q


def load_models(cfg: Config) -> tuple[Any, Any, Path, Path]:
    from ultralytics import YOLO
    a, g = weights_path(cfg), _resolve(cfg, cfg.objects.weights)
    for p in (a, g):
        if not p.is_file():
            raise OSError(f"weights not found: {p}")
    return YOLO(str(a)), YOLO(str(g)), a, g


# -- preflight --------------------------------------------------------------------------------------------
def _position_checks(cfg: Config, reader: TelemetryReader) -> list[Check]:
    out = []
    g = reader.gps
    f = cfg.flight
    if g is None:
        out.append(Check("gps", "FAIL", "no GPS_RAW_INT from the autopilot"))
    else:
        ok = g["fix_type"] >= f.min_fix_type and g["satellites"] >= f.min_satellites
        out.append(Check("gps", "OK" if ok else "FAIL", f"fix type {g['fix_type']} (need >= {f.min_fix_type}), "
                         f"{g['satellites']} satellites (need >= {f.min_satellites})"))
    out.append(Check("home", "OK" if reader.home else "FAIL",
                     f"home {reader.home}" if reader.home else "no HOME_POSITION (the local origin for distances)"))
    now = reader.boot_now()
    pose = None if now is None else reader.pose_at(now, cfg.mavlink.max_pose_age_s)
    out.append(Check("pose", "OK" if pose else "FAIL", f"pose {pose}" if pose else "no recent position with a heading"))
    return out


def _serial_ports_hint() -> str:
    """The serial ports this computer has, to put next to a port that would not open."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return " (pyserial is not installed: pip install -r requirements.txt)"
    ports = sorted(p.device for p in list_ports.comports())
    return f" (serial ports here: {', '.join(ports) if ports else 'none'})"


def check_link(cfg: Config, reader: TelemetryReader, settle_s: float = 3.0,
               log: Callable[[str], None] = lambda s: None) -> list[Check]:
    """HEARTBEAT, then GPS fix, HOME and a pose: these may take a while after power-on (GPS lock, EKF origin), so
    they are checked again until [flight].preflight_wait_s before a FAIL."""
    out = []
    if not reader.wait_heartbeat(cfg.mavlink.heartbeat_timeout_s):
        return [Check("pixhawk", "FAIL", f"no autopilot HEARTBEAT on {reader.url} in {cfg.mavlink.heartbeat_timeout_s:g}"
                      " s (wiring TX/RX/GND, SERIAL2_PROTOCOL = 2, SERIAL2_BAUD, the port name)")]
    reader.request_streams(cfg.mavlink.position_hz)
    t0 = time.monotonic()
    end = t0 + settle_s
    while time.monotonic() < end:
        reader.heartbeat()
        reader.pump(None)
        time.sleep(0.02)
    out.append(Check("pixhawk", "OK", f"autopilot {reader.autopilot} on {reader.url}, mode {reader.mode}, "
                     f"armed {reader.armed}, {reader.packets} packets in {settle_s:g} s"))
    pos = _position_checks(cfg, reader)
    last_ask = last_note = time.monotonic()
    while any(c.level == "FAIL" for c in pos) and time.monotonic() - t0 < cfg.flight.preflight_wait_s:
        reader.heartbeat()
        reader.pump(None)
        now = time.monotonic()
        if now - last_ask > 5.0:                     # HOME comes once the autopilot has its EKF origin: ask again
            reader.request_streams(cfg.mavlink.position_hz)
            last_ask = now
        if now - last_note > 10.0:
            log("  waiting for " + "; ".join(f"{c.name}: {c.detail}" for c in pos if c.level == "FAIL"))
            last_note = now
        time.sleep(0.05)
        pos = _position_checks(cfg, reader)
    out += pos
    if reader.battery:
        out.append(Check("battery", "OK", f"{reader.battery['remaining_pct']} %, {reader.battery['voltage_v']} V "
                         "(the autopilot's own failsafes decide; shown for the log)"))
    return out


def check_ai(cfg: Config, use_llm: bool, use_vlm: bool) -> list[Check]:
    if not (use_llm or use_vlm):
        return [Check("ai", "OK", "AI off for this flight (--no-llm --no-vlm)")]
    from hima.advisor import ollama_client as oc
    try:
        r = oc.requests.get(oc.base_url(cfg.llm) + "/api/tags", timeout=5)
        have = {m["name"] for m in r.json().get("models", [])}
    except Exception as e:                 # noqa: BLE001 - any failure: the flight goes on without the AI
        return [Check("ai", "WARN", f"Ollama not reachable at {cfg.llm.url} ({type(e).__name__}); the flight runs "
                      "with the AI off; alerts are rules and still work")]
    need = flight_models(cfg, use_llm, use_vlm) + ([cfg.report.model] if use_llm else [])
    missing = [m for m in need if m not in have and f"{m}:latest" not in have]
    if missing:
        return [Check("ai", "WARN", f"models not pulled: {missing} (ollama pull <name>); those parts fall back")]
    return [Check("ai", "OK", f"Ollama has {need}")]


def check_disk(cfg: Config, out_root: Path) -> Check:
    out_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out_root).free / 1e9
    ok = free >= cfg.flight.min_free_gb
    return Check("disk", "OK" if ok else "FAIL", f"{free:.1f} GB free in {out_root} (need {cfg.flight.min_free_gb:g})")


def check_gpu() -> Check:
    try:
        import torch
        if torch.cuda.is_available():
            return Check("gpu", "OK", f"CUDA: {torch.cuda.get_device_name(0)}")
        return Check("gpu", "WARN", "no CUDA GPU for torch: YOLO runs on the CPU (slow)")
    except Exception as e:                 # noqa: BLE001
        return Check("gpu", "WARN", f"torch not usable: {e}")


def print_checks(checks: list[Check], log: Callable[[str], None]) -> bool:
    for c in checks:
        log(f"  [{c.level:4}] {c.name:8} {c.detail}")
    return not any(c.level == "FAIL" for c in checks)


# -- the flight ------------------------------------------------------------------------------------------
class StopFlag:
    """Set by Ctrl+C / SIGTERM (systemd stop): the flight lands cleanly instead of dying."""

    def __init__(self, install: bool = True):
        self.event = threading.Event()
        self.why: str | None = None
        if install and threading.current_thread() is threading.main_thread():
            for s in (signal.SIGINT, signal.SIGTERM):
                signal.signal(s, self._handler)

    def _handler(self, signum, frame):
        self.set(signal.Signals(signum).name)

    def set(self, why: str) -> None:
        self.why = self.why or why
        self.event.set()

    def __bool__(self) -> bool:
        return self.event.is_set()


def fly(cfg: Config, *, out: str | Path | None = None, url: str | None = None, baud: int | None = None,
        device: str | None = None, use_llm: bool = True, use_vlm: bool = True, wait_arm: bool | None = None,
        max_frames: int | None = None, models: tuple[Any, Any] | None = None, ai_generate=None, generate_fn=None,
        stop: StopFlag | None = None, preflight_only: bool = False,
        log: Callable[[str], None] = lambda s: print(s, flush=True)) -> dict[str, Any]:
    """models: (animal, general) already loaded (tests). ai_generate / generate_fn: in-flight AI and landing report
    (None = Ollama per hima.toml). Returns the metrics (also written to <out>/fly_metrics.json)."""
    stop = stop if stop is not None else StopFlag()
    wait_arm = cfg.flight.start_on_arm if wait_arm is None else wait_arm
    out_root = _resolve(cfg, cfg.flight.out_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out) if out is not None else out_root / f"flight-{stamp}"
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")

    log(f"HIMA flight {stamp}: config {cfg.source} (sha256 {cfg.sha256[:12]})")
    checks: list[Check] = [Check("config", "OK", f"{cfg.source}")]
    if models is None:
        t0 = time.perf_counter()
        try:
            animal, general, apath, gpath = load_models(cfg)
        except OSError as e:
            checks.append(Check("weights", "FAIL", str(e)))
            print_checks(checks, log)
            return {"ok": False, "checks": [asdict(c) for c in checks]}
        load_s = time.perf_counter() - t0
        wname, gname = apath.name, gpath.name
    else:
        animal, general = models
        load_s, wname, gname = None, "test-model", "test-general"
    try:
        check_class_names(dict(animal.names), cfg.detector.class_map)
        checks.append(Check("weights", "OK", f"{wname} {dict(animal.names)}; general {gname}"
                            + (f"; loaded in {load_s:.1f} s" if load_s else "")))
    except ValueError as e:
        checks.append(Check("weights", "FAIL", str(e)))

    cam = reader = None
    try:
        try:
            cam = Camera(device or cfg.camera.device, cfg.camera.width_px, cfg.camera.height_px)
            asked = (cfg.camera.width_px, cfg.camera.height_px)
            got = (cam.width_px, cam.height_px)
            checks.append(Check("camera", "OK" if got == asked or cam.is_file else "WARN",
                                f"{cam.device}: {got[0]}x{got[1]}" + ("" if got == asked else f" (asked {asked[0]}x"
                                f"{asked[1]}; hfov {cfg.camera.hfov_deg:g} deg is used for the frame's full width)")))
        except CameraError as e:
            checks.append(Check("camera", "FAIL", str(e)))
        try:
            reader = TelemetryReader(url or cfg.mavlink.url, baud or cfg.mavlink.baud)
        except Exception as e:                                # a wrong port name, a port in use, no pyserial
            checks.append(Check("pixhawk", "FAIL", f"cannot open {url or cfg.mavlink.url}: {type(e).__name__}: {e}"
                                f"{_serial_ports_hint()}"))
        if reader is not None:
            checks += check_link(cfg, reader, log=log)
        checks.append(check_disk(cfg, out_root))
        checks.append(check_gpu())
        ai_checks = check_ai(cfg, use_llm, use_vlm) if ai_generate is None else [Check("ai", "OK", "test AI")]
        checks += ai_checks
        log("preflight:")
        ok = print_checks(checks, log)
        if not ok or preflight_only:
            if not ok:
                log("preflight FAILED: no flight. Fix the FAIL lines and run again.")
            return {"ok": ok, "checks": [asdict(c) for c in checks]}
        if any(c.name == "ai" and c.level == "WARN" and "not reachable" in c.detail for c in ai_checks):
            use_llm = use_vlm = False

        # autonomy: plan, upload, safety parameters; a FAIL here is a FAIL of the preflight
        auto = FlightAutonomy(cfg, reader, out, log)
        try:
            auto_checks = [Check(*c) for c in auto.prepare()]
        except Exception as e:                                # noqa: BLE001
            auto_checks = [Check("autonomy", "FAIL", f"{type(e).__name__}: {e}")]
        checks += auto_checks
        if not print_checks(auto_checks, log):
            log("autonomy preflight FAILED: no flight. Fix the FAIL lines and run again.")
            return {"ok": False, "checks": [asdict(c) for c in checks], "autonomy": auto.summary()}

        # warm-up: the first YOLO call is slow (4 s); do it on the ground, not over the first animal
        imgsz = model_imgsz(cfg, cam.width_px)
        _, _, img0 = cam.latest()
        t0 = time.perf_counter()
        animal.predict(img0, imgsz=imgsz, conf=cfg.detector.conf, verbose=False)
        general.predict(img0, imgsz=cfg.objects.imgsz, verbose=False)
        warm_s = time.perf_counter() - t0
        log(f"YOLO warm-up {warm_s:.1f} s")

        ai = prep = release = None
        fmodels = flight_models(cfg, use_llm, use_vlm)
        if use_llm or use_vlm:
            ai = AiGateway(ai_generate or ollama_generate(cfg))
            if ai_generate is None:
                prep = prepare_flight_models(cfg, fmodels)
                log(f"AI models: loaded " + ", ".join(f"{x['model']} {x['load_s']} s" if x["error"] is None
                                                      else f"{x['model']} FAILED" for x in prep["loaded"]))

        if wait_arm:
            log("waiting for the autopilot to ARM (Ctrl+C to quit) ...")
            last_note = time.monotonic()
            while not reader.armed and not stop:
                reader.heartbeat()
                reader.pump(None)
                if time.monotonic() - last_note > 10:
                    log(f"  still disarmed (mode {reader.mode}, GPS {reader.gps})")
                    last_note = time.monotonic()
                time.sleep(0.05)
            if stop:
                log(f"stopped before arming ({stop.why})")
                return {"ok": True, "flown": False, "checks": [asdict(c) for c in checks]}
            log(f"ARMED, mode {reader.mode}: recording")

        run = _fly_loop(cfg, cam, reader, animal, general, ai, use_llm, use_vlm, out, wait_arm, max_frames,
                        stop, wname, imgsz, log, auto)
        cam.close()
        if prep is not None:
            release = release_flight_models(cfg, fmodels)
        return _landing(cfg, out, run, checks, use_llm, use_vlm, generate_fn,
                        {"animal": wname, "general": gname, "load_s": load_s, "warm_up_s": round(warm_s, 2),
                         "periodic": cfg.periodic.model if use_llm else None,
                         "window": cfg.window.model if use_vlm else None, "ai_prepare": prep, "ai_release": release},
                        log)
    finally:
        if cam is not None:
            cam.close()
        if reader is not None:
            reader.close()


def _fly_loop(cfg, cam, reader, animal, general, ai, use_llm, use_vlm, out: Path, wait_arm: bool,
              max_frames, stop: StopFlag, wname: str, imgsz: int, log, auto: FlightAutonomy | None = None
              ) -> dict[str, Any]:
    names = dict(animal.names)
    w, h = cam.width_px, cam.height_px
    stamp = datetime.now(timezone.utc)
    rec = FlightMissionRecorder(
        mission_id=f"flight-{stamp.strftime('%Y%m%dT%H%M%SZ')}", source="real",
        camera={"width_px": w, "height_px": h, "hfov_deg": cfg.camera.hfov_deg, "mount": "nadir",
                "params_known": True},
        detector={"weights": wname, "class_map": dict(cfg.detector.class_map), "conf": cfg.detector.conf,
                  "tracker": cfg.detector.tracker, "imgsz": imgsz},
        geometry_assumptions=("flat_ground", "nadir_camera"), created_at=stamp)
    if reader.home is not None:            # the origin of x_m / y_m: GPS position of each animal found
        rec.meta["home"] = {"lat_deg": float(reader.home[0]), "lon_deg": float(reader.home[1])}
    flight = OnboardFlight(rec.meta, cfg, general_model=general, ai=ai, periodic=True,
                           windows=use_vlm and ai is not None, periodic_ai=use_llm)
    video = None
    writer = None
    if cfg.flight.save_video:
        video = out / "flight" / "flight.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*cfg.triage.clip_fourcc), cfg.camera.fps, (w, h))
        if not writer.isOpened():
            raise OSError(f"cannot write {video} with fourcc {cfg.triage.clip_fourcc}")
    timing: dict[str, list[float]] = {"track_s": [], "onboard_s": [], "frame_s": []}
    counts = {"no_pose": 0, "low_agl": 0, "no_new_frame": 0, "frame_errors": 0, "statustext_sent": 0,
              "statustext_bytes": 0}
    errors: list[str] = []
    sent_texts: list[str] = []
    forwarded = 0
    boot0 = None
    last_t = -1.0
    step = 1.0 / cfg.camera.fps
    next_slot = time.monotonic()
    last_cam = 0
    wall0 = time.monotonic()
    end_why = None
    link_warned = False
    nopos = NoPosition(out / "flight" / NO_POSITION_DIR, cfg.flight.no_position_image_every_s,
                       cfg.triage.jpeg_quality, dict(cfg.detector.class_map))
    raw = RawLog(out / "flight")
    raw.write("start", mission_id=rec.meta["mission_id"], utc=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
              camera=rec.meta["camera"], detector=rec.meta["detector"], home=rec.meta.get("home"),
              general_model=cfg.objects.weights, config_sha256=cfg.sha256)
    n_obj = 0

    def utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"

    def raw_frame(t_s, pose_, used: str, dets) -> None:
        g = reader.gps or {}
        b = reader.battery or {}
        raw.write("frame", wall_s=round(time.monotonic() - wall0, 3), utc=utc(), t=t_s,
                  pose=None if pose_ is None else {"x_m": round(pose_.x_m, 2), "y_m": round(pose_.y_m, 2),
                                                   "agl_m": round(pose_.agl_m, 2),
                                                   "hdg_deg": round(pose_.heading_deg, 1)},
                  mode=reader.mode, armed=reader.armed, batt_pct=b.get("remaining_pct"),
                  gps=[g.get("fix_type"), g.get("satellites")] if g else None, used=used, det=raw_boxes(dets))

    def raw_objects() -> None:
        nonlocal n_obj
        ob = flight.objects
        if ob is None:
            return
        for smp in ob.samples[n_obj:]:
            raw.write("objects", t=smp["t"], boxes=[[o["cls"], o["group"], o["conf"], *o["bbox_xywh_px"],
                                                     o["on_animal"], o["kept"]] for o in smp["boxes"]])
        n_obj = len(ob.samples)

    def mission_t(boot: float | None) -> float | None:
        if boot is None:
            return None
        return round(boot - boot0, 3) if boot0 is not None else None

    def tell(text: str) -> None:
        """A system message straight to the Pixhawk; with the link down it may not arrive (or the port may fail)."""
        try:
            n = reader.send_statustext(text, 4, 0, 0)          # MAV_SEVERITY_WARNING
        except Exception as e:                                  # noqa: BLE001 - a dead link must not stop the flight
            log(f"  could not send {text!r} ({type(e).__name__}: {e})")
            return
        counts["statustext_sent"] += 1
        counts["statustext_bytes"] += n
        sent_texts.append(text)
        raw.write("text", wall_s=round(time.monotonic() - wall0, 3), text=text)

    def forward() -> None:
        nonlocal forwarded
        for d in flight.deliveries[forwarded:]:
            a = d.alert
            n = reader.send_statustext(a.text, getattr(a, "severity", 6), getattr(a, "msg_id", 0),
                                       getattr(a, "chunk_seq", 0))
            counts["statustext_sent"] += 1
            counts["statustext_bytes"] += n
            sent_texts.append(a.text)
            raw.write("text", wall_s=round(time.monotonic() - wall0, 3), text=a.text)
        forwarded = len(flight.deliveries)

    def steer(texts: list[str]) -> None:
        for s_ in texts:
            log(f"  autonomy: {s_}")
            tell(s_)

    try:
        while True:
            reader.heartbeat()
            reader.pump(None)
            if stop:
                end_why = stop.why
                break
            if wait_arm and reader.armed is False:
                end_why = "disarmed"
                break
            age = reader.heartbeat_age()
            if age is not None and age > cfg.mavlink.heartbeat_timeout_s and not link_warned:
                log(f"  WARNING: no autopilot HEARTBEAT for {age:.0f} s: the flight goes on without a position "
                    f"(the disarm cannot be seen; it ends when the camera stops, on Ctrl+C or at max_flight_s)")
                link_warned = True
            elif age is not None and age <= cfg.mavlink.heartbeat_timeout_s:
                link_warned = False
            if cam.ended:
                end_why = f"camera: {cam.error}"
                break
            if time.monotonic() - wall0 > cfg.flight.max_flight_s:
                end_why = "max_flight_s"
                break
            if max_frames is not None and len(rec.frames) >= max_frames:
                end_why = "max_frames"
                break
            now = time.monotonic()
            if now < next_slot:
                time.sleep(min(next_slot - now, 0.01))
                continue
            next_slot = max(next_slot + step, now - step)      # never a burst to catch up
            got = cam.latest(last_cam)
            if got is None:
                counts["no_new_frame"] += 1
                continue
            last_cam, _, image = got
            c0 = time.perf_counter()
            boot = reader.boot_now()
            pose = None if boot is None else reader.pose_at(boot, cfg.mavlink.max_pose_age_s)
            if pose is None:
                counts["no_pose"] += 1                      # no telemetry: better no analysis than a wrong place
                now = time.monotonic()
                t_np = mission_t(boot)
                fix = reader.fixes[-1] if reader.fixes else None
                fix_age = None if fix is None or boot is None else boot - fix.t
                if not nopos.active:
                    nopos.start(now, t_np, utc(), _fix_dict(fix, fix_age))
                    raw.write("event", wall_s=round(now - wall0, 3), utc=utc(), t=t_np, what="no_position")
                    log(f"  NO POSITION at t {t_np if t_np is not None else '?'} s: no-position mode (every frame is "
                        f"tracked, no places); one image every {nopos.every_s:g} s is saved in {nopos.folder}")
                    tell("HIMA NO POSITION" + (f" @{t_np:.0f}" if t_np is not None else ""))
                try:
                    dets = _detections(animal.track(image, persist=True, conf=cfg.detector.conf,
                                                    tracker=cfg.detector.tracker, imgsz=imgsz, verbose=False)[0],
                                       names)
                    nopos.observe(now, dets)
                    raw_frame(t_np, None, "no_position", dets)
                    if nopos.due(now):
                        nopos.save(now, image, dets, t_np, utc(), fix, fix_age)
                except Exception as e:                      # noqa: BLE001 - never lose the flight over one frame
                    counts["frame_errors"] += 1
                    if counts["frame_errors"] <= 3:
                        log(f"  ERROR in the no-position mode ({type(e).__name__}: {e})")
                        errors.append(f"no position: {type(e).__name__}: {e}"[:300])
                if auto is not None:
                    steer(auto.no_pose(t_np))
                continue
            if nopos.active:
                gap = nopos.end(time.monotonic(), mission_t(boot), utc())
                raw.write("event", wall_s=round(time.monotonic() - wall0, 3), utc=utc(), t=mission_t(boot),
                          what="position_back", gap_s=round(gap, 2))
                an = nopos.spans[-1]["analysis"]
                log(f"  position back after {gap:.1f} s ({nopos.spans[-1]['images']} images saved; "
                    f"{an['frames_tracked']} frames tracked, up to {an['max_animals_in_one_frame']} animals together)")
                tell(f"HIMA POSITION BACK @{mission_t(boot) or 0:.0f} {gap:.0f}s")
            if boot0 is None:
                boot0 = boot
            t = round(boot - boot0, 3)
            if t <= last_t:
                continue
            if pose.agl_m < cfg.flight.min_agl_m:
                counts["low_agl"] += 1                      # taking off or landing: not analysed (seen in SITL)
                raw_frame(t, pose, "low_agl", [])
                continue
            last_t = t
            try:
                res = animal.track(image, persist=True, conf=cfg.detector.conf, tracker=cfg.detector.tracker,
                                   imgsz=imgsz, verbose=False)[0]
                dets = _detections(res, names)
                rec.add_frame(t, {"x_m": pose.x_m, "y_m": pose.y_m, "agl_m": pose.agl_m,
                                  "heading_deg": pose.heading_deg}, dets)
                raw_frame(t, pose, "analysed", dets)
                if writer is not None:
                    writer.write(image)                     # video frame k = session frame k (package)
                c1 = time.perf_counter()
                flight.push(rec.frames[-1], image)
                raw_objects()
                forward()
            except Exception as e:                          # noqa: BLE001 - one bad frame must never lose the flight
                counts["frame_errors"] += 1
                if counts["frame_errors"] <= 3:
                    log(f"  ERROR at t {t:.1f} s ({type(e).__name__}: {e}); the frame is skipped, the flight goes on")
                    errors.append(f"t {t:.3f}: {type(e).__name__}: {e}"[:300])
                continue
            c2 = time.perf_counter()
            if auto is not None:
                try:
                    steer(auto.frame(t, pose, image, flight, rec.meta["camera"]))
                except Exception as e:                      # noqa: BLE001 - ArduPilot flies the route on its own
                    counts["frame_errors"] += 1
                    if counts["frame_errors"] <= 3:
                        log(f"  ERROR in autonomy at t {t:.1f} s ({type(e).__name__}: {e})")
                        errors.append(f"autonomy t {t:.3f}: {type(e).__name__}: {e}"[:300])
            for k, v in (("track_s", c1 - c0), ("onboard_s", c2 - c1), ("frame_s", c2 - c0)):
                timing[k].append(v)
            if len(rec.frames) % max(1, int(cfg.camera.fps * 30)) == 0:
                log(f"  t {t:7.1f} s: {len(rec.frames[-1]['detections'])} tracked, alerts {len(flight.alerts)}, "
                    f"messages {counts['statustext_sent']}, frame {timing['frame_s'][-1] * 1000:.0f} ms, "
                    f"mode {reader.mode}")
        if nopos.active:
            nopos.end(time.monotonic(), mission_t(reader.boot_now()), utc())
        raw.write("event", wall_s=round(time.monotonic() - wall0, 3), utc=utc(), what="end", why=end_why)
        log(f"flight ended: {end_why}; {len(rec.frames)} frames"
            + (f"; {len(nopos.images)} no-position images in {len(nopos.spans)} span(s)" if nopos.spans else ""))
        if not rec.frames:
            raise RuntimeError(f"no frame was recorded ({counts}); nothing to land")
        raw_session = out / "sessions" / f"{rec.meta['mission_id']}.raw.session.json"
        rec.save(raw_session)                                       # the raw record is safe before any landing step runs
        final = flight.land()
        forward()
    finally:
        if writer is not None:
            writer.release()
        raw.close()
    return {"rec": rec, "raw": raw, "flight": flight, "final": final, "timing": timing, "counts": counts, "errors": errors,
            "sent": sent_texts,
            "video": video, "end": end_why, "wall_s": time.monotonic() - wall0, "no_position": nopos,
            "camera": {"device": cam.device, "width_px": w, "height_px": h, "frames_read": cam.frames_read,
                       "frames_taken": cam.taken, "source_fps": cam.source_fps},
            "link": reader.state(), "autonomy": auto.summary() if auto is not None else None,
            "vehicle_photos": auto.photos.package_images() if auto is not None else []}


def _landing(cfg: Config, out: Path, run: dict[str, Any], checks: list[Check], use_llm: bool, use_vlm: bool,
             generate_fn, models: dict[str, Any], log) -> dict[str, Any]:
    fl: OnboardFlight = run["flight"]
    rec: FlightMissionRecorder = run["rec"]
    doc = {**rec.to_session(), "analysis": run["final"]}
    out.mkdir(parents=True, exist_ok=True)
    sess = out / "sessions" / f"{doc['meta']['mission_id']}.session.json"
    save_session(doc, sess)
    movement = movement_summary(doc, run["final"])
    flight_log = fl.flight_log(telemetry=run["link"], movement=movement,
                               mavlink={"statustext_sent": run["counts"]["statustext_sent"],
                                        "wire_bytes": run["counts"]["statustext_bytes"]},
                               flight={"end": run["end"], "camera": run["camera"],
                                       "preflight": [asdict(c) for c in checks], "counts": run["counts"],
                                       "frame_errors": run["errors"],
                                       "no_position": run["no_position"].summary(),
                                       "raw_flight": run["raw"].summary(),
                                       "autonomy": run.get("autonomy")})
    t0 = time.perf_counter()
    raw_gz = run["raw"].compress_to(out / "flight" / RAW_PACKAGE_FILE)
    photos = run.get("vehicle_photos") or []
    pkg, manifest = build_package(doc, cfg, out / "packages", video=run["video"], untracked=rec.untracked,
                                  use_llm=use_llm, generate_fn=generate_fn,
                                  extras={"flight_log": flight_log,
                                          "images": fl.images() + run["no_position"].package_images() + photos,
                                          "raw_flight": raw_gz,
                                          "no_position_analysis": run["no_position"].analysis()},
                                  video_timing="frames")
    package_s = time.perf_counter() - t0
    rep = json.loads((pkg / "report.json").read_text(encoding="utf-8"))
    tm = run["timing"]
    metrics = {
        "schema_version": "hima.fly_metrics/1", "ok": True, "flown": True,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_sha256": cfg.sha256, "use_llm": use_llm, "use_vlm": use_vlm, "end": run["end"],
        "machine": {"platform": platform.platform(), "python": platform.python_version(), "gpu": _gpu()},
        "models": models, "checks": [asdict(c) for c in checks],
        "frames": len(doc["frames"]), "mission_s": doc["frames"][-1]["t"], "wall_s": round(run["wall_s"], 2),
        "camera": run["camera"], "counts": run["counts"], "frame_errors": run["errors"],
        "no_position": run["no_position"].short(),
        "raw_flight": {**run["raw"].summary(), "package_file": RAW_PACKAGE_FILE},
        "vehicle_photos": len(run.get("vehicle_photos") or []),
        "timing": {k: dist(v) for k, v in tm.items()},
        "autonomy": run.get("autonomy"),
        "alerts": [a.text for a in fl.alerts], "vehicle_alerts": [a.text for a in fl.object_alerts],
        "dwell_alerts": [a.text for a in fl.dwell.alerts], "statustext": run["sent"],
        "package": {"dir": str(pkg), "package_s": round(package_s, 2), "used_bytes": manifest["budget"]["used_bytes"],
                    "report_source": rep["source"], "report_headline": rep["report"]["headline"]},
        "session": str(sess),
    }
    (out / METRICS_FILE).write_text(json.dumps(metrics, indent=1, default=str), encoding="utf-8")
    log(f"landed: {metrics['frames']} frames, {metrics['mission_s']:g} s; {len(fl.alerts)} animal alerts, "
        f"{len(fl.object_alerts)} vehicle alerts, {len(fl.dwell.alerts)} dwell; {len(run['sent'])} STATUSTEXT sent")
    log(f"package for the ground station: {pkg}")
    return metrics
