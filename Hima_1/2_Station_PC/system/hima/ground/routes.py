"""The Route page's logic: the drone's proposed route before take-off. The operator can
move, add and delete points, read any point's GPS position, and send the edited route or let the drone fly its own.
The drone waits [route_review].wait_s (60 s), then flies its own route.

An edited route is checked here first (planner.plan_from_targets: safe area, landing approach, battery) when this
station has the reserve boundary; the drone checks it again before it accepts it. The map itself is
route_map/index.html (no outside files: the station in the field has no internet), shown by the web dashboard.
"""
from __future__ import annotations

import datetime as dt
import math
import threading
from pathlib import Path
from typing import Any

from hima.config import Config
from hima.ground.imagery import images_for
from hima.ground.plans import PlanStore

LAUNCH_MATCH_M = 1000.0
_RESERVE: dict[str, tuple[Any, str | None]] = {}
_LOCK = threading.Lock()


def _utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def deadline(offer: dict[str, Any]) -> dt.datetime | None:
    """When the drone stops waiting, on this PC's clock: received + the wait (radio: what was left when read)."""
    got = _utc(offer.get("received_utc"))
    if got is None:
        return None
    left = offer.get("wait_left_s", offer.get("wait_s")) or 0
    return got + dt.timedelta(seconds=float(left))


def pending(store: PlanStore) -> str | None:
    """The newest offer still waiting for an answer, if any."""
    now = dt.datetime.now(dt.timezone.utc)
    for pid in store.ids()[:3]:
        off = store.offer(pid)
        d = None if off is None else deadline(off)
        if d is not None and d > now and store.answer(pid) is None:
            return pid
    return None


def _reserve(cfg: Config):
    with _LOCK:
        if cfg.sha256 not in _RESERVE:
            from hima.autonomy.reserve import Reserve
            try:
                _RESERVE[cfg.sha256] = (Reserve.load(cfg), None)
            except Exception as e:                                # noqa: BLE001 - a station without the boundary file
                _RESERVE[cfg.sha256] = (None, f"{type(e).__name__}: {e}")
        return _RESERVE[cfg.sha256]


def _hav(a, b) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def check_route(cfg: Config, offer: dict[str, Any], targets: list[list[float]]) -> tuple[bool, str]:
    """The planner's own check, when this station has the reserve boundary; else the length only."""
    from hima.autonomy.planner import PlanError, budget, plan_from_targets
    la = (offer["launch"]["lat_deg"], offer["launch"]["lon_deg"])
    pts = [(float(a), float(b)) for a, b in targets]
    length = sum(_hav(p, q) for p, q in zip([la] + pts, pts + [la]))
    r, err = _reserve(cfg)
    if r is None:
        b = budget(cfg).route_m
        ok = length <= b
        return ok, (f"{length / 1e3:.1f} km of {b / 1e3:.1f} km allowed"
                    + ("" if ok else ": too long") + f" (safe area not checked here: {err}; the drone checks it)")
    name, d = min(((n, _hav(la, p)) for n, p in cfg.reserve.launch.items()), key=lambda x: x[1])
    if d > LAUNCH_MATCH_M:
        return False, f"the launch point is {d:.0f} m from the nearest [reserve.launch] point"
    try:
        plan = plan_from_targets(cfg, r, name, dt.date.today(), pts)
    except PlanError as e:
        return False, str(e)
    return True, f"OK: {len(pts)} points, {plan.length_m / 1e3:.1f} km of {plan.budget.route_m / 1e3:.1f} km allowed"


def area(offer: dict[str, Any], pad_deg: float = 0.05) -> tuple[float, float, float, float]:
    pts = [[offer["launch"]["lat_deg"], offer["launch"]["lon_deg"]]] + offer["targets"]
    la = [p[0] for p in pts]
    lo = [p[1] for p in pts]
    return min(la) - pad_deg, min(lo) - pad_deg, max(la) + pad_deg, max(lo) + pad_deg


def offers(store: PlanStore) -> list[dict[str, Any]]:
    """Every offer, newest first, with its state (for the Route page's list)."""
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for pid in store.ids():
        off, ans, res = store.offer(pid), store.answer(pid), store.result(pid)
        if off is None:
            out.append({"plan_id": pid, "state": "unreadable"})
            continue
        d = deadline(off)
        state = ("demo" if off.get("demo") else "flown: " + str(res.get("used")) if res
                 else "answered: " + ans["decision"] if ans
                 else "waiting" if d is not None and d > now else "over")
        out.append({"plan_id": pid, "state": state, "points": len(off["targets"]), "route_km": off.get("route_km"),
                    "received_utc": off.get("received_utc"), "demo": bool(off.get("demo"))})
    return out


def route_view(cfg: Config, store: PlanStore, pid: str, imagery_dir: Path) -> dict[str, Any] | None:
    """Everything the Route page shows for one offer, and the map's arguments."""
    offer = store.offer(pid)
    if offer is None:
        return None
    answer, result = store.answer(pid), store.result(pid)
    d = deadline(offer)
    left = None if d is None else (d - dt.datetime.now(dt.timezone.utc)).total_seconds()
    images = images_for(imagery_dir, area(offer))
    la = [offer["launch"]["lat_deg"], offer["launch"]["lon_deg"]]
    return {
        "plan_id": pid, "demo": bool(offer.get("demo")), "points": len(offer["targets"]),
        "route_km": offer.get("route_km"), "route_budget_km": offer.get("route_budget_km"),
        "seen_via": offer.get("seen_via", [offer.get("source", "?")]), "received_utc": offer.get("received_utc"),
        "left_s": left, "answer": answer, "result": result, "wait_s": cfg.route_review.wait_s,
        "images": [{k: im[k] for k in ("name", "source", "licence")} for im in images],
        "imagery_dir": str(imagery_dir),
        "targets": (answer or {}).get("targets") or offer["targets"],
        "map": {"plan_id": pid, "launch": la, "targets": offer["targets"], "fence": offer.get("fence") or [],
                "route_budget_km": offer.get("route_budget_km"), "plan_margin_m": offer.get("plan_margin_m"),
                "land_approach_m": offer.get("land_approach_m"),
                "deadline_ms": None if d is None else int(d.timestamp() * 1000),
                "editable": not offer.get("demo") and answer is None and (left is None or left > 0),
                "answered": None if answer is None else answer["decision"], "images": images, "height": 640},
    }


def act(cfg: Config, store: PlanStore, pid: str, action: str, targets: list[list[float]]) -> tuple[bool, str]:
    """The operator's button on the map: check / edit (send the edited route) / approve (fly the drone's own)."""
    offer = store.offer(pid)
    if offer is None:
        return False, "no such route offer"
    if action == "check":
        return check_route(cfg, offer, targets)
    if action not in ("edit", "approve"):
        return False, f"unknown action {action!r}"
    if offer.get("demo"):
        return False, "Demo route: nothing waits on it, and nothing is sent."
    if store.answer(pid) is not None:
        return False, "This route already has an answer."
    d = deadline(offer)
    if d is not None and d <= dt.datetime.now(dt.timezone.utc):
        return False, "Too late: the drone has stopped waiting and flies its own route."
    if action == "approve":
        store.write_answer(pid, "approve")
        return True, "Sent: the drone flies its own route now."
    ok, msg = check_route(cfg, offer, targets)
    if not ok:
        return False, f"Not sent: {msg}"
    store.write_answer(pid, "edit", targets)
    return True, f"Sent: {msg}. The drone checks it again before it flies it."
