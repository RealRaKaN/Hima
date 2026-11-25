"""The link to the Pixhawk (ArduPilot) over MAVLink 2: read the drone's pose and state, send our texts back.

One class for every connection pymavlink's mavutil knows (https://mavlink.io/en/mavgen_python/):
  "/dev/ttyTHS1"            the Jetson UART wired to TELEM2 (baud = SERIAL2_BAUD on the autopilot)
  "tcp:127.0.0.1:5763"      ArduPilot SITL, its SERIAL2 (= TELEM2) port
  "udpin:0.0.0.0:14550"     a telemetry router (mavlink-router / MAVProxy) sending to us
  "udpin:127.0.0.1:0"       any free UDP port on this PC (tests; see .port)

Messages read (common.xml, MAVLink 2):
  HEARTBEAT (0) from the autopilot: armed flag (base_mode & 128), flight mode (custom_mode), system status
  GLOBAL_POSITION_INT (33): lat / lon (degE7), relative_alt (mm above HOME), vx / vy (cm/s, north / east), hdg
      (cdeg, 65535 = unknown)
  GPS_RAW_INT (24): fix_type (3 = 3D), satellites_visible
  SYS_STATUS (1): battery_remaining (%), voltage_battery (mV)
  ATTITUDE (30): kept, not used (fixed nadir camera)
  HOME_POSITION (242): the local origin
  SYSTEM_TIME (2): autopilot clock (time_boot_ms) -> UTC
Sent: HEARTBEAT 1 Hz as an onboard computer (MAV_TYPE_ONBOARD_CONTROLLER), MAV_CMD_SET_MESSAGE_INTERVAL for the
messages above, MAV_CMD_GET_HOME_POSITION, and STATUSTEXT (253) for our alerts. ArduPilot forwards a STATUSTEXT
from the companion to its other links (the ground radio), so the ground station sees our messages.

pose_at(t) gives the pose at autopilot time t (s since boot): the latest position at or before t, moved forward
with its reported velocity. boot_now() estimates the autopilot time now from the last message and the wall clock.
Altitude above ground = relative_alt: right only over flat ground at the height of home (geometry_assumptions).
"""
from __future__ import annotations

import contextlib
import io
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

os.environ.setdefault("MAVLINK20", "1")          # before pymavlink is imported: MAVLink 2 packets
from pymavlink import mavutil                    # noqa: E402
from pymavlink.dialects.v20 import common as mav2  # noqa: E402

from hima.analytics.geometry import DronePose     # noqa: E402
from hima.telemetry.geo import to_local           # noqa: E402

UNKNOWN_HDG = 65535
ARMED_FLAG = 128                                  # MAV_MODE_FLAG_SAFETY_ARMED
STATION_CMD = 31010                               # MAV_CMD_USER_1: the station answers the route offer
# ArduCopter custom_mode numbers (ArduCopter/mode.h)
COPTER_MODES = {0: "STABILIZE", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER", 6: "RTL", 9: "LAND",
                16: "POSHOLD", 17: "BRAKE", 21: "SMART_RTL"}
# ArduPlane custom_mode numbers (ArduPlane/mode.h); a VTOL quadplane sends HEARTBEAT.type FIXED_WING (1) or a VTOL
# type (19-25), and its modes are these
PLANE_MODES = {0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO", 5: "FBWA", 6: "FBWB",
               7: "CRUISE", 8: "AUTOTUNE", 10: "AUTO", 11: "RTL", 12: "LOITER", 13: "TAKEOFF", 14: "AVOID_ADSB",
               15: "GUIDED", 17: "QSTABILIZE", 18: "QHOVER", 19: "QLOITER", 20: "QLAND", 21: "QRTL",
               22: "QAUTOTUNE", 23: "QACRO", 24: "THERMAL", 25: "LOITER_ALT_QLAND", 26: "AUTOLAND"}
PLANE_TYPES = {1} | set(range(19, 26))
STREAMS = {"GLOBAL_POSITION_INT": 33, "ATTITUDE": 30, "GPS_RAW_INT": 24, "SYS_STATUS": 1, "SYSTEM_TIME": 2,
           "HOME_POSITION": 242}


class _Notes(io.TextIOBase):
    """pymavlink prints link trouble ("EOF on TCP socket", reconnect attempts) on every read: keep a count and the
    last lines instead of flooding the log (a lost TCP link printed 20 MB in the first SITL try)."""

    def __init__(self, keep: int = 20):
        self.count, self.last, self.keep = 0, [], keep

    def write(self, s: str) -> int:
        for line in s.splitlines():
            if line.strip():
                self.count += 1
                self.last = (self.last + [line.strip()])[-self.keep:]
        return len(s)


@dataclass(frozen=True)
class Fix:
    t: float                 # autopilot time, s since boot
    lat_deg: float
    lon_deg: float
    rel_alt_m: float
    vn_mps: float
    ve_mps: float
    hdg_deg: float | None


@dataclass
class TelemetryReader:
    """Companion-computer side of the Pixhawk link."""
    url: str = "udpin:127.0.0.1:0"
    baud: int = 921600
    src_system: int = 1                              # same system id as the vehicle: the GCS shows our texts with it
    src_component: int = 191                         # MAV_COMP_ID_ONBOARD_COMPUTER
    clock: Callable[[], float] = time.monotonic
    fixes: list[Fix] = field(default_factory=list)
    attitude: dict | None = None
    home: tuple[float, float] | None = None
    time_offset_s: float | None = None               # UTC seconds minus autopilot seconds (SYSTEM_TIME)
    packets: int = 0
    bad_packets: int = 0
    autopilot: tuple[int, int] | None = None         # (system, component) of the autopilot's HEARTBEAT
    armed: bool | None = None
    mode: str | None = None
    system_status: int | None = None
    last_heartbeat: float | None = None              # wall clock (self.clock) of the autopilot's last HEARTBEAT
    gps: dict | None = None                          # fix_type, satellites
    battery: dict | None = None                      # remaining_pct, voltage_v
    texts_in: list[tuple[int, str]] = field(default_factory=list)   # STATUSTEXT seen from the autopilot
    vehicle_kind: str | None = None                  # "plane" (ArduPlane, VTOL quadplane) or "copter"
    mission_seq: int | None = None                   # MISSION_CURRENT: the mission item being flown
    acks: list[tuple[int, int]] = field(default_factory=list)       # COMMAND_ACK (command, result), newest last
    station_cmds: list[dict] = field(default_factory=list)          # MAV_CMD_USER_1 to us from the station
    max_fixes: int = 20000                           # keep memory flat on a long flight

    def __post_init__(self):
        self.notes = _Notes()
        self.conn = mavutil.mavlink_connection(self.url, baud=self.baud, source_system=self.src_system,
                                               source_component=self.src_component, dialect="common",
                                               robust_parsing=True, autoreconnect=True)
        self._boot_ref: tuple[float, float] | None = None   # (autopilot s, wall s) of the newest timed message
        self._last_hb_sent = -1e9

    @property
    def port(self) -> int | None:
        """UDP port we listen on (udpin), else None."""
        sock = getattr(self.conn, "port", None)
        try:
            return sock.getsockname()[1]
        except (AttributeError, OSError, TypeError):
            return None

    def close(self) -> None:
        self.conn.close()

    # -- reading ------------------------------------------------------------------------------------
    def pump(self, n_packets: int | None = None, timeout_s: float = 0.0) -> int:
        """Read messages: exactly n_packets (waiting up to timeout_s for each), or, with None, whatever is
        already waiting. Returns the number read. The flight calls pump() once per frame."""
        got = 0
        with contextlib.redirect_stdout(self.notes):
            while n_packets is None or got < n_packets:
                if n_packets is not None and timeout_s > 0:
                    m = self.conn.recv_match(blocking=True, timeout=timeout_s)
                else:
                    m = self.conn.recv_msg()
                if m is None:
                    break
                got += 1
                self._handle(m)
        return got

    def _handle(self, m) -> None:
        typ = m.get_type()
        if typ == "BAD_DATA":
            self.bad_packets += 1
            return
        self.packets += 1
        now = self.clock()
        if hasattr(m, "time_boot_ms") and typ in ("GLOBAL_POSITION_INT", "ATTITUDE", "SYSTEM_TIME"):
            self._boot_ref = (m.time_boot_ms / 1000.0, now)
        if typ == "HEARTBEAT":
            if m.type in (mav2.MAV_TYPE_GCS, mav2.MAV_TYPE_ONBOARD_CONTROLLER) or \
                    m.autopilot == mav2.MAV_AUTOPILOT_INVALID:
                return                                   # a ground station or another companion, not the autopilot
            self.autopilot = (m.get_srcSystem(), m.get_srcComponent())
            self.armed = bool(m.base_mode & ARMED_FLAG)
            self.vehicle_kind = "plane" if m.type in PLANE_TYPES else "copter"
            modes = PLANE_MODES if self.vehicle_kind == "plane" else COPTER_MODES
            self.mode = modes.get(m.custom_mode, str(m.custom_mode))
            self.system_status = m.system_status
            self.last_heartbeat = now
        elif typ == "GLOBAL_POSITION_INT":
            self.fixes.append(Fix(m.time_boot_ms / 1000.0, m.lat / 1e7, m.lon / 1e7, m.relative_alt / 1000.0,
                                  m.vx / 100.0, m.vy / 100.0, None if m.hdg == UNKNOWN_HDG else m.hdg / 100.0))
            if len(self.fixes) > self.max_fixes:
                del self.fixes[: len(self.fixes) - self.max_fixes // 2]
        elif typ == "ATTITUDE":
            self.attitude = {"t": m.time_boot_ms / 1000.0, "roll": m.roll, "pitch": m.pitch, "yaw": m.yaw}
        elif typ == "HOME_POSITION":
            self.home = (m.latitude / 1e7, m.longitude / 1e7)
        elif typ == "SYSTEM_TIME" and m.time_unix_usec:
            self.time_offset_s = m.time_unix_usec / 1e6 - m.time_boot_ms / 1000.0
        elif typ == "GPS_RAW_INT":
            self.gps = {"fix_type": m.fix_type, "satellites": m.satellites_visible}
        elif typ == "SYS_STATUS":
            self.battery = {"remaining_pct": m.battery_remaining,
                            "voltage_v": None if m.voltage_battery == 65535 else m.voltage_battery / 1000.0}
        elif typ == "STATUSTEXT":
            self.texts_in.append((m.severity, m.text))
        elif typ == "MISSION_CURRENT":
            self.mission_seq = m.seq
        elif typ == "COMMAND_ACK":
            self.acks = (self.acks + [(m.command, m.result)])[-50:]
        elif typ == "COMMAND_LONG" and m.command == STATION_CMD and m.target_system == self.src_system                 and m.target_component == self.src_component:
            # the station's answer to the route offer over the radio (autonomy/review.py); ArduPilot routes a
            # message to the link where it saw the target (system, component), i.e. to us on TELEM2
            self.station_cmds = (self.station_cmds + [{"src": [m.get_srcSystem(), m.get_srcComponent()],
                                                       "p": [m.param1, m.param2, m.param3, m.param4],
                                                       "at": now}])[-20:]

    def boot_now(self) -> float | None:
        """Autopilot time now (s since boot), from the newest timed message and the wall clock since it."""
        if self._boot_ref is None:
            return None
        boot_s, wall = self._boot_ref
        return boot_s + (self.clock() - wall)

    def heartbeat_age(self) -> float | None:
        return None if self.last_heartbeat is None else self.clock() - self.last_heartbeat

    def pose_at(self, t: float, max_age_s: float = 1.0) -> DronePose | None:
        """Pose at autopilot time t; None without home, without a fix at or before t, or when the latest
        fix is older than max_age_s (telemetry lost: better no pose than a wrong one)."""
        if self.home is None:
            return None
        fix = None
        for f in reversed(self.fixes):                   # newest first; fixes arrive in time order
            if f.t <= t + 1e-9:
                fix = f
                break
        if fix is None or t - fix.t > max_age_s or fix.hdg_deg is None:
            return None
        east, north = to_local(fix.lat_deg, fix.lon_deg, *self.home)
        dt = t - fix.t
        return DronePose(east + fix.ve_mps * dt, north + fix.vn_mps * dt, fix.rel_alt_m, fix.hdg_deg % 360.0)

    # -- sending ------------------------------------------------------------------------------------
    def heartbeat(self, every_s: float = 1.0) -> bool:
        """Send our HEARTBEAT if every_s passed since the last one (call it every frame)."""
        now = self.clock()
        if now - self._last_hb_sent < every_s:
            return False
        self._last_hb_sent = now
        with contextlib.redirect_stdout(self.notes):
            self.conn.mav.heartbeat_send(mav2.MAV_TYPE_ONBOARD_CONTROLLER, mav2.MAV_AUTOPILOT_INVALID, 0, 0,
                                         mav2.MAV_STATE_ACTIVE)
        return True

    def request_streams(self, position_hz: float = 10.0) -> None:
        """Ask the autopilot for the messages we read (MAV_CMD_SET_MESSAGE_INTERVAL, 511) and for HOME."""
        if self.autopilot is None:
            raise ConnectionError("no autopilot HEARTBEAT yet")
        sysid, comp = self.autopilot
        rates = {"GLOBAL_POSITION_INT": position_hz, "ATTITUDE": position_hz, "GPS_RAW_INT": 2.0,
                 "SYS_STATUS": 1.0, "SYSTEM_TIME": 1.0, "HOME_POSITION": 0.5}
        for name, hz in rates.items():
            self.conn.mav.command_long_send(sysid, comp, mav2.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                                            STREAMS[name], int(1e6 / hz), 0, 0, 0, 0, 0)
        self.conn.mav.command_long_send(sysid, comp, mav2.MAV_CMD_GET_HOME_POSITION, 0, 0, 0, 0, 0, 0, 0, 0)

    def wait_heartbeat(self, timeout_s: float) -> bool:
        end = self.clock() + timeout_s
        while self.clock() < end:
            self.heartbeat()
            self.pump(1, timeout_s=0.2)
            if self.autopilot is not None:
                return True
        return False

    def send_statustext(self, text: str, severity: int, msg_id: int = 0, chunk_seq: int = 0) -> int:
        """Send one STATUSTEXT; returns its size on the wire (bytes)."""
        data = text.encode("ascii")
        if len(data) > 50:
            raise ValueError(f"STATUSTEXT text is char[50]; got {len(data)} bytes")
        msg = self.conn.mav.statustext_encode(severity, data, id=msg_id, chunk_seq=chunk_seq)
        buf = msg.pack(self.conn.mav)
        with contextlib.redirect_stdout(self.notes):
            self.conn.write(buf)
        return len(buf)

    def state(self) -> dict[str, Any]:
        """What the flight log keeps about the link."""
        return {"url": self.url, "link_notes": self.notes.count, "link_notes_last": self.notes.last,
                "autopilot": self.autopilot, "vehicle_kind": self.vehicle_kind, "armed": self.armed, "mode": self.mode,
                "gps": self.gps, "battery": self.battery, "home": self.home, "fixes": len(self.fixes),
                "packets": self.packets, "bad_packets": self.bad_packets, "time_offset_s": self.time_offset_s}
