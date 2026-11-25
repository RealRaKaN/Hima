"""The station's radio bridge for route offers: the other way, when the drone at its launch point has no Wi-Fi.

    python -m hima receive --radio COM5            (or [route_review].radio_url)

On the station PC, connected to the ground radio (a serial port while QGroundControl is closed, or a UDP stream from
a MAVLink router). The drone says "HIMA PLAN <stamp> <n>p <km>km <s>s" (STATUSTEXT, forwarded by the autopilot):
  1. the bridge reads the route and the fence from the autopilot (MAVLink mission download; points only, no images)
     and writes them as an offer (source "radio") for the dashboard, unless the Wi-Fi offer is already there;
  2. when the dashboard writes an answer for a radio offer: "edit" -> the bridge builds the mission from the points
     the same way the drone does (export.mission_lines) and uploads it to the autopilot, then sends MAV_CMD_USER_1 to
     the Jetson (system 1, component 191) with param1 = 2; "approve" -> param1 = 1. param2 = the plan's number;
  3. "HIMA PLAN USED ..." from the drone is written as the result.
The drone checks an edited route itself and puts its own back when the check fails (autonomy/review.py).
The bridge's MAVLink system id is [route_review].station_sysid (254), not the Jetson's 1 (the autopilot's GCS failsafe
listens to system 1 only) and not QGroundControl's 255.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hima.autonomy.review import APPROVE, EDITED, plan_number, targets_from_mission
from hima.config import Config
from hima.ground.plans import PlanStore, now_utc
from hima.telemetry.mavlink import STATION_CMD
from hima.telemetry.mission import download_items, upload_items

PLAN_TEXT = re.compile(r"HIMA PLAN (\d{8}T\d{6}Z) (\d+)p (\d+)km (\d+)s")
USED_TEXT = re.compile(r"HIMA PLAN (USED [A-Z]+|EDITED .*|REFUSED .*)")
JETSON = (1, 191)
FENCE_VERTEX = 5001


def mission_items(cfg: Config, launch_latlon: tuple[float, float], targets: list[tuple[float, float]]
                  ) -> list[dict[str, float]]:
    """The drone's mission for these points (export.mission_lines), as items for the upload."""
    from hima.autonomy.export import mission_lines
    from types import SimpleNamespace
    lines = mission_lines(cfg, SimpleNamespace(launch_latlon=launch_latlon, targets_latlon=targets))
    out = []
    for ln in lines[1:]:
        f = ln.split("\t")
        out.append({"seq": int(f[0]), "current": int(f[1]), "frame": int(f[2]), "cmd": int(f[3]),
                    "p1": float(f[4]), "p2": float(f[5]), "p3": float(f[6]), "p4": float(f[7]),
                    "lat": float(f[8]), "lon": float(f[9]), "alt": float(f[10])})
    return out


class RadioBridge:
    def __init__(self, cfg: Config, plans: str | Path, url: str, *, baud: int = 57600,
                 log: Callable[[str], None] = lambda s: print(s, flush=True), conn: Any = None):
        self.cfg, self.store, self.url, self.baud, self._log = cfg, PlanStore(plans), url, baud, log
        self.conn = conn
        self.autopilot: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.handled: set[str] = set()
        self.pending: list[tuple[str, int, float]] = []         # (plan_id, wait_s, seen) offers to download
        self._last_hb = 0.0

    def log(self, s: str) -> None:
        self._log(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  radio: {s}")

    def start(self) -> None:
        if self.conn is None:
            from pymavlink import mavutil
            self.conn = mavutil.mavlink_connection(self.url, baud=self.baud, source_system=self.cfg.route_review
                                                   .station_sysid, source_component=190, dialect="common",
                                                   autoreconnect=True)
        self.log(f"route offers over the radio on {self.url} (system id {self.cfg.route_review.station_sysid})")
        self._thread = threading.Thread(target=self._run, name="hima-radio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self.conn is not None:
            self.conn.close()

    # ---- loop -----------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        if now - self._last_hb >= 1.0:
            self._last_hb = now
            self.conn.mav.heartbeat_send(6, 8, 0, 0, 0)          # MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID

    def handle(self, m) -> None:
        typ = m.get_type()
        if typ == "HEARTBEAT" and m.autopilot != 8 and m.type not in (6, 18):
            self.autopilot = (m.get_srcSystem(), m.get_srcComponent())
        elif typ == "STATUSTEXT":
            text = m.text if isinstance(m.text, str) else m.text.decode(errors="replace")
            text = text.rstrip("\x00")
            g = PLAN_TEXT.search(text)
            if g:
                pid = "plan-" + g.group(1)
                self.log(f"offer {pid}: {g.group(2)} points, {g.group(3)} km; reading the route from the autopilot")
                self.pending.append((pid, int(g.group(4)), time.monotonic()))
                return
            g = USED_TEXT.search(text)
            if g:
                for pid in self.store.ids()[:1]:
                    if self.store.result(pid) is None:
                        self.store.write_result(pid, {"used": g.group(1).lower(), "reason": text, "via": "radio"})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:                                # noqa: BLE001 - the bridge must keep running
                self.log(f"ERROR {type(e).__name__}: {e}")
                time.sleep(1.0)

    def step(self) -> None:
        self.tick()
        m = self.conn.recv_match(blocking=True, timeout=0.2)
        if m is not None:
            self.handle(m)
        if self.pending and self.autopilot is not None:
            pid, wait_s, seen = self.pending.pop(0)
            self.download_offer(pid, wait_s, seen)
        self.answers()

    def download_offer(self, pid: str, wait_s: int, seen: float) -> None:
        if self.store.offer(pid) is not None:                     # the Wi-Fi offer came first: nothing to read
            self.store.save_offer({"plan_id": pid}, "radio")
            return
        items = download_items(self.conn, self.autopilot, 0, timeout_s=20, on_other=self.handle, tick=self.tick)
        fence = download_items(self.conn, self.autopilot, 1, timeout_s=20, on_other=self.handle, tick=self.tick)
        if not items:
            self.log(f"offer {pid}: the route could not be read from the autopilot")
            return
        home = items[0]
        tg = targets_from_mission(items)
        left = max(0.0, wait_s - (time.monotonic() - seen))
        doc = {"schema_version": "hima.route_offer/1", "plan_id": pid, "created_utc": now_utc(), "wait_s": wait_s,
               "wait_left_s": round(left, 1),
               "launch": {"name": "home", "lat_deg": home["lat"], "lon_deg": home["lon"]},
               "targets": [[round(a, 7), round(b, 7)] for a, b in tg],
               "fence": [[round(it["lat"], 7), round(it["lon"], 7)] for it in fence or [] if int(it["cmd"]) == FENCE_VERTEX],
               "route_km": None, "route_budget_km": None, "patrol_alt_m": self.cfg.vehicle.patrol_alt_m}
        self.store.save_offer(doc, "radio")
        self.log(f"offer {pid}: {len(tg)} points read ({left:.0f} s left for the answer)")

    def answers(self) -> None:
        for pid in self.store.ids()[:5]:
            if pid in self.handled:
                continue
            off, ans = self.store.offer(pid), self.store.answer(pid)
            if off is None or ans is None or off.get("source") != "radio" or self.autopilot is None:
                continue
            self.handled.add(pid)
            if ans["decision"] == "edit":
                la = (off["launch"]["lat_deg"], off["launch"]["lon_deg"])
                items = mission_items(self.cfg, la, [tuple(p) for p in ans["targets"]])
                ok = upload_items(self.conn, self.autopilot, items, 0, timeout_s=30, on_other=self.handle,
                                  tick=self.tick)
                self.log(f"{pid}: edited route ({len(ans['targets'])} points) uploaded: {'accepted' if ok else 'NOT accepted'}")
                if not ok:
                    self.store.write_result(pid, {"used": "drone", "reason": "the autopilot refused the edited route "
                                                  "from the station", "via": "radio"})
                    continue
            p1 = EDITED if ans["decision"] == "edit" else APPROVE
            for _ in range(3):                                    # no ACK comes back through the router: say it 3x
                self.conn.mav.command_long_send(JETSON[0], JETSON[1], STATION_CMD, 0, p1, plan_number(pid), 0, 0, 0, 0, 0)
                time.sleep(0.2)
            self.log(f"{pid}: answer {ans['decision']} sent to the drone")
