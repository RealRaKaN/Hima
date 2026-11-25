"""The Jetson's hands on ArduPlane (VTOL quadplane): modes, guided points, uploading the plan, checking the safety
parameters. Only commands from autonomy/behaviour.py come here; the LLM never commands the drone.

MAVLink (common.xml):
  MAV_CMD_DO_SET_MODE (176)      param1 = 1 (custom mode), param2 = ArduPlane mode number
  MAV_CMD_DO_REPOSITION (192)    COMMAND_INT, frame GLOBAL_RELATIVE_ALT_INT, param2 = 1 (change to GUIDED),
                                 x / y = lat / lon degE7, z = height above home
  mission protocol               MISSION_COUNT -> MISSION_REQUEST(_INT) -> MISSION_ITEM_INT ... -> MISSION_ACK,
                                 mission type 0 (route) and 1 (fence)

Jetson failure -> home: ArduPilot's own GCS failsafe, with the Jetson as "the GCS": MAV_GCS_SYSID =
the Jetson's system id, FS_GCS_ENABL = 1, FS_LONG_ACTN = 1 (RTL). When the Jetson's HEARTBEAT stops for
FS_LONG_TIMEOUT seconds, ArduPilot returns home by itself, whatever the Jetson asked before. export.safety_params()
lists them; check_params() compares them with the autopilot before arming (this program never writes parameters).
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Callable

from hima.autonomy.behaviour import Command
from hima.autonomy.export import read_waypoints, safety_params  # noqa: F401 - safety_params used by callers
from hima.telemetry.geo import to_latlon
from hima.telemetry.mission import download_items, upload_items

PLANE_MODE_NUM = {"MANUAL": 0, "CIRCLE": 1, "STABILIZE": 2, "FBWA": 5, "FBWB": 6, "CRUISE": 7, "AUTO": 10,
                  "RTL": 11, "LOITER": 12, "TAKEOFF": 13, "GUIDED": 15, "QSTABILIZE": 17, "QHOVER": 18,
                  "QLOITER": 19, "QLAND": 20, "QRTL": 21}
CMD_DO_SET_MODE = 176
CMD_DO_REPOSITION = 192
FRAME_GLOBAL_RELATIVE_ALT_INT = 6


class Executor:
    def __init__(self, reader: Any, home: tuple[float, float], log: Callable[[str], None] = print):
        self.reader = reader
        self.home = home
        self.log = log
        self.sent: list[dict[str, Any]] = []

    def _target(self) -> tuple[int, int]:
        if self.reader.autopilot is None:
            raise ConnectionError("no autopilot HEARTBEAT")
        return self.reader.autopilot

    def apply(self, t: float, cmds: list[Command]) -> list[str]:
        """Send the mode / guided commands; return the texts to tell the ground station."""
        say = []
        for c in cmds:
            if c.kind == "say":
                say.append(c.text)
                continue
            try:
                if c.kind == "mode":
                    self.set_mode(c.mode)
                    self.sent.append({"t": round(t, 1), "mode": c.mode})
                elif c.kind == "goto":
                    lat, lon = to_latlon(c.x, c.y, *self.home)
                    self.goto(lat, lon, c.alt)
                    self.sent.append({"t": round(t, 1), "goto": [round(lat, 7), round(lon, 7), c.alt]})
            except Exception as e:                       # noqa: BLE001 - a failed send must not stop the flight
                self.log(f"  autonomy: could not send {c} ({type(e).__name__}: {e})")
                self.sent.append({"t": round(t, 1), "error": f"{c.kind}: {e}"[:200]})
        return say

    def set_mode(self, mode: str) -> None:
        sysid, comp = self._target()
        self.reader.conn.mav.command_long_send(sysid, comp, CMD_DO_SET_MODE, 0, 1, PLANE_MODE_NUM[mode],
                                               0, 0, 0, 0, 0)

    def goto(self, lat: float, lon: float, alt_m: float) -> None:
        sysid, comp = self._target()
        self.reader.conn.mav.command_int_send(sysid, comp, FRAME_GLOBAL_RELATIVE_ALT_INT, CMD_DO_REPOSITION, 0, 0,
                                              -1, 1, 0, math.nan, int(round(lat * 1e7)), int(round(lon * 1e7)),
                                              float(alt_m))

    # ---- before take-off --------------------------------------------------------------------------
    def upload(self, items: list[dict[str, float]], mission_type: int, timeout_s: float = 30.0) -> bool:
        """Mission protocol (telemetry/mission.py); other messages still reach the reader."""
        r = self.reader
        return upload_items(r.conn, self._target(), items, mission_type, timeout_s=timeout_s, on_other=r._handle,
                            tick=r.heartbeat)

    def download(self, mission_type: int, timeout_s: float = 30.0) -> list[dict[str, float]] | None:
        """The items the autopilot holds now (the route the station may have changed over the radio)."""
        r = self.reader
        return download_items(r.conn, self._target(), mission_type, timeout_s=timeout_s, on_other=r._handle,
                              tick=r.heartbeat)

    def upload_plan(self, plan_dir: str | Path) -> dict[str, Any]:
        d = Path(plan_dir)
        fence = read_waypoints(d / "fence.waypoints")
        mission = read_waypoints(d / "mission.waypoints")
        ok_f = self.upload(fence, 1)
        ok_m = ok_f and self.upload(mission, 0)
        return {"fence": ok_f, "mission": ok_m, "fence_items": len(fence), "mission_items": len(mission)}

    def read_param(self, name: str, timeout_s: float = 3.0) -> float | None:
        r = self.reader
        sysid, comp = self._target()
        r.conn.mav.param_request_read_send(sysid, comp, name.encode(), -1)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            m = r.conn.recv_match(blocking=True, timeout=0.5)
            if m is None:
                continue
            if m.get_type() == "PARAM_VALUE" and m.param_id == name:
                return float(m.param_value)
            r._handle(m)
        return None

    def check_params(self, want: dict[str, float]) -> list[str]:
        """Differences between the autopilot and want, as lines ('' list = all as wanted)."""
        bad = []
        for k, v in want.items():
            got = self.read_param(k)
            if got is None:
                bad.append(f"{k}: not read (wanted {v:g})")
            elif abs(got - float(v)) > 1e-3:
                bad.append(f"{k} = {got:g}, wanted {v:g}")
        return bad
