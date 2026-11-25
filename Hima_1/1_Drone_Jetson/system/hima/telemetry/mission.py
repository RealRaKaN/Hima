"""The MAVLink mission protocol, for any pymavlink connection: the Jetson (autonomy/executor.py) and the station's
radio bridge (ground/route_radio.py) use the same two functions.

upload:   MISSION_COUNT -> (MISSION_REQUEST_INT | MISSION_REQUEST) per item -> MISSION_ITEM_INT ... -> MISSION_ACK
download: MISSION_REQUEST_LIST -> MISSION_COUNT -> MISSION_REQUEST_INT per item -> MISSION_ITEM_INT -> MISSION_ACK
mission_type 0 = the route, 1 = the fence (common.xml MAV_MISSION_TYPE). Items are dicts as export.read_waypoints
gives them: seq, frame, cmd, p1..p4, lat, lon, alt. Messages that are not part of the exchange go to on_other.
"""
from __future__ import annotations

import time
from typing import Any, Callable

MISSION_ACCEPTED = 0


def _noop(*_a) -> None:
    return None


def _src(m) -> int | None:
    f = getattr(m, "get_srcSystem", None)
    return f() if f else None


def upload_items(conn, target: tuple[int, int], items: list[dict[str, float]], mission_type: int, *,
                 timeout_s: float = 30.0, on_other: Callable[[Any], None] = _noop,
                 tick: Callable[[], Any] = _noop) -> bool:
    sysid, comp = target
    mav = conn.mav
    mav.mission_count_send(sysid, comp, len(items), mission_type)
    sent: set[int] = set()
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        tick()
        m = conn.recv_match(blocking=True, timeout=0.5)
        if m is None:
            continue
        typ = m.get_type()
        if typ in ("MISSION_REQUEST_INT", "MISSION_REQUEST") and getattr(m, "mission_type", 0) == mission_type \
                and _src(m) in (sysid, None) and 0 <= m.seq < len(items):
            it = items[m.seq]
            mav.mission_item_int_send(sysid, comp, m.seq, int(it["frame"]), int(it["cmd"]), 0, 1, it["p1"], it["p2"],
                                      it["p3"], it["p4"], int(round(it["lat"] * 1e7)), int(round(it["lon"] * 1e7)),
                                      float(it["alt"]), mission_type)
            sent.add(m.seq)
        elif typ == "MISSION_ACK" and getattr(m, "mission_type", 0) == mission_type and _src(m) in (sysid, None):
            return m.type == MISSION_ACCEPTED and len(sent) == len(items)
        else:
            on_other(m)
    return False


def download_items(conn, target: tuple[int, int], mission_type: int, *, timeout_s: float = 30.0,
                   on_other: Callable[[Any], None] = _noop, tick: Callable[[], Any] = _noop
                   ) -> list[dict[str, float]] | None:
    """The autopilot's items, or None (no answer in timeout_s)."""
    sysid, comp = target
    mav = conn.mav
    mav.mission_request_list_send(sysid, comp, mission_type)
    count: int | None = None
    items: dict[int, dict[str, float]] = {}
    end = time.monotonic() + timeout_s
    last_ask = time.monotonic()
    while time.monotonic() < end:
        tick()
        m = conn.recv_match(blocking=True, timeout=0.5)
        if m is None or _src(m) not in (sysid, None):
            if m is not None:
                on_other(m)
            if time.monotonic() - last_ask > 2.0:          # a lost packet: ask again
                last_ask = time.monotonic()
                if count is None:
                    mav.mission_request_list_send(sysid, comp, mission_type)
                elif len(items) < count:
                    mav.mission_request_int_send(sysid, comp, len(items), mission_type)
            continue
        typ = m.get_type()
        if typ == "MISSION_COUNT" and getattr(m, "mission_type", 0) == mission_type and count is None:
            count = m.count
            if count == 0:
                mav.mission_ack_send(sysid, comp, MISSION_ACCEPTED, mission_type)
                return []
            mav.mission_request_int_send(sysid, comp, 0, mission_type)
            last_ask = time.monotonic()
        elif typ == "MISSION_ITEM_INT" and getattr(m, "mission_type", 0) == mission_type and count is not None:
            if m.seq == len(items):
                items[m.seq] = {"seq": m.seq, "current": m.current, "frame": m.frame, "cmd": m.command,
                                "p1": m.param1, "p2": m.param2, "p3": m.param3, "p4": m.param4,
                                "lat": m.x / 1e7, "lon": m.y / 1e7, "alt": m.z}
                if len(items) == count:
                    mav.mission_ack_send(sysid, comp, MISSION_ACCEPTED, mission_type)
                    return [items[i] for i in range(count)]
                mav.mission_request_int_send(sysid, comp, len(items), mission_type)
                last_ask = time.monotonic()
        else:
            on_other(m)
    return None
