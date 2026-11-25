"""HIMA link tests: one run that checks every link the drone computer needs, before the Jetson arrives.

    Windows laptop:   double-click launcher\\test_links.bat      (options: launcher\\test_links.bat --help)
    Jetson / Linux:   bash launcher/test_links.sh

The ports are found by waiting for the autopilot's HEARTBEAT on each one (hima_start.py's list of ports):
    companion  the link the drone computer uses: TELEM2 through a USB-TTL adapter (as on the drone), or the
               Pixhawk's own USB port (then the TELEM2 wire itself is not tested), or, if nothing else answers,
               the telemetry radio
    ground     the ground telemetry radio's USB, the link Mission Planner uses. With it on the same computer the
               texts test is automatic: sent on the companion link, read back through the radio. A radio is known
               by the RADIO_STATUS it adds to the stream, else by its speed (57600 baud). --mavlink and --ground name
               the ports instead.

Tests (each prints OK / WARN / FAIL / SKIP; the report goes to runs/link_tests/):
    1  find        the autopilot: port, baud, firmware and version, MAVLink 2
    2  params      MAV_SYSID (SYSID_THISMAV before ArduPilot 4.7), SERIAL2_PROTOCOL / SERIAL2_BAUD /
                   BRD_SER2_RTSCTS (TELEM2), SERIAL1 (radio): read only
    3  quality     packets lost (MAVLink sequence numbers) and damaged, on each link; the radio's RSSI and noise
    4  rates       the messages the flight asks for, at the rates it asks for them
    5  freshness   age of the newest position (the flight skips a frame when it is older than
                   [mavlink].max_pose_age_s) and the round trip to the autopilot (TIMESYNC)
    6  gps         fix, satellites, home, battery (outdoors; s + Enter skips the wait)
    7  rotate      hands: a quarter turn clockwise gives about +90 degrees of heading
    8  lift        hands: lifting about 1 m raises the altitude about 1 m
    9  arm         hands, PROPELLERS OFF: arm then disarm are seen (they start and end the flight recording)
    10 texts       5 numbered STATUSTEXT reach the ground station (automatic through the radio, else asked)
    11 cut         hands: the link is cut for about 10 s and comes back by itself
    12 camera      20 s from the camera: frames per second at the asked size
    13 station     the ground station answers with the same delivery key, if delivery is set up
    14 preflight   the flight program's own checks before take-off (python -m hima preflight) on the same
                   port: HEARTBEAT, streams, GPS / HOME / pose, camera, weights, disk, GPU (--no-preflight)

It never writes a parameter, never arms, never sends a movement command. It reads, asks for message rates as the
flight does (they reset when the Pixhawk restarts) and sends text messages and HEARTBEATs. On the ground link it
speaks as system 254, not Mission Planner's 255, so the Pixhawk's GCS failsafe never counts it as a ground station.
The links are read in one thread, and they keep being read while it waits for a key press, so nothing piles up
in the serial buffers. The thresholds below are mine (provisional), not measured limits.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import queue
import random
import re
import statistics
import string
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil                      # noqa: E402
from pymavlink.dialects.v20 import common as mav2  # noqa: E402

from hima.telemetry.mavlink import TelemetryReader, _Notes  # noqa: E402

# ---------------------------------------------------------------------------------------------- thresholds (mine)
LOSS_WARN_PCT = 1.0            # packets lost on a link: up to 1 % OK, up to 5 % WARN, more FAIL
LOSS_FAIL_PCT = 5.0
RATE_SHARE = 0.8               # a stream is OK at >= 80 % of the asked rate
POSITION_FAIL_SHARE = 0.5      # GLOBAL_POSITION_INT under half the asked rate: FAIL
RTT_WARN_MS = 250.0            # round trip to the autopilot (TIMESYNC)
ROTATE_DEG = (60.0, 120.0)     # a quarter turn clockwise by hand
LIFT_M = (0.5, 1.6)            # lifted about 1 m by hand
LINK_LOST_S = 3.0              # no autopilot HEARTBEAT for this long: the link is cut
TEXTS_N = 5
TEXT_SEVERITY = 6              # MAV_SEVERITY_INFO, as the flight's texts by default
ARM_WAIT_S = 120.0
CUT_WAIT_S = 60.0
BACK_WAIT_S = 90.0
CAMERA_S = 20.0
# pacing (the tests make these shorter)
SETTLE_S = 2.0                 # after asking for new rates
TEXT_GAP_S = 1.0
HEADING_AVG_S = 1.0
ALT_AVG_S = 1.5
PARAM_WAIT_S = 1.5
RADIO_LISTEN_S = 2.0           # after the autopilot's HEARTBEAT on a port, listen this long for RADIO_STATUS

GROUND_SYSID, GROUND_COMPID = 254, 190
GROUND_RATES = {33: 4.0, 30: 4.0, 24: 1.0, 1: 1.0}     # msg id -> Hz, a light ground-station load on the radio
FLIGHT_RATES = {"GLOBAL_POSITION_INT": None, "ATTITUDE": None, "GPS_RAW_INT": 2.0, "SYS_STATUS": 1.0,
                "SYSTEM_TIME": 1.0, "HOME_POSITION": 0.5}   # None = [mavlink].position_hz (TelemetryReader.request_streams)
SERIAL_BAUD_CODES = {1: 1200, 2: 2400, 4: 4800, 9: 9600, 19: 19200, 38: 38400, 57: 57600, 111: 111100,
                     115: 115200, 230: 230400, 256: 256000, 460: 460800, 500: 500000, 921: 921600,
                     1500: 1500000, 2000: 2000000}
VEHICLES = {1: "fixed wing", 2: "quadrotor", 3: "coaxial", 4: "helicopter", 10: "ground rover", 13: "hexarotor",
            14: "octorotor", 15: "tricopter"}
FIRMWARE = {3: "ArduPilot", 12: "PX4"}
RADIO_BAUD = 57600             # a SiK radio's serial speed out of the box
CORE = ("find", "params", "quality", "rates", "freshness")


def baud_code(baud: int) -> int | None:
    return next((code for code, b in SERIAL_BAUD_CODES.items() if b == baud), None)


def is_network(url: str) -> bool:
    return url.startswith(("tcp", "udp"))


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:g}"


def _is_autopilot(m) -> bool:
    return m.autopilot != mav2.MAV_AUTOPILOT_INVALID and m.type not in (mav2.MAV_TYPE_GCS,
                                                                        mav2.MAV_TYPE_ONBOARD_CONTROLLER)


# ---------------------------------------------------------------------------------------------- results
@dataclass
class Result:
    n: int
    name: str
    level: str                 # OK | WARN | FAIL | SKIP
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class Link:
    role: str                  # companion | ground
    url: str
    baud: int
    kind: str                  # telem2 | usb | radio | network
    why: str = ""
    firmware: str = ""


# ---------------------------------------------------------------------------------------------- counting what arrives
class LinkStats:
    """What came in on one link: counts per message type, sequence gaps per sender, and the replies waited for."""

    def __init__(self) -> None:
        self.autopilot: tuple[int, int] | None = None
        self.heartbeat: dict | None = None
        self.texts: list[tuple[float, int, int, str]] = []
        self.timesync: dict[int, float] = {}          # our ts1 -> perf_counter when the answer was read
        self.acks: list[tuple[int, int]] = []         # (command, result)
        self.params: dict[str, float] = {}
        self.version: Any = None
        self.pos_wall: float | None = None            # clock time of the newest GLOBAL_POSITION_INT
        self.window()

    def window(self) -> None:
        """Start a measuring window: counts and sequence gaps from now on."""
        self.counts: dict[str, int] = {}
        self.received: dict[tuple[int, int], int] = {}
        self.lost: dict[tuple[int, int], int] = {}
        self._seq: dict[tuple[int, int], int] = {}
        self.bad = self.v1 = self.v2 = 0
        self.radio: list[dict] = []

    def see(self, m: Any, now: float) -> None:
        typ = m.get_type()
        if typ == "BAD_DATA":
            self.bad += 1
            return
        src = (m.get_srcSystem(), m.get_srcComponent())
        v2 = m.get_msgbuf()[0] == 0xFD
        if typ == "HEARTBEAT" and _is_autopilot(m):
            self.autopilot = src
            self.heartbeat = {"type": m.type, "autopilot": m.autopilot, "base_mode": m.base_mode,
                              "custom_mode": m.custom_mode, "mavlink": 2 if v2 else 1}
        seq = m.get_seq()
        last = self._seq.get(src)
        if last is not None:
            gap = (seq - last - 1) % 256
            if gap < 128:                                 # a jump back is a repeat or a restart, not a loss
                self.lost[src] = self.lost.get(src, 0) + gap
        self._seq[src] = seq
        self.received[src] = self.received.get(src, 0) + 1
        if src == self.autopilot:
            self.counts[typ] = self.counts.get(typ, 0) + 1
            if v2:
                self.v2 += 1
            else:
                self.v1 += 1
            if typ == "GLOBAL_POSITION_INT":
                self.pos_wall = now
        if typ == "RADIO_STATUS":
            self.radio.append({"src": src, "rssi": m.rssi, "remrssi": m.remrssi, "noise": m.noise,
                               "remnoise": m.remnoise, "rxerrors": m.rxerrors, "fixed": m.fixed, "txbuf": m.txbuf})
        elif typ == "STATUSTEXT":
            self.texts.append((now, src[0], src[1], m.text))
        elif typ == "TIMESYNC" and m.tc1 != 0:
            self.timesync.setdefault(m.ts1, time.perf_counter())
        elif typ == "COMMAND_ACK":
            self.acks.append((m.command, m.result))
        elif typ == "PARAM_VALUE":
            name = m.param_id if isinstance(m.param_id, str) else m.param_id.decode(errors="ignore")
            self.params[name.rstrip("\x00")] = m.param_value
        elif typ == "AUTOPILOT_VERSION":
            self.version = m

    def loss(self, src: tuple[int, int] | None) -> tuple[int, int, float]:
        """(received, lost, lost %) for one sender (the autopilot)."""
        if src is None:
            return 0, 0, 0.0
        got, lost = self.received.get(src, 0), self.lost.get(src, 0)
        return got, lost, (100.0 * lost / (got + lost) if got + lost else 0.0)


class CompanionReader(TelemetryReader):
    """The flight's own reader (same parsing, same HEARTBEAT as an onboard computer), counting what it sees."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stats = LinkStats()

    def _handle(self, m) -> None:
        self.stats.see(m, self.clock())
        super()._handle(m)


class GroundLink:
    """The ground radio's port, played like a ground station (MAV_TYPE_GCS, system 254)."""

    def __init__(self, url: str, baud: int, clock: Callable[[], float] = time.monotonic):
        self.url, self.baud, self.clock = url, baud, clock
        self.notes = _Notes()
        self.conn = mavutil.mavlink_connection(url, baud=baud, source_system=GROUND_SYSID,
                                               source_component=GROUND_COMPID, dialect="common",
                                               robust_parsing=True, autoreconnect=True)
        self.stats = LinkStats()
        self._hb = -1e9

    @property
    def port(self) -> int | None:
        try:
            return self.conn.port.getsockname()[1]
        except (AttributeError, OSError, TypeError):
            return None

    def heartbeat(self) -> None:
        now = self.clock()
        if now - self._hb >= 1.0:
            self._hb = now
            with contextlib.redirect_stdout(self.notes):
                self.conn.mav.heartbeat_send(mav2.MAV_TYPE_GCS, mav2.MAV_AUTOPILOT_INVALID, 0, 0, mav2.MAV_STATE_ACTIVE)

    def pump(self) -> None:
        with contextlib.redirect_stdout(self.notes):
            while True:
                m = self.conn.recv_msg()
                if m is None:
                    break
                self.stats.see(m, self.clock())

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------------------------- keyboard
class Console:
    """A line from the keyboard without stopping: the links are read while a person does something."""

    def __init__(self) -> None:
        self.buf = ""
        self.closed = False

    def poll(self) -> str | None:
        if self.closed:
            return "s"
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):                 # arrows and function keys: two characters, ignored
                    msvcrt.getwch()
                    continue
                if ch in ("\r", "\n"):
                    line, self.buf = self.buf, ""
                    print(flush=True)
                    return line.strip()
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch == "\x08":
                    if self.buf:
                        self.buf = self.buf[:-1]
                        print("\b \b", end="", flush=True)
                    continue
                self.buf += ch
                print(ch, end="", flush=True)
            return None
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if line == "":                                 # no keyboard (stdin closed): skip what needs hands
                self.closed = True
                print("  (no keyboard: the tests that need hands are skipped)", flush=True)
                return "s"
            return line.strip()
        return None


class NullConsole:
    """--no-hands: nobody at the keyboard."""

    def poll(self) -> str | None:
        return None


# ---------------------------------------------------------------------------------------------- the bench
class Bench:
    """The open links, read in one loop."""

    def __init__(self, cfg, reader: CompanionReader, comp_link: Link, ground: GroundLink | None = None,
                 ground_link: Link | None = None, *, console: Any = None, say: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 on_prompt: Callable[[str | None], None] = lambda text: None):
        self.cfg, self.reader, self.comp_link = cfg, reader, comp_link
        self.on_prompt = on_prompt                         # the page (link_ui.py): what to do now, or None
        self.ground, self.ground_link = ground, ground_link
        self.console = console if console is not None else Console()
        self.say, self.clock, self.sleep = say, clock, sleep
        self.port_errors: list[str] = []

    @staticmethod
    def send(owner: Any, fn: Callable[[], Any]) -> Any:
        """Send on a link; pymavlink's link messages go to that link's notes, not the screen."""
        with contextlib.redirect_stdout(owner.notes):
            return fn()

    def _pump(self, owner: Any) -> None:
        conn = owner.conn
        serial_port = isinstance(conn, mavutil.mavserial)
        if serial_port and conn.portdead:
            # Closed after a read error. pymavlink reopens it on the next HEARTBEAT it sends (once a second);
            # the old handle must be closed first, or Windows refuses the port and Linux gives the plugged-in
            # device a new name (ttyACM1).
            if getattr(conn.port, "is_open", False):
                with contextlib.suppress(Exception):
                    conn.port.close()
            return
        try:
            owner.pump() if isinstance(owner, GroundLink) else owner.pump(None)
        except OSError as e:          # pymavlink's serial read has no error handling: a pulled USB cable lands here
            self.port_errors.append(f"{getattr(conn, 'device', '?')}: {e}")
            if serial_port:
                with contextlib.suppress(Exception):
                    conn.port.close()
                conn.portdead = True

    def tick(self) -> None:
        self.reader.heartbeat()
        self._pump(self.reader)
        if self.ground is not None:
            self.ground.heartbeat()
            self._pump(self.ground)

    def run_for(self, seconds: float, every: Callable[[], None] | None = None, step_s: float = 0.02) -> None:
        end = self.clock() + seconds
        while self.clock() < end:
            self.tick()
            if every is not None:
                every()
            self.sleep(step_s)

    def wait(self, done: Callable[[], bool], timeout_s: float, step_s: float = 0.02) -> bool:
        end = self.clock() + timeout_s
        while self.clock() < end:
            self.tick()
            if done():
                return True
            self.sleep(step_s)
        self.tick()
        return done()

    def wait_key(self, prompt: str | None, timeout_s: float | None = None,
                 until: Callable[[], bool] | None = None) -> str | None:
        """Show prompt and keep reading the links until Enter (returns the line, "" for Enter), until() is true
        (None) or the timeout (None). What the Pixhawk says meanwhile (e.g. "PreArm: ...") is shown."""
        if prompt:
            self.say(prompt)
        self.on_prompt(prompt or "Waiting (s skips the wait).")
        try:
            return self._wait_key(timeout_s, until)
        finally:
            self.on_prompt(None)

    def _wait_key(self, timeout_s: float | None, until: Callable[[], bool] | None) -> str | None:
        shown = len(self.reader.texts_in)
        end = None if timeout_s is None else self.clock() + timeout_s
        while end is None or self.clock() < end:
            self.tick()
            for _sev, text in self.reader.texts_in[shown:]:
                self.say(f"      Pixhawk says: {text}")
            shown = len(self.reader.texts_in)
            line = self.console.poll()
            if line is not None:
                return line
            if until is not None and until():
                return None
            self.sleep(0.02)
        return None

    # -- asking the autopilot ----------------------------------------------------------------------------
    def target(self) -> tuple[int, int]:
        return self.reader.autopilot or (1, 1)

    def read_param(self, name: str, tries: int = 3) -> float | None:
        st = self.reader.stats
        for _ in range(tries):
            sysid, comp = self.target()
            self.send(self.reader, lambda: self.reader.conn.mav.param_request_read_send(
                sysid, comp, name.encode("ascii"), -1))
            if self.wait(lambda: name in st.params, PARAM_WAIT_S):
                return st.params[name]
        return None

    def autopilot_version(self, wait_s: float = 1.5) -> Any:
        st = self.reader.stats
        for _ in range(2):
            sysid, comp = self.target()
            self.send(self.reader, lambda: self.reader.conn.mav.command_long_send(
                sysid, comp, mav2.MAV_CMD_REQUEST_MESSAGE, 0, mav2.MAVLINK_MSG_ID_AUTOPILOT_VERSION, 0, 0, 0, 0, 0, 0))
            if self.wait(lambda: st.version is not None, wait_s):
                return st.version
        return None

    def timesync(self, owner: Any) -> int:
        """Send a TIMESYNC request on that link; the answer echoes ts1 (owner.stats.timesync[ts1])."""
        ts1 = time.perf_counter_ns()
        owner.stats.timesync.pop(ts1, None)
        self.send(owner, lambda: owner.conn.mav.timesync_send(0, ts1))
        return ts1


# ---------------------------------------------------------------------------------------------- finding the links
def probe(url: str, baud: int, wait_s: float = 3.0) -> dict:
    """Open one port and wait for the autopilot's HEARTBEAT. Only then send ours (as a ground station): a SiK
    telemetry radio adds RADIO_STATUS to the stream once it hears one, and that tells the radio apart."""
    out: dict = {"ok": False, "radio": False, "detail": "", "firmware": "", "system": None}
    notes = _Notes()
    try:
        with contextlib.redirect_stdout(notes):             # pymavlink prints its TCP connect retries
            conn = mavutil.mavlink_connection(url, baud=baud, source_system=GROUND_SYSID,
                                              source_component=GROUND_COMPID, autoreconnect=False)
    except Exception as e:                                  # noqa: BLE001 - busy, no permission, gone, refused
        text = str(e)
        hint = ""
        if any(w in text.lower() for w in ("denied", "permission", "busy")):
            hint = (" (in use by another program, e.g. Mission Planner: close it)" if os.name == "nt" else
                    " (no permission: sudo usermod -aG dialout $USER, then log out and in)")
        out["detail"] = f"cannot open: {text}{hint}"
        return out
    try:
        end = time.monotonic() + wait_s
        hb_next: float | None = None
        while time.monotonic() < end:
            if hb_next is not None and time.monotonic() >= hb_next:
                hb_next = time.monotonic() + 0.5
                with contextlib.redirect_stdout(notes):
                    conn.mav.heartbeat_send(mav2.MAV_TYPE_GCS, mav2.MAV_AUTOPILOT_INVALID, 0, 0, mav2.MAV_STATE_ACTIVE)
            with contextlib.redirect_stdout(notes):
                m = conn.recv_match(blocking=True, timeout=0.2)
            if m is None:
                continue
            typ = m.get_type()
            if typ == "RADIO_STATUS":
                out["radio"] = True
            elif typ == "HEARTBEAT" and _is_autopilot(m) and not out["ok"]:
                fw = FIRMWARE.get(m.autopilot, f"autopilot {m.autopilot}")
                out.update(ok=True, firmware=fw, system=m.get_srcSystem(), detail=f"{fw}, system {m.get_srcSystem()}")
                end = time.monotonic() + RADIO_LISTEN_S
                hb_next = time.monotonic()
            if out["ok"] and out["radio"]:
                break
        if not out["ok"]:
            out["detail"] = "no autopilot HEARTBEAT"
        return out
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def find_links(candidates: list[tuple[str, list[int], str]], *, mavlink: str | None = None, baud: int | None = None,
               ground: str | None = None, ground_baud: int | None = None, cfg_baud: int = 921600,
               say: Callable[[str], None] = print, probe_fn: Callable[..., dict] = probe
               ) -> tuple[Link | None, Link | None, list[str]]:
    """Which port is the companion link and which the ground radio. candidates: hima_start.port_candidates()
    as (port, bauds, why). --mavlink / --ground name them; the other ports are tried one by one."""
    notes: list[str] = []
    why_of = {p: w for p, _b, w in candidates}
    telem_bauds = list(dict.fromkeys([cfg_baud, 921600, 115200, RADIO_BAUD]))

    def try_port(url: str, bauds: list[int], why: str) -> tuple[int | None, dict | None]:
        for bd in bauds:
            r = probe_fn(url, bd or 115200, wait_s=8.0 if is_network(url) else 3.0)
            extra = "; a telemetry radio (RADIO_STATUS)" if r.get("radio") else ""
            say(f"  {url:<22} {('@ ' + str(bd)) if bd else '':<10} {why}: {r['detail']}{extra}")
            if r["ok"]:
                return bd, r
            if r["detail"].startswith("cannot open"):
                break                                       # the port itself failed: other bauds will too
        return None, None

    def make(role: str, url: str, bd: int | None, why: str, r: dict, kind: str) -> Link:
        return Link(role, url, bd or (115200 if kind == "usb" else cfg_baud), kind, why, r.get("firmware", ""))

    comp = gnd = None
    if ground:                    # first: ArduPilot SITL opens TELEM2 (5763) only once something connects to 5760
        bauds = [ground_baud] if ground_baud else ([0] if is_network(ground) else [RADIO_BAUD, 115200])
        bd, r = try_port(ground, bauds, "--ground")
        if r:
            gnd = make("ground", ground, bd, "--ground", r, "network" if is_network(ground) else "radio")
    if mavlink:
        why = why_of.get(mavlink, "")
        bauds = [baud] if baud else ([0] if is_network(mavlink) else ([115200] if "autopilot USB" in why else telem_bauds))
        bd, r = try_port(mavlink, bauds, "--mavlink")
        if r:
            kind = ("network" if is_network(mavlink) else "radio" if r["radio"] else
                    "usb" if "autopilot USB" in why else "telem2")
            comp = make("companion", mavlink, bd, "--mavlink" + (f" ({why})" if why else ""), r, kind)

    found: list[tuple[str, int, str, dict]] = []
    if not (mavlink and ground):
        for port, bauds, why in candidates:
            if port in (mavlink, ground):
                continue
            bd, r = try_port(port, bauds, why)
            if r:
                found.append((port, bd, why, r))
    radios = [f for f in found if f[3].get("radio")]
    usbs = [f for f in found if f not in radios and "autopilot USB" in f[2]]
    wires = [f for f in found if f not in radios and f not in usbs]
    if not ground and not radios and cfg_baud != RADIO_BAUD:
        slow = [f for f in wires if f[1] == RADIO_BAUD]
        if slow and (len(wires) > 1 or usbs or mavlink):  # another port can be the companion link
            radios.append(slow[0])
            wires.remove(slow[0])
            notes.append(f"{slow[0][0]} is taken as the telemetry radio: it answered at {RADIO_BAUD} baud, the radio's "
                         f"speed (no RADIO_STATUS heard). If it is the TELEM2 adapter: --mavlink {slow[0][0]}")
    if not ground and radios:
        gnd = make("ground", *radios[0][:3], radios[0][3], "radio")
    if not mavlink:
        if wires:
            comp = make("companion", *wires[0][:3], wires[0][3], "telem2")
        elif usbs:
            comp = make("companion", *usbs[0][:3], usbs[0][3], "usb")
        elif gnd is not None and gnd.kind == "radio" and not ground:
            comp, gnd = Link("companion", gnd.url, gnd.baud, "radio", gnd.why, gnd.firmware), None
    if comp is not None and comp.kind == "usb":
        notes.append("the link under test is the Pixhawk's USB port: the TELEM2 wire is not tested (on the drone "
                     "the system uses TELEM2)")
    elif comp is not None and comp.kind == "radio":
        notes.append("the link under test is the telemetry radio: it tests the Pixhawk and the radio, not the TELEM2 "
                     "wire the drone computer will use")
    return comp, gnd, notes


# ---------------------------------------------------------------------------------------------- the tests
def _circ_mean(degs: list[float]) -> float | None:
    if not degs:
        return None
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360.0


def _heading_now(b: Bench) -> tuple[float | None, str]:
    """Mean heading over HEADING_AVG_S: the position message's heading (what the flight uses), else ATTITUDE yaw."""
    from_pos: list[float] = []
    from_att: list[float] = []
    end = b.clock() + HEADING_AVG_S
    while b.clock() < end:
        b.tick()
        f = b.reader.fixes[-1] if b.reader.fixes else None
        if f is not None and f.hdg_deg is not None:
            from_pos.append(f.hdg_deg)
        if b.reader.attitude:
            from_att.append(math.degrees(b.reader.attitude["yaw"]) % 360.0)
        b.sleep(0.05)
    if from_pos:
        return _circ_mean(from_pos), "position heading"
    return _circ_mean(from_att), "ATTITUDE yaw"


def _alt_now(b: Bench) -> float | None:
    vals: list[float] = []
    end = b.clock() + ALT_AVG_S
    while b.clock() < end:
        b.tick()
        if b.reader.fixes:
            vals.append(b.reader.fixes[-1].rel_alt_m)
        b.sleep(0.05)
    return statistics.median(vals) if vals else None


def _skip(line: str | None) -> bool:
    return line is not None and line.lower().startswith("s")


def test_params(b: Bench) -> Result:
    kind = b.comp_link.kind
    # the vehicle's system id: MAV_SYSID from ArduPilot 4.7, SYSID_THISMAV before
    sysid_name, sysid = "MAV_SYSID", b.read_param("MAV_SYSID", tries=2)
    if sysid is None:
        sysid_name, sysid = "SYSID_THISMAV", b.read_param("SYSID_THISMAV", tries=2)
        sysid_name = sysid_name if sysid is not None else "MAV_SYSID"
    names = ["SERIAL2_PROTOCOL", "SERIAL2_BAUD", "BRD_SER2_RTSCTS"]
    if b.ground_link is not None or kind == "radio":
        names += ["SERIAL1_PROTOCOL", "SERIAL1_BAUD"]
    vals = {sysid_name: sysid, **{n: b.read_param(n) for n in names}}
    want = int(b.cfg.mavlink.baud)
    want_code = baud_code(want)
    fails: list[str] = []
    warns: list[str] = []
    info: list[str] = []

    def telem2(msg: str, fail: bool = False) -> None:
        """A TELEM2 setting that is not what the drone computer needs."""
        if kind == "telem2":
            (fails if fail else warns).append(msg)
        elif kind in ("usb", "radio"):
            warns.append(msg + " (before the drone computer moves to TELEM2)")
        else:
            info.append(msg)                              # a simulator: its serial settings do not matter

    if all(v is None for v in vals.values()):
        warns.append("no parameter answered: the Pixhawk does not answer parameter reads on this link")
    elif sysid is None:
        warns.append("the system id was not read (neither MAV_SYSID nor SYSID_THISMAV answered)")
    elif int(sysid) != b.reader.src_system:
        warns.append(f"{sysid_name} = {int(sysid)}, but the system sends as system {b.reader.src_system}: Mission "
                     f"Planner shows its texts as another vehicle (set {sysid_name} = {b.reader.src_system})")
    proto = vals["SERIAL2_PROTOCOL"]
    if proto is None:
        telem2("SERIAL2_PROTOCOL not read")
    elif int(proto) == 1:
        telem2("SERIAL2_PROTOCOL = 1 (MAVLink 1): set 2")
    elif int(proto) != 2:
        telem2(f"SERIAL2_PROTOCOL = {int(proto)} (not MAVLink): set 2", fail=True)
    sb = vals["SERIAL2_BAUD"]
    if sb is not None:
        sb_baud = SERIAL_BAUD_CODES.get(int(sb))
        if want_code is not None and int(sb) != want_code:
            telem2(f"SERIAL2_BAUD = {int(sb)} ({sb_baud or '?'} baud) but hima.toml [mavlink].baud = {want}: "
                   f"set SERIAL2_BAUD = {want_code}")
        if kind == "telem2" and sb_baud is not None and sb_baud != b.comp_link.baud:
            warns.append(f"this port answered at {b.comp_link.baud} baud but SERIAL2_BAUD is {sb_baud}: is the adapter "
                         "wired to TELEM1 or another port instead of TELEM2?")
    rts = vals["BRD_SER2_RTSCTS"]
    if rts is None:
        info.append("BRD_SER2_RTSCTS: not on this board")
    elif int(rts) != 0:
        telem2(f"BRD_SER2_RTSCTS = {int(rts)}: set 0 (three wires, no flow control)")
    if vals.get("SERIAL1_BAUD") is not None:
        info.append(f"TELEM1 (radio): SERIAL1_PROTOCOL = {_fmt(vals['SERIAL1_PROTOCOL'])}, SERIAL1_BAUD = "
                    f"{_fmt(vals['SERIAL1_BAUD'])}")
    shown = ", ".join(f"{k} = {_fmt(v)}" for k, v in vals.items() if not k.startswith("SERIAL1"))
    level = "FAIL" if fails else ("WARN" if warns else "OK")
    detail = ("; ".join(fails + warns) + f" [{shown}]") if fails or warns else shown
    if info:
        detail += "; " + "; ".join(info)
    return Result(2, "params", level, detail, {"params": vals, "link": kind})


def test_window(b: Bench, window_s: float) -> list[Result]:
    """Tests 3, 4 and 5 in one measuring window: the flight's own rates, loss, freshness, round trip."""
    cfg = b.cfg
    st = b.reader.stats
    st.acks.clear()
    b.send(b.reader, lambda: b.reader.request_streams(cfg.mavlink.position_hz))
    if b.ground is not None:
        g = b.ground
        gsys, gcomp = g.stats.autopilot or b.target()
        for msg_id, hz in GROUND_RATES.items():
            b.send(g, lambda m=msg_id, h=hz: g.conn.mav.command_long_send(
                gsys, gcomp, mav2.MAV_CMD_SET_MESSAGE_INTERVAL, 0, m, int(1e6 / h), 0, 0, 0, 0, 0))
    b.run_for(SETTLE_S)                                   # the new rates settle
    st.window()
    if b.ground is not None:
        b.ground.stats.window()
    ages: list[float] = []
    sent_c: dict[int, float] = {}
    sent_g: dict[int, float] = {}
    last_sync = [-1e9]

    def every() -> None:
        now = b.clock()
        if st.pos_wall is not None:
            ages.append(now - st.pos_wall)
        if now - last_sync[0] >= 0.5:
            last_sync[0] = now
            sent_c[b.timesync(b.reader)] = time.perf_counter()
            if b.ground is not None:
                sent_g[b.timesync(b.ground)] = time.perf_counter()

    t0 = b.clock()
    b.run_for(window_s, every, step_s=0.01)
    dur = max(1e-6, b.clock() - t0)
    b.run_for(0.6)                                        # late TIMESYNC answers
    out: list[Result] = []

    def quality(stats: LinkStats, link: Link, name: str) -> Result:
        got, lost, pct = stats.loss(stats.autopilot)
        data: dict = {"packets": got, "lost": lost, "lost_pct": round(pct, 3), "damaged": stats.bad,
                      "per_s": round(got / dur, 1), "mavlink1": stats.v1, "seconds": round(dur, 1)}
        if got == 0:
            return Result(3, name, "FAIL", f"nothing from the autopilot in {dur:.0f} s", data)
        level = "OK" if pct <= LOSS_WARN_PCT else ("WARN" if pct <= LOSS_FAIL_PCT else "FAIL")
        detail = f"{got} packets in {dur:.0f} s ({got / dur:.0f}/s), lost {lost} ({pct:.2f} %), damaged {stats.bad}"
        if stats.v1:
            level = "WARN" if level == "OK" else level
            detail += f"; {stats.v1} MAVLink 1 packets (the port should speak MAVLink 2)"
        if level != "OK" and link.kind == "telem2" and link.baud >= 460800:
            detail += "; with this loss try SERIAL2_BAUD = 115 and [mavlink].baud = 115200 (CH340 adapters are unstable fast)"
        if stats.radio:
            rr = stats.radio

            def med(k: str) -> float:
                return statistics.median(r[k] for r in rr)
            data["radio"] = {k: med(k) for k in ("rssi", "remrssi", "noise", "remnoise")}
            data["radio"]["rxerrors"] = (rr[-1]["rxerrors"] - rr[0]["rxerrors"]) % 65536
            data["radio"]["fixed"] = (rr[-1]["fixed"] - rr[0]["fixed"]) % 65536
            detail += (f"; radio RSSI {med('rssi'):.0f} / remote {med('remrssi'):.0f}, noise {med('noise'):.0f} / "
                       f"{med('remnoise'):.0f} (SiK units), rx errors +{data['radio']['rxerrors']}, corrected "
                       f"+{data['radio']['fixed']}: compare near and far")
        return Result(3, name, level, detail, data)

    out.append(quality(st, b.comp_link, "quality"))
    if b.ground is not None:
        out.append(quality(b.ground.stats, b.ground_link, "quality-radio"))

    # 4. rates
    rows, data, level = [], {}, "OK"
    for name, hz in FLIGHT_RATES.items():
        asked = cfg.mavlink.position_hz if hz is None else hz
        got_hz = st.counts.get(name, 0) / dur
        data[name] = {"asked_hz": asked, "got_hz": round(got_hz, 2)}
        if name == "HOME_POSITION":
            rows.append(f"HOME_POSITION {got_hz:.1f}/{asked:g}" if got_hz else "HOME_POSITION none (no home yet: "
                        "it comes with the GPS fix)")
            continue
        low = got_hz < RATE_SHARE * asked
        rows.append(f"{name} {got_hz:.1f}/{asked:g}" + (" LOW" if low else ""))
        if (name in ("GLOBAL_POSITION_INT", "SYSTEM_TIME") and got_hz == 0) or \
                (name == "GLOBAL_POSITION_INT" and got_hz < POSITION_FAIL_SHARE * asked):
            level = "FAIL"
        elif low and level == "OK":
            level = "WARN"
    acks = [r for c, r in st.acks if c == mav2.MAV_CMD_SET_MESSAGE_INTERVAL]
    refused = [r for r in acks if r != 0]
    data["acks"] = {"accepted": len(acks) - len(refused), "refused": refused}
    detail = "per second got/asked: " + ", ".join(rows)
    if refused:
        detail += f"; {len(refused)} rate requests refused (results {refused})"
        level = "WARN" if level == "OK" else level
    elif not acks:
        detail += "; no COMMAND_ACK for the rate requests"
    out.append(Result(4, "rates", level, detail, data))

    # 5. freshness and round trip
    max_age = cfg.mavlink.max_pose_age_s
    data = {}
    if ages:
        srt = sorted(ages)
        p95 = srt[min(len(srt) - 1, int(0.95 * len(srt)))]
        data.update(age_median_s=round(statistics.median(srt), 3), age_p95_s=round(p95, 3), age_max_s=round(srt[-1], 3))
        level = "OK" if srt[-1] <= max_age else ("WARN" if p95 <= max_age else "FAIL")
        detail = (f"newest position {statistics.median(srt) * 1000:.0f} ms old (median), {p95 * 1000:.0f} ms (95 %), "
                  f"{srt[-1] * 1000:.0f} ms (worst); the flight skips frames over {max_age * 1000:.0f} ms")
    else:
        level, detail = "FAIL", "no GLOBAL_POSITION_INT: the flight would have no position"

    def rtts(sent: dict[int, float], stats: LinkStats) -> list[float]:
        return [(stats.timesync[k] - t) * 1000 for k, t in sent.items() if k in stats.timesync]

    rc = rtts(sent_c, st)
    if rc:
        data["rtt_ms"] = {"median": round(statistics.median(rc), 1), "max": round(max(rc), 1), "answered": len(rc),
                          "asked": len(sent_c)}
        detail += f"; round trip {statistics.median(rc):.0f} ms (median, {len(rc)}/{len(sent_c)} answered)"
        if statistics.median(rc) > RTT_WARN_MS and level == "OK":
            level = "WARN"
    else:
        detail += "; no TIMESYNC answer (round trip not measured)"
    if b.ground is not None:
        rg = rtts(sent_g, b.ground.stats)
        if rg:
            data["rtt_radio_ms"] = {"median": round(statistics.median(rg), 1), "max": round(max(rg), 1),
                                    "answered": len(rg), "asked": len(sent_g)}
            detail += f"; through the ground link {statistics.median(rg):.0f} ms ({len(rg)}/{len(sent_g)})"
    out.append(Result(5, "freshness", level, detail, data))
    return out


def test_gps(b: Bench, wait_s: float) -> Result:
    f = b.cfg.flight

    def state() -> tuple[bool, str, dict]:
        g, home, bat = b.reader.gps, b.reader.home, b.reader.battery
        parts, ok = [], True
        if g is None:
            ok = False
            parts.append("no GPS_RAW_INT")
        else:
            ok = g["fix_type"] >= f.min_fix_type and g["satellites"] >= f.min_satellites
            parts.append(f"fix type {g['fix_type']} (needs >= {f.min_fix_type}), {g['satellites']} satellites "
                         f"(needs >= {f.min_satellites})")
        ok = ok and home is not None
        parts.append(f"home {home[0]:.5f}, {home[1]:.5f}" if home else "no home yet")
        parts.append(f"battery {bat['remaining_pct']} %, {bat['voltage_v']} V" if bat else "no battery reading")
        return ok, ", ".join(parts), {"gps": g, "home": home, "battery": bat}

    ok, detail, data = state()
    if not ok and wait_s > 0:
        b.say(f"      waiting up to {wait_s:.0f} s for the GPS fix and home (outdoors; s + Enter skips the wait)")
        last_ask = [b.clock()]

        def done() -> bool:
            if b.clock() - last_ask[0] > 5.0:             # home comes once the EKF has its origin: ask again
                last_ask[0] = b.clock()
                b.send(b.reader, lambda: b.reader.request_streams(b.cfg.mavlink.position_hz))
            return state()[0]
        line = b.wait_key(None, timeout_s=wait_s, until=done)
        ok, detail, data = state()
        if _skip(line) and not ok:
            return Result(6, "gps", "SKIP", "skipped: " + detail, data)
    if not ok:
        return Result(6, "gps", "FAIL", detail + " (indoors the GPS cannot get a fix)", data)
    return Result(6, "gps", "WARN" if b.reader.battery is None else "OK", detail, data)


def test_rotate(b: Bench) -> Result:
    line = b.wait_key("      Put the drone down, facing any way, still. Press Enter (s + Enter skips).")
    if _skip(line):
        return Result(7, "rotate", "SKIP", "skipped")
    h1, _src = _heading_now(b)
    if h1 is None:
        return Result(7, "rotate", "FAIL", "no heading from the autopilot (no position heading, no ATTITUDE)")
    line = b.wait_key(f"      Heading now {h1:.0f} deg. Turn it a quarter turn CLOCKWISE (to the right, seen from "
                      "above), put it down, press Enter.")
    if _skip(line):
        return Result(7, "rotate", "SKIP", "skipped")
    h2, src = _heading_now(b)
    d = ((h2 - h1 + 180.0) % 360.0) - 180.0
    data = {"before_deg": round(h1, 1), "after_deg": round(h2, 1), "change_deg": round(d, 1), "source": src}
    if ROTATE_DEG[0] <= d <= ROTATE_DEG[1]:
        level, detail = "OK", f"heading {h1:.0f} -> {h2:.0f} deg: {d:+.0f} deg for a quarter turn clockwise ({src})"
    elif -ROTATE_DEG[1] <= d <= -ROTATE_DEG[0]:
        level, detail = "FAIL", (f"heading changed {d:+.0f} deg: the other way. If it was turned clockwise, the compass "
                                 "orientation is wrong (COMPASS_ORIENT; calibrate in Mission Planner)")
    else:
        level, detail = "FAIL", (f"heading changed {d:+.0f} deg for a quarter turn: calibrate the compass (Mission "
                                 "Planner: Compass) and keep metal and magnets away while testing")
    if src != "position heading":
        level = "WARN" if level == "OK" else level
        detail += "; the position message has no heading, so the flight would have no pose"
    return Result(7, "rotate", level, detail, data)


def test_lift(b: Bench) -> Result:
    line = b.wait_key("      Hold the drone still at ground level. Press Enter (s + Enter skips).")
    if _skip(line):
        return Result(8, "lift", "SKIP", "skipped")
    a1 = _alt_now(b)
    if a1 is None:
        return Result(8, "lift", "FAIL", "no GLOBAL_POSITION_INT: no altitude")
    line = b.wait_key("      Lift it about 1 m (from the floor to waist height) and hold it still. Press Enter.")
    if _skip(line):
        return Result(8, "lift", "SKIP", "skipped")
    a2 = _alt_now(b)
    d = a2 - a1
    data = {"before_m": round(a1, 2), "after_m": round(a2, 2), "change_m": round(d, 2)}
    if LIFT_M[0] <= d <= LIFT_M[1]:
        return Result(8, "lift", "OK", f"altitude above home {a1:.2f} -> {a2:.2f} m: {d:+.2f} m for about 1 m", data)
    hint = (" (no home yet: the altitude above home may not move before the GPS fix)" if b.reader.home is None
            else " (barometer: keep it out of wind and sun, hold it still longer)")
    return Result(8, "lift", "FAIL" if d < 0.2 else "WARN", f"altitude changed {d:+.2f} m for about 1 m{hint}", data)


def test_arm(b: Bench) -> Result:
    b.say("      !! TAKE THE PROPELLERS OFF FIRST !! Armed, the motors may spin.")
    t0 = b.clock()
    line = b.wait_key(f"      Arm the drone (Mission Planner or the RC). Waiting up to {ARM_WAIT_S:.0f} s "
                      "(s + Enter skips).", timeout_s=ARM_WAIT_S, until=lambda: bool(b.reader.armed))
    if not b.reader.armed:
        why = ("skipped" if _skip(line) else
               f"not armed within {ARM_WAIT_S:.0f} s (pre-arm checks? read what the Pixhawk said above)")
        return Result(9, "arm", "SKIP", why)
    t_arm = b.clock() - t0
    b.wait_key(f"      ARMED seen after {t_arm:.0f} s. Now disarm.", timeout_s=ARM_WAIT_S,
               until=lambda: b.reader.armed is False)
    if b.reader.armed:
        return Result(9, "arm", "WARN", f"armed seen, disarm not seen within {ARM_WAIT_S:.0f} s: DISARM NOW",
                      {"armed_after_s": round(t_arm, 1)})
    return Result(9, "arm", "OK", "ARMED then DISARMED seen (the flight records between the two)",
                  {"armed_after_s": round(t_arm, 1)})


def test_texts(b: Bench, hands: bool) -> Result:
    code = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    texts = [f"HIMA TEST {i}/{TEXTS_N} {code}" for i in range(1, TEXTS_N + 1)]
    seen_before = len(b.ground.stats.texts) if b.ground is not None else 0
    sent: dict[str, float] = {}
    for t in texts:
        b.reader.send_statustext(t, TEXT_SEVERITY)
        sent[t] = b.clock()
        b.run_for(TEXT_GAP_S)
    if b.ground is not None:
        def arrived() -> dict[str, float]:
            got: dict[str, float] = {}
            for when, _sys, comp, text in b.ground.stats.texts[seen_before:]:
                if text in sent and text not in got and comp == b.reader.src_component:
                    got[text] = when
            return got
        b.wait(lambda: len(arrived()) == len(texts), 5.0)
        got = arrived()
        lat = [(got[t] - sent[t]) * 1000 for t in texts if t in got]
        data = {"sent": len(texts), "arrived": len(got), "code": code,
                "latency_ms": {"median": round(statistics.median(lat)), "max": round(max(lat))} if lat else None}
        if len(got) == len(texts):
            in_order = sorted(got, key=got.get) == texts
            return Result(10, "texts", "OK" if in_order else "WARN",
                          f"{len(got)}/{len(texts)} arrived through the ground link ({b.ground_link.url}), "
                          f"{statistics.median(lat):.0f} ms after sending (median), worst {max(lat):.0f} ms"
                          + ("" if in_order else "; out of order"), data)
        return Result(10, "texts", "WARN" if got else "FAIL",
                      f"{len(got)}/{len(texts)} arrived through the ground link: the Pixhawk does not pass the "
                      "companion's texts to the radio, or the radio drops them", data)
    if not hands:
        return Result(10, "texts", "SKIP", f"sent {len(texts)} (code {code}); no ground link here, and --no-hands",
                      {"sent": len(texts), "code": code})
    line = b.wait_key(f"      Sent \"HIMA TEST 1/{TEXTS_N} {code}\" to \"{TEXTS_N}/{TEXTS_N} {code}\". In Mission Planner "
                      "(Messages tab), did they arrive? y = all, p = some, n = none, s = cannot check:")
    ans = (line or "s").lower()[:1]
    data = {"sent": len(texts), "code": code, "answer": ans}
    answers = {
        "y": Result(10, "texts", "OK", f"all {len(texts)} seen in Mission Planner (code {code})", data),
        "p": Result(10, "texts", "WARN", f"only some seen in Mission Planner (code {code}): radio range or loss", data),
        "n": Result(10, "texts", "FAIL", f"none seen in Mission Planner (code {code}): is Mission Planner on the radio, "
                    "and the radios linked (solid green light)?", data)}
    return answers.get(ans, Result(10, "texts", "SKIP", f"not checked (code {code})", data))


def test_cut(b: Bench) -> Result:
    how = {"telem2": "pull the TX wire out of TELEM2",
           "usb": "unplug the Pixhawk's USB cable (without a battery the Pixhawk restarts: that is fine)",
           "radio": "unplug the ground radio's USB cable",
           "network": "stop the link"}.get(b.comp_link.kind, "cut the link")
    t0 = b.clock()
    line = b.wait_key(f"      Cut the link for about 10 s: {how}. Waiting for the cut, up to {CUT_WAIT_S:.0f} s "
                      "(s + Enter skips).", timeout_s=CUT_WAIT_S,
                      until=lambda: (b.reader.heartbeat_age() or 0) > LINK_LOST_S)
    if (b.reader.heartbeat_age() or 0) <= LINK_LOST_S:
        return Result(11, "cut", "SKIP", "skipped" if _skip(line) else f"no cut seen within {CUT_WAIT_S:.0f} s")
    t_lost = b.clock()
    notes_before = b.reader.notes.count
    b.say(f"      Link lost (no HEARTBEAT for {LINK_LOST_S:.0f} s). Put it back now.")
    back_age = min(1.5, LINK_LOST_S)
    back = b.wait(lambda: (b.reader.heartbeat_age() or 1e9) < back_age, BACK_WAIT_S)
    data = {"lost_seen_after_s": round(t_lost - t0, 1), "port_errors": b.port_errors[-3:],
            "link_notes": b.reader.notes.last[-3:]}
    if not back:
        hint = (" (a USB port can come back under another name: check the port list, then run again)"
                if b.comp_link.kind in ("usb", "radio") else " (check the wire)")
        return Result(11, "cut", "FAIL", f"the link did not come back within {BACK_WAIT_S:.0f} s{hint}", data)
    gap = b.clock() - t_lost + LINK_LOST_S
    data.update(back_after_s=round(gap, 1), link_notes_during=b.reader.notes.count - notes_before)
    return Result(11, "cut", "OK", f"lost, then back about {gap:.0f} s after the cut, with nothing restarted (the "
                  "flight ends only after 60 s without the link)", data)


def test_camera(b: Bench, device: str | None) -> Result:
    cfg = b.cfg
    device = str(device if device is not None else cfg.camera.device)
    try:
        from hima.sources.camera import Camera, CameraError
    except ImportError as e:
        return Result(12, "camera", "FAIL", f"cannot load the camera code: {e}")
    try:
        cam = Camera(device, cfg.camera.width_px, cfg.camera.height_px)
    except (CameraError, OSError) as e:
        return Result(12, "camera", "FAIL", f"{e} (another camera: --camera 1; on a laptop 0 is often its own camera)")
    try:
        n0, t0 = cam.frames_read, b.clock()
        b.wait(lambda: cam.ended, CAMERA_S)               # a video file ends early
        secs = max(1e-6, b.clock() - t0)
        fps = (cam.frames_read - n0) / secs
        size = (cam.width_px, cam.height_px)
        want = (cfg.camera.width_px, cfg.camera.height_px)
        data = {"device": device, "fps": round(fps, 1), "size": list(size), "asked": list(want), "seconds": round(secs, 1),
                "error": cam.error}
        if cam.ended and not cam.is_file:
            return Result(12, "camera", "FAIL", f"camera {device} stopped: {cam.error}", data)
        warns = []
        if size != want and not cam.is_file:
            warns.append(f"it gives {size[0]}x{size[1]}, hima.toml asks {want[0]}x{want[1]}")
        if fps < cfg.camera.fps:
            warns.append(f"{fps:.1f} frames/s, the flight takes {cfg.camera.fps:g}")
        detail = f"camera {device}: {size[0]}x{size[1]}, {fps:.1f} frames/s over {secs:.0f} s"
        return Result(12, "camera", "WARN" if warns else "OK", detail + ("; " + "; ".join(warns) if warns else ""), data)
    finally:
        cam.close()


def test_station(cfg) -> Result:
    try:
        from hima.contract.delivery import DeliveryKeyError, fingerprint, load_key
        from hima.onboard.deliver import config_path, ping
    except ImportError as e:
        return Result(13, "station", "SKIP", f"delivery code not loaded: {e}")
    try:
        key = load_key(config_path(cfg, cfg.delivery.key_file))
    except DeliveryKeyError as e:
        return Result(13, "station", "SKIP", f"delivery not set up here ({e})")
    t0 = time.monotonic()
    ok, detail = ping(cfg, key)
    ms = (time.monotonic() - t0) * 1000
    if ok:
        return Result(13, "station", "OK", f"{detail}, same key ({fingerprint(key)}), answered in {ms:.0f} ms",
                      {"ms": round(ms)})
    return Result(13, "station", "WARN", f"not reachable now: {detail} (the flight is not affected: the package waits)")


PREFLIGHT_SKY = ("gps", "home", "pose")            # these wait for the open sky (test 6), not for the link
_CHECK_LINE = re.compile(r"\s*\[(OK|WARN|FAIL)\s*\]\s+(\S+)\s+(.*)")


def preflight_cmd(link: Link, camera: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "hima", "preflight", "--mavlink", link.url, "--no-llm", "--no-vlm"]
    if not is_network(link.url):
        cmd += ["--baud", str(link.baud)]
    if camera is not None:
        cmd += ["--camera", str(camera)]
    return cmd


def test_preflight(link: Link, camera: str | None, system: Path, *, wait_s: float, no_camera: bool = False,
                   console: Any = None, say: Callable[[str], None] = print, timeout_s: float | None = None,
                   cmd: list[str] | None = None) -> Result:
    """Test 14: the flight program's own checks before take-off (python -m hima preflight) on the same port, with the
    code that flies: the port, the HEARTBEAT and the streams, then GPS / HOME / pose (it waits up to
    [flight].preflight_wait_s = wait_s for them), the camera, the weights, the disk, the GPU. Nothing is recorded,
    nothing is armed. Its lines are shown as they come; s + Enter stops the wait for the GPS."""
    cmd = cmd or preflight_cmd(link, camera)
    timeout_s = timeout_s if timeout_s is not None else wait_s + 240.0
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(cmd, cwd=str(system), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
    except OSError as e:
        return Result(14, "preflight", "FAIL", f"cannot start python -m hima preflight: {e}")
    q: queue.Queue = queue.Queue()

    def pipe() -> None:
        for line in p.stdout:
            q.put(line.rstrip())
        q.put(None)

    threading.Thread(target=pipe, daemon=True).start()
    out: list[str] = []
    waiting = eof = False
    why = None
    try:
        while not (eof and p.poll() is not None):
            try:
                line = q.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line is None:
                eof = True
            elif line.strip():
                out.append(line)
                say("    | " + line.strip())
                waiting = waiting or line.strip().startswith("waiting for")
            if time.monotonic() - t0 > timeout_s:
                why = "timeout"
                break
            if console is not None and _skip(console.poll()):
                why = "stopped"
                break
    finally:
        if p.poll() is None:                              # timeout, s + Enter or Ctrl+C: the port is let go
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    checks = [{"level": m.group(1), "name": m.group(2), "detail": m.group(3)}
              for m in (_CHECK_LINE.match(s) for s in out) if m]
    data = {"exit": p.returncode, "seconds": round(time.monotonic() - t0), "checks": checks, "last_lines": out[-4:]}
    if why == "timeout":
        return Result(14, "preflight", "FAIL", f"python -m hima preflight did not finish in {timeout_s:.0f} s", data)
    if not checks:
        if why == "stopped":
            if waiting:
                return Result(14, "preflight", "WARN", f"stopped by you while the flight program waited for the GPS; "
                              f"it had already opened {link.url} and seen the Pixhawk's HEARTBEAT (it waits for the "
                              "GPS only after that)", data)
            return Result(14, "preflight", "SKIP", "stopped by you before its checks ended", data)
        return Result(14, "preflight", "FAIL", f"python -m hima preflight exit {p.returncode}: "
                      + " | ".join(s.strip() for s in out[-4:]), data)

    def listed(cs: list[dict]) -> str:
        return "; ".join(f"{c['name']}: {c['detail']}" for c in cs)

    fails = [c for c in checks if c["level"] == "FAIL"]
    note = ""
    if no_camera and any(c["name"] == "camera" for c in fails):
        fails = [c for c in fails if c["name"] != "camera"]
        note = " (its camera FAIL is not counted: --no-camera)"
    px = next((c for c in checks if c["name"] == "pixhawk"), None)
    sky = [c for c in fails if c["name"] in PREFLIGHT_SKY]
    other = [c for c in fails if c["name"] not in PREFLIGHT_SKY]
    if px is None or px["level"] == "FAIL":
        return Result(14, "preflight", "FAIL", "the flight program cannot talk to the Pixhawk: "
                      + (px["detail"] if px else listed(fails) or "no pixhawk line"), data)
    if other:
        return Result(14, "preflight", "FAIL", f"the flight program talks to the Pixhawk on {link.url}, but: "
                      f"{listed(other)}" + (f"; it also waits for the open sky: {listed(sky)}" if sky else "") + note,
                      data)
    if sky:
        return Result(14, "preflight", "WARN", f"the flight program talks to the Pixhawk on {link.url} "
                      f"({px['detail']}); it waits for the open sky: {listed(sky)}" + note, data)
    warns = [c for c in checks if c["level"] == "WARN"]
    return Result(14, "preflight", "WARN" if warns else "OK",
                  f"the flight program's own checks passed on {link.url} in {data['seconds']} s "
                  f"({', '.join(c['name'] for c in checks)})" + (f"; {listed(warns)}" if warns else "") + note, data)


# ---------------------------------------------------------------------------------------------- whole run
def run_tests(b: Bench, results: list[Result], *, find_result: Result, hands: bool, window_s: float,
              gps_wait_s: float, camera: str | None = None, camera_test: bool = True, station_test: bool = True,
              header: Callable[[int, str], None] = lambda n, name: None,
              show: Callable[[Result], None] = lambda r: None) -> list[Result]:
    """Tests 1 (version) to 13, appended to results as they finish (a Ctrl+C keeps the ones done)."""
    def add(r: Result) -> None:
        results.append(r)
        show(r)

    st = b.reader.stats
    v = b.autopilot_version()
    hb = st.heartbeat or {}
    if v is not None:
        sw = v.flight_sw_version
        find_result.data["version"] = f"{(sw >> 24) & 255}.{(sw >> 16) & 255}.{(sw >> 8) & 255}"
        find_result.detail += f", firmware {find_result.data['version']}"
    find_result.data.update(vehicle=VEHICLES.get(hb.get("type"), str(hb.get("type"))), mavlink=hb.get("mavlink"),
                            system=(b.reader.autopilot or (None,))[0])
    find_result.detail += f", {find_result.data['vehicle']}, MAVLink {hb.get('mavlink', '?')}"
    if hb.get("mavlink") == 1:
        find_result.level = "WARN" if find_result.level == "OK" else find_result.level
        find_result.detail += " (the port speaks MAVLink 1: set its SERIALn_PROTOCOL = 2)"
    add(find_result)

    header(2, "params (read only)")
    add(test_params(b))
    header(3, f"quality, rates, freshness: {window_s:g} s at the flight's own message rates")
    for r in test_window(b, window_s):
        add(r)
    header(6, "gps, home, battery")
    add(test_gps(b, gps_wait_s))
    if hands:
        header(7, "rotate (hands)")
        add(test_rotate(b))
        header(8, "lift (hands)")
        add(test_lift(b))
        header(9, "arm and disarm (hands, PROPELLERS OFF)")
        add(test_arm(b))
    else:
        for n, name in ((7, "rotate"), (8, "lift"), (9, "arm")):
            add(Result(n, name, "SKIP", "--no-hands"))
    header(10, "texts to the ground station")
    add(test_texts(b, hands))
    if hands:
        header(11, "cut the link (hands)")
        add(test_cut(b))
    else:
        add(Result(11, "cut", "SKIP", "--no-hands"))
    header(12, "camera")
    add(test_camera(b, camera) if camera_test else Result(12, "camera", "SKIP", "--no-camera"))
    header(13, "ground station (Wi-Fi delivery)")
    add(test_station(b.cfg) if station_test else Result(13, "station", "SKIP", "--no-station"))
    return results


def verdict(results: list[Result]) -> tuple[str, str]:
    fails = [r for r in results if r.level == "FAIL"]
    if any(r.name in CORE for r in fails):
        return "NOT READY", "a test of the link to the Pixhawk failed: fix it first"
    if fails:
        return ("LINK OK, SOME TESTS FAILED", "the link to the Pixhawk passed; read the FAIL lines (the GPS needs the "
                "open sky)")
    if any(r.level == "WARN" for r in results):
        return "READY, WITH WARNINGS", "read the WARN lines"
    return "READY", "every test passed"


def latest_report(out_dir: Path) -> Path | None:
    reports = sorted(out_dir.glob("links-*.json"))
    return reports[-1] if reports else None


def write_report(results: list[Result], links: dict, meta: dict, out_dir: Path,
                 compare: dict | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    v, why = verdict(results)
    doc = {"schema": "hima.link_tests/1", "time_utc": stamp, "verdict": v, "meta": meta, "links": links,
           "results": [asdict(r) for r in results]}
    jp = out_dir / f"links-{stamp}.json"
    jp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    lines = [f"# HIMA link tests {stamp}", "", f"**{v}**: {why}", "",
             f"- computer: {meta.get('device', '?')}, Python {meta.get('python', '?')}"]
    for role, link in links.items():
        if link:
            lines.append(f"- {role} link: {link['url']}" + ("" if is_network(link["url"]) else f" at {link['baud']} baud")
                         + f" ({link['kind']}" + (f": {link['why']}" if link.get("why") else "") + ")")
        else:
            lines.append(f"- {role} link: none")
    lines += [f"- note: {n}" for n in meta.get("notes", [])]
    lines += ["", "| # | test | result | detail |", "|---|---|---|---|"]
    lines += [f"| {r.n} | {r.name} | {r.level} | {r.detail.replace('|', '/')} |" for r in results]
    if compare:
        before = {r["name"]: r for r in compare.get("results", [])}
        lines += ["", f"## Compared with {compare.get('_file', 'the earlier run')} ({compare.get('time_utc', '?')})", "",
                  "| test | before | now |", "|---|---|---|"]
        lines += [f"| {r.name} | {before[r.name]['level'] if r.name in before else '-'} | {r.level} |" for r in results]
    lines += ["", "Next, on the drone computer: wire the Pixhawk's TELEM2 to the Jetson's UART (TX to pin 10, RX to pin 8, "
              "GND to pin 6), copy this .json into system/runs/link_tests/, run `bash launcher/test_links.sh --compare` "
              "and read the lines that changed."]
    mp = out_dir / f"links-{stamp}.md"
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mp, jp


def main_launcher(cfg, dev: dict, args, *, system: Path, candidates: list[tuple[str, list[int], str]],
                  say: Callable[[str], None] = print) -> int:
    """hima_start.py --links (inside .venv). candidates: hima_start.port_candidates(cfg, dev)."""
    ui = None
    if not getattr(args, "no_ui", True):
        import link_ui
        ui = link_ui.LinkUI()
        try:
            url = ui.start()
        except OSError as e:
            say(f"  the page did not start ({e}): the console works as before")
            ui = None
        else:
            shown = link_ui.open_browser(url)
            say(f"  the page: {url}" + ("" if shown else " (open it in a browser on this computer)"))
            out_say = say

            def say(line: str) -> None:                    # noqa: F811 - the console and the page
                out_say(line)
                ui.say(line)

    def header(n: int, name: str) -> None:
        say(f"\n== test {n}: {name}")
        if ui:
            ui.header(n, name)

    def show(r: Result) -> None:
        say(f"  [{r.level:<4}] {r.detail}")
        if ui:
            ui.show(r)

    out_dir = system / "runs" / "link_tests"
    compare = None
    if args.compare:
        cp = latest_report(out_dir) if args.compare == "latest" else Path(args.compare)
        try:
            compare = json.loads(cp.read_text(encoding="utf-8")) if cp else None
            if compare is not None:
                compare["_file"] = cp.name
        except (OSError, ValueError) as e:
            say(f"  cannot read {cp} to compare: {e}")
        if compare is None:
            say(f"  nothing to compare with: put the earlier run's .json in {out_dir}")

    say("\nHIMA link tests (nothing on the Pixhawk is changed). Plugged in: the Pixhawk (USB, or the TELEM2 adapter)\n"
        "and, if you have it, the ground radio's USB. Close Mission Planner first: it holds the ports.")
    header(1, "find the links")
    comp, gnd, notes = find_links(candidates, mavlink=args.mavlink, baud=args.baud, ground=args.ground,
                                  ground_baud=args.ground_baud, cfg_baud=int(cfg.mavlink.baud), say=say)
    for n in notes:
        say(f"  note: {n}")
    if comp is None:
        if gnd is not None:
            say(f"  only the ground radio answered ({gnd.url}): plug the Pixhawk in too (USB or the TELEM2 adapter), "
                "or name a port with --mavlink")
        if ui:
            ui.finish("NOT READY: no autopilot answered", "")
        show(Result(1, "find", "FAIL", "no autopilot answered on a companion port. TELEM2: TX to RX, RX to TX, GND to "
                    "GND (no 5V), SERIAL2_PROTOCOL = 2, SERIAL2_BAUD = 921, then reboot the Pixhawk. USB: close "
                    "Mission Planner"))
        return 1
    find = Result(1, "find", "OK", f"{comp.firmware} on {comp.url}" + ("" if comp.kind == "network" else
                  f" at {comp.baud} baud") + f" ({comp.kind})" +
                  (f"; ground link {gnd.url}" + ("" if gnd.kind == "network" else f" at {gnd.baud}") if gnd else
                   "; no ground link here"),
                  {"companion": asdict(comp), "ground": asdict(gnd) if gnd else None})
    if comp.kind in ("usb", "radio"):
        find.level = "WARN"
        find.detail += ": the TELEM2 wire is not the link tested"
    reader = CompanionReader(url=comp.url, baud=comp.baud)
    ground = GroundLink(gnd.url, gnd.baud) if gnd else None
    console: Any = NullConsole() if args.no_hands else Console()
    if ui:
        import link_ui
        console = link_ui.UiConsole(ui, console)
    b = Bench(cfg, reader, comp, ground, gnd, console=console, say=say,
              on_prompt=ui.prompt if ui else (lambda text: None))
    results: list[Result] = []
    stopped = False
    try:
        if not reader.wait_heartbeat(cfg.mavlink.heartbeat_timeout_s):
            show(Result(1, "find", "FAIL", f"{comp.url} answered the search but not the flight's reader"))
            return 1
        run_tests(b, results, find_result=find, hands=not args.no_hands, window_s=args.window,
                  gps_wait_s=args.gps_wait, camera=args.camera, camera_test=not args.no_camera,
                  station_test=not args.no_station, header=header, show=show)
    except KeyboardInterrupt:
        stopped = True
        say("\n  stopped (Ctrl+C): the report keeps the tests done so far")
    finally:
        reader.close()
        if ground is not None:
            ground.close()
    if not stopped and args.no_preflight:
        results.append(Result(14, "preflight", "SKIP", "--no-preflight"))
    elif not stopped:
        header(14, "the flight program's own checks before take-off (python -m hima preflight; the links are closed "
                   "for it)")
        if not args.no_hands:
            say("  (s + Enter stops its wait for the GPS)")
        try:
            if ui:
                ui.prompt("The flight program's checks run (s stops its wait for the GPS).")
            r = test_preflight(comp, args.camera, system, wait_s=float(cfg.flight.preflight_wait_s),
                               no_camera=args.no_camera, console=b.console, say=say)
            if ui:
                ui.prompt(None)
            results.append(r)
            show(r)
        except KeyboardInterrupt:
            stopped = True
            say("\n  stopped (Ctrl+C): the report keeps the tests done so far")
    meta = {"device": " ".join(x for x in (dev.get("os"), dev.get("machine")) if x) or dev.get("kind", "?"),
            "python": platform.python_version(), "notes": notes, "window_s": args.window,
            "stopped": stopped, "port_errors": b.port_errors[-5:]}
    if dev.get("model"):
        meta["device"] += f", {dev['model']}"
    md, js = write_report(results, {"companion": asdict(comp), "ground": asdict(gnd) if gnd else None}, meta,
                          out_dir, compare)
    v, why = verdict(results)
    say("\n== summary")
    for r in results:
        say(f"  [{r.level:<4}] {r.n:>2} {r.name:<14} {r.detail[:110]}")
    if compare:
        say(f"\n== compared with {compare['_file']}")
        before = {r["name"]: r["level"] for r in compare.get("results", [])}
        changed = [r for r in results if before.get(r.name) != r.level]
        for r in changed:
            say(f"  {r.name:<14} {before.get(r.name, '-'):<5} -> {r.level}")
        if not changed:
            say("  no test changed")
    say(f"\n{v}: {why}\nreport: {md}\n        {js}")
    if ui:
        ui.finish(f"{v}: {why}", str(md))
    if stopped:
        return 130
    return 1 if any(r.level == "FAIL" for r in results) else 0
