"""Autonomy in the live flight: fly.py calls this; it joins the plan, the behaviour, the rangers and the
executor.

Before arming (prepare):
  - only for ArduPlane (HEARTBEAT type fixed wing / VTOL) and [behaviour].enabled; a copter flies as before
  - the launch point = the [reserve.launch] point nearest the autopilot's HOME (within launch_match_m)
  - today's plan (planner.best_plan from that point) is written into <flight>/plan/
  - with [behaviour].upload_plan: fence and mission are uploaded, then the safety parameters are read back
    (fence, and the Jetson-failure RTL: MAV_GCS_SYSID / FS_GCS_ENABL / FS_LONG_ACTN); any difference is a FAIL
In flight (frame, every analysed frame; no_pose when there is none):
  animals: registered animals of the frame (identity.live_detections), placed on the ground (pixel_to_ground)
  vehicles: kept VEHICLE boxes of a new general-model sample, placed the same way; owner from rangers.owner
  then Behaviour.tick and the Executor sends its mode / guided commands; the texts go to the ground station.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from hima.analytics.geometry import Camera, DronePose, pixel_to_ground
from hima.autonomy import history
from hima.autonomy.behaviour import FOLLOW_VEHICLE, Behaviour
from hima.autonomy.executor import Executor, safety_params
from hima.autonomy.planner import best_plan
from hima.autonomy.rangers import MarkerReader, haversine_m, load_ops, owner
from hima.autonomy.reserve import Reserve, resolve
from hima.config import Config
from hima.telemetry.geo import to_latlon

LAUNCH_MATCH_M = 1000.0
VEHICLE_DIR = "vehicle_follow"


class VehiclePhotos:
    """Following a vehicle: one camera image every every_s, as taken (no boxes drawn), with the
    drone's place and the vehicle's and herd's places. Written at once (index.jsonl one line per image: crash-safe);
    nothing here deletes them, and the package keeps them at priority 0 (never dropped by the storage budget)."""

    def __init__(self, folder: Path, every_s: float, jpeg_quality: int):
        self.folder, self.every_s, self.quality = folder, every_s, jpeg_quality
        self.images: list[dict[str, Any]] = []
        self._last = -1e9

    def due(self, t: float) -> bool:
        return t - self._last >= self.every_s - 1e-9

    def save(self, t: float, image, entry: dict[str, Any]) -> None:
        import cv2
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise OSError("cannot encode the vehicle image")
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f"vf-{len(self.images) + 1:05d}.jpg"
        (self.folder / name).write_bytes(buf.tobytes())
        e = {"file": name, "t_s": round(t, 2), **entry}
        with open(self.folder / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e) + "\n")
        self.images.append(e)
        self._last = t

    def package_images(self) -> list[dict[str, Any]]:
        out = []
        for e in self.images:
            d = e["drone"]
            herd = e.get("herd_m")
            alert = (f"following vehicle {e['follow']}: drone {d['lat_deg']:.6f}, {d['lon_deg']:.6f}, {d['agl_m']:g} m"
                     + ("" if herd is None else f"; vehicle {herd:.0f} m from the herd")
                     + (f"; {len(e['vehicle_boxes_px'])} vehicle box(es)" if e["vehicle_boxes_px"] else "")
                     + f"; {e['utc']}")
            out.append({"purpose": "vehicle_follow", "t": max(0.0, e["t_s"]), "path": self.folder / e["file"],
                        "alert": alert, "window": None, "track_ids": []})
        return out


class FlightAutonomy:
    def __init__(self, cfg: Config, reader: Any, out: Path, log: Callable[[str], None] = print,
                 today: dt.date | None = None, station_client: Any = None):
        self.cfg, self.reader, self.out, self.log = cfg, reader, Path(out), log
        self.station_client = station_client                     # tests; None = [delivery] (route review)
        self.today = today or dt.datetime.now(dt.timezone.utc).date()
        self.active = False
        self.reason = ""
        self.plan_info: dict[str, Any] = {}
        self.behaviour: Behaviour | None = None
        self.executor: Executor | None = None
        self.reserve: Reserve | None = None
        self.ops: list = []
        self.markers: MarkerReader | None = None
        self.mission_items = 0
        self._obj_samples = 0
        self.vehicles_seen: list[dict[str, Any]] = []
        self.photos = VehiclePhotos(self.out / "flight" / VEHICLE_DIR, cfg.behaviour.vehicle_photo_every_s,
                                    cfg.triage.jpeg_quality)
        self._last_vehicle_px: tuple[float, list] | None = None

    # ---- before arming ------------------------------------------------------------------------------
    def prepare(self) -> list[tuple[str, str, str]]:
        """Returns preflight lines (name, level, detail)."""
        cfg, r = self.cfg, self.reader
        if not cfg.behaviour.enabled:
            self.reason = "off ([behaviour].enabled = false)"
            return [("autonomy", "OK", self.reason)]
        if r.vehicle_kind != "plane":
            self.reason = f"off: the autopilot is a {r.vehicle_kind or 'unknown vehicle'} (autonomy is for ArduPlane)"
            return [("autonomy", "WARN", self.reason)]
        if r.home is None:
            return [("autonomy", "FAIL", "no HOME position: the launch point is not known")]
        try:
            self.reserve = Reserve.load(cfg)
        except Exception as e:                                    # noqa: BLE001
            return [("autonomy", "FAIL", f"reserve boundary: {type(e).__name__}: {e}")]
        near = min(cfg.reserve.launch.items(), key=lambda kv: haversine_m(r.home, kv[1]))
        d = haversine_m(r.home, near[1])
        if d > LAUNCH_MATCH_M:
            return [("autonomy", "FAIL", f"HOME is {d:.0f} m from the nearest launch point ({near[0]}); "
                     f"take off from a [reserve.launch] point")]
        hist = history.load(resolve(cfg, cfg.reserve.history))
        from hima.autonomy.export import write_plan
        plan, plans, sc = best_plan(cfg, self.reserve, hist, self.today, near[0])
        files = write_plan(self.out / "plan", cfg, self.reserve, plan, sc, plans)
        self.plan_info = {**plan.summary(), "dir": str(self.out / "plan"), "launch_offset_m": round(d, 1)}
        out = [("plan", "OK", f"launch {near[0]} ({d:.0f} m from HOME): {len(plan.targets_xy)} targets, "
                f"{plan.length_m / 1e3:.1f} km, {plan.summary()['herd_time_min']} min kept for herds")]
        self.executor = Executor(r, r.home, self.log)
        if cfg.behaviour.upload_plan:
            up = self.executor.upload_plan(self.out / "plan")
            self.plan_info["upload"] = up
            self.mission_items = up["mission_items"]
            out.append(("upload", "OK" if up["fence"] and up["mission"] else "FAIL",
                        f"fence {up['fence_items']} items {'accepted' if up['fence'] else 'NOT accepted'}, "
                        f"mission {up['mission_items']} items {'accepted' if up['mission'] else 'NOT accepted'}"))
            rr = cfg.route_review
            if rr.enabled and rr.wait_s > 0 and up["fence"] and up["mission"]:
                from hima.autonomy.review import RouteReview
                rv = RouteReview(cfg, r, self.executor, self.reserve, plan, sc, self.out, self.today, self.log,
                                 client=self.station_client)
                plan = rv.run()
                self.plan_info.update({**plan.summary(), "review": rv.info})
                from hima.autonomy.export import read_waypoints
                self.mission_items = len(read_waypoints(self.out / "plan" / "mission.waypoints"))
                out.append(("route", "OK", f"{rv.info['used']} route ({rv.info['reason']}); "
                            f"{len(plan.targets_xy)} points, {plan.length_m / 1e3:.1f} km"))
            bad = self.executor.check_params(safety_params(cfg, r.src_system))
            self.plan_info["param_check"] = bad
            out.append(("safety params", "FAIL" if bad else "OK",
                        "; ".join(bad) if bad else "fence on; Jetson heartbeat lost -> RTL (GCS failsafe)"))
        else:
            from hima.autonomy.export import read_waypoints
            self.mission_items = len(read_waypoints(self.out / "plan" / "mission.waypoints"))
        if cfg.rangers.enabled:
            rf = resolve(cfg, cfg.rangers.field_ops)
            self.ops = load_ops(rf)
            try:
                self.markers = MarkerReader(cfg)
            except Exception as e:                                # noqa: BLE001 - OpenCV without aruco
                out.append(("rangers", "WARN", f"roof markers off ({type(e).__name__}: {e})"))
            out.append(("rangers", "OK", f"{len(self.ops)} field-work entries; roof markers "
                        f"{'on' if self.markers else 'off'}"))
        else:
            out.append(("rangers", "OK", "off: one vehicle class, every vehicle near a herd is followed"))
        self.behaviour = Behaviour(cfg, plan.budget, is_safe=self.is_safe)
        self.active = True
        self.reason = "on"
        return out

    def is_safe(self, x: float, y: float) -> bool:
        """A flight-local point (east, north from HOME) is inside the planner's safe area."""
        if self.reserve is None or self.reader.home is None:
            return False
        lat, lon = to_latlon(x, y, *self.reader.home)
        k = self.reserve.cell_of(*self.reserve.to_xy(lat, lon))
        return k is not None and bool(self.reserve.safe[k])

    # ---- in flight ----------------------------------------------------------------------------------
    def _utc(self, t: float) -> dt.datetime:
        r = self.reader
        boot = r.boot_now()
        if r.time_offset_s is not None and boot is not None:
            return dt.datetime.fromtimestamp(r.time_offset_s + boot, dt.timezone.utc)
        return dt.datetime.now(dt.timezone.utc)

    def frame(self, t: float, pose: DronePose, image, flight, meta_camera: dict) -> list[str]:
        if not self.active:
            return []
        cam = Camera.from_meta(meta_camera)
        b = self.behaviour
        pts = []
        for d in flight.identity.live_detections():
            g = pixel_to_ground(d["bbox_xywh_px"][0], d["bbox_xywh_px"][1], cam, pose)
            pts.append((d["track_id"], d.get("species", "?"), g.x_m, g.y_m))
        b.see_animals(t, pts)
        ob = flight.objects
        if ob is not None and len(ob.samples) > self._obj_samples:
            self._obj_samples = len(ob.samples)
            vs = []
            for o in ob.last_kept:
                if o.group != "VEHICLE":
                    continue
                g = pixel_to_ground(o.bbox_xywh_px[0], o.bbox_xywh_px[1], cam, pose)
                lat, lon = to_latlon(g.x_m, g.y_m, *self.reader.home)
                who = (owner(self.cfg, self.ops, self.markers, image, o.bbox_xywh_px, lat, lon, self._utc(t))
                       if self.cfg.rangers.enabled else None)
                vs.append((g.x_m, g.y_m, who))
                if len(self.vehicles_seen) < 500:
                    self.vehicles_seen.append({"t": round(t, 1), "x_m": round(g.x_m, 1), "y_m": round(g.y_m, 1),
                                               "owner": who})
            b.see_vehicles(t, vs)
            self._last_vehicle_px = (t, [list(o.bbox_xywh_px) for o in ob.last_kept if o.group == "VEHICLE"])
        out = self._tick(t, (pose.x_m, pose.y_m, pose.agl_m))
        if b.state == FOLLOW_VEHICLE and self.photos.due(t):
            self._photo(t, pose, image)
        return out

    def _photo(self, t: float, pose: DronePose, image) -> None:
        b, home = self.behaviour, self.reader.home
        f = b._follow or {}
        lat, lon = to_latlon(pose.x_m, pose.y_m, *home)
        c, herd = f.get("_center"), f.get("_herd")
        seen = self._last_vehicle_px
        boxes = seen[1] if seen is not None and t - seen[0] <= self.photos.every_s else []
        d = None if c is None or herd is None else ((c[0] - herd[0]) ** 2 + (c[1] - herd[1]) ** 2) ** 0.5
        self.photos.save(t, image, {
            "utc": self._utc(t).strftime("%Y-%m-%dT%H:%M:%SZ"), "follow": len(b.follows) + 1,
            "drone": {"x_m": round(pose.x_m, 1), "y_m": round(pose.y_m, 1), "agl_m": round(pose.agl_m, 1),
                      "lat_deg": round(lat, 7), "lon_deg": round(lon, 7), "heading_deg": round(pose.heading_deg, 1)},
            "vehicle_xy_m": None if c is None else [round(c[0], 1), round(c[1], 1)],
            "herd_m": None if d is None else round(d, 1),
            "herd_xy_m": None if herd is None else [round(herd[0], 1), round(herd[1], 1)],
            "vehicle_boxes_px": [[round(v, 1) for v in bb] for bb in boxes],
            "hovering": b.hovering((pose.x_m, pose.y_m, pose.agl_m))})

    def no_pose(self, t: float | None) -> list[str]:
        if not self.active or t is None:
            return []
        return self._tick(t, None)

    def _tick(self, t: float, pose) -> list[str]:
        r = self.reader
        batt = (r.battery or {}).get("remaining_pct")
        batt = None if batt is None or batt < 0 else float(batt)
        landing = r.mission_seq is not None and self.mission_items > 1 and r.mission_seq >= self.mission_items - 1
        cmds = self.behaviour.tick(t, pose, r.mode, batt, landing)
        return self.executor.apply(t, cmds) if cmds else []

    def summary(self) -> dict[str, Any]:
        s: dict[str, Any] = {"active": self.active, "reason": self.reason, "plan": self.plan_info}
        if self.behaviour is not None:
            s["behaviour"] = self.behaviour.summary()
        if self.executor is not None:
            s["commands"] = self.executor.sent[-500:]
        s["vehicles_seen"] = self.vehicles_seen
        s["vehicle_photos"] = {"folder": VEHICLE_DIR, "every_s": self.photos.every_s, "images": len(self.photos.images)}
        return s
