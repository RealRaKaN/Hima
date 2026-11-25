"""The route is shown at the station before take-off and may be changed there.

After the drone has planned today's route and uploaded it to the autopilot (onflight.prepare), it offers it to the
station and waits [route_review].wait_s (60 s). Two ways, both at once:
  Wi-Fi  (the same signed connection as the packages, [delivery].station_url): POST /v1/plan with the route and the
         fence; then GET /v1/plan/answer every poll_s.
  radio  (the telemetry radio, through the autopilot): a STATUSTEXT "HIMA PLAN <stamp> <n>p <km>km <s>s". The
         station's radio bridge reads the route from the autopilot (mission download: points only, no images), and
         answers with MAV_CMD_USER_1 to the Jetson (system 1, component 191): param1 = 1 approve, 2 edited (the
         station uploaded its route to the autopilot first), param2 = the plan's number (HHMMSS of its stamp).
Answers: approve (fly the drone's route now), edit (the new points). No answer in wait_s, or the station cannot be
reached: the drone flies its own route. An edited route is checked on the drone like its own
(planner.plan_from_targets: safe area, landing approach, battery); a route that fails is refused, the drone's own
route goes back to the autopilot and the reason goes to the station. Whatever is chosen, the drone builds the mission
itself (take-off, speed, patrol height, VTOL landing) from the points and uploads it, so the autopilot never keeps a
mission the drone did not check.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hima.autonomy.export import NAV_WAYPOINT, write_plan
from hima.autonomy.planner import Plan, PlanError, plan_from_targets
from hima.config import Config

APPROVE, EDITED = 1, 2


def plan_number(plan_id: str) -> int:
    """HHMMSS of plan-YYYYMMDDTHHMMSSZ: fits a float param exactly."""
    return int(plan_id[-7:-1])


def offer_doc(cfg: Config, reserve, plan: Plan, plan_id: str, wait_s: float) -> dict[str, Any]:
    b = plan.budget
    return {"schema_version": "hima.route_offer/1", "plan_id": plan_id,
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "wait_s": wait_s,
            "launch": {"name": plan.launch, "lat_deg": plan.launch_latlon[0], "lon_deg": plan.launch_latlon[1]},
            "targets": [[round(a, 7), round(c, 7)] for a, c in plan.targets_latlon],
            "fence": [[round(a, 7), round(c, 7)] for a, c in reserve.fence_latlon()],
            "route_km": round(plan.length_m / 1e3, 2), "route_budget_km": round(b.route_m / 1e3, 2),
            "patrol_alt_m": cfg.vehicle.patrol_alt_m, "plan_margin_m": reserve.plan_margin_m,
            "land_approach_m": cfg.vehicle.land_approach_m, "summary": plan.summary()}


def targets_from_mission(items: list[dict[str, float]]) -> list[tuple[float, float]]:
    """The route points of a mission read from the autopilot: NAV_WAYPOINT items after home (seq 0)."""
    return [(it["lat"], it["lon"]) for it in items if it["seq"] > 0 and int(it["cmd"]) == NAV_WAYPOINT]


class RouteReview:
    def __init__(self, cfg: Config, reader: Any, executor: Any, reserve: Any, plan: Plan, scores: Any, out: Path,
                 today, log: Callable[[str], None] = print, client: Any = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.reader, self.ex, self.reserve = cfg, reader, executor, reserve
        self.plan, self.scores, self.out, self.today, self.log = plan, scores, Path(out), today, log
        self.client, self.clock, self.sleep = client, clock, sleep
        self.plan_id = "plan-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.info: dict[str, Any] = {"plan_id": self.plan_id, "wait_s": cfg.route_review.wait_s,
                                     "wifi": None, "radio": None, "answer": None, "used": "drone", "reason": ""}

    # ---- the two ways -------------------------------------------------------------------------------
    def _wifi_client(self):
        if self.client is not None or not self.cfg.route_review.wifi:
            return self.client
        from hima.contract import delivery as dl
        from hima.onboard.deliver import StationClient, config_path
        key = dl.load_key(config_path(self.cfg, self.cfg.delivery.key_file))
        self.client = StationClient(self.cfg.delivery.station_url, key, min(5.0, self.cfg.delivery.timeout_s))
        return self.client

    def _offer_wifi(self, doc: dict[str, Any]) -> None:
        try:
            c = self._wifi_client()
            if c is None:
                self.info["wifi"] = "off"
                return
            st, ans = c.call("POST", "/v1/plan", json.dumps(doc).encode("utf-8"))
            self.info["wifi"] = "shown" if st == 200 else f"refused {st}: {ans.get('error')}"
        except Exception as e:                                    # noqa: BLE001 - no Wi-Fi at the launch point
            self.info["wifi"] = f"unreachable: {type(e).__name__}: {e}"[:200]
            self.client = None

    def _say(self, text: str) -> None:
        try:
            self.reader.send_statustext(text[:50], 5, 0, 0)       # MAV_SEVERITY_NOTICE
        except Exception as e:                                    # noqa: BLE001
            self.log(f"  route review: could not send {text!r} ({type(e).__name__}: {e})")

    # ---- waiting --------------------------------------------------------------------------------------
    def run(self) -> Plan:
        rr = self.cfg.route_review
        doc = offer_doc(self.cfg, self.reserve, self.plan, self.plan_id, rr.wait_s)
        if rr.wifi:
            self._offer_wifi(doc)
        if rr.radio:
            self._say(f"HIMA PLAN {self.plan_id[5:]} {len(self.plan.targets_latlon)}p "
                      f"{self.plan.length_m / 1e3:.0f}km {rr.wait_s:.0f}s")
            self.info["radio"] = "sent"
        self.log(f"route {self.plan_id} offered to the station (Wi-Fi: {self.info['wifi']}; radio: "
                 f"{self.info['radio']}); waiting {rr.wait_s:g} s for an answer")
        seen_cmds = len(getattr(self.reader, "station_cmds", []))
        t0 = self.clock()
        next_poll = t0
        answer = None
        while self.clock() - t0 < rr.wait_s and answer is None:
            self.reader.heartbeat()
            self.reader.pump(None)
            cmds = getattr(self.reader, "station_cmds", [])
            for c in cmds[seen_cmds:]:
                if int(round(c["p"][1])) == plan_number(self.plan_id) and int(round(c["p"][0])) in (APPROVE, EDITED):
                    answer = {"via": "radio", "decision": "approve" if int(round(c["p"][0])) == APPROVE else "edit"}
                    break
            seen_cmds = len(cmds)
            if answer is None and self.client is not None and self.clock() >= next_poll:
                next_poll = self.clock() + rr.poll_s
                try:
                    st, a = self.client.call("GET", "/v1/plan/answer", query={"plan_id": self.plan_id})
                    if st == 200 and a.get("decision") in ("approve", "edit"):
                        answer = {"via": "wifi", "decision": a["decision"], "targets": a.get("targets")}
                except Exception as e:                            # noqa: BLE001 - the Wi-Fi went away: radio only
                    self.log(f"  route review: Wi-Fi lost ({type(e).__name__}: {e})")
                    self.client = None
            if answer is None:
                self.sleep(0.05)
        self.info["waited_s"] = round(self.clock() - t0, 1)
        self.info["answer"] = answer
        plan = self._decide(answer)
        self._report()
        return plan

    def _decide(self, answer: dict[str, Any] | None) -> Plan:
        if answer is None:
            self.info["reason"] = f"no answer in {self.cfg.route_review.wait_s:g} s"
            return self.plan
        if answer["decision"] == "approve":
            self.info["reason"] = f"approved at the station ({answer['via']})"
            return self.plan
        try:
            if answer["via"] == "wifi":
                pts = [(float(a), float(c)) for a, c in answer.get("targets") or []]
            else:
                items = self.ex.download(0)
                if items is None:
                    raise PlanError("could not read the route from the autopilot")
                pts = targets_from_mission(items)
            new = plan_from_targets(self.cfg, self.reserve, self.plan.launch, self.today, pts, self.scores)
        except PlanError as e:
            self.info["reason"] = f"edited route refused: {e}"
            self.log(f"  route review: {self.info['reason']}; the drone's own route stays")
            self._say(f"HIMA PLAN REFUSED {str(e)[:30]}")
            if answer["via"] == "radio":                          # the station's mission is on the autopilot now
                self._upload(self.out / "plan")
            return self.plan
        own = self.out / "plan"
        own.rename(self.out / "plan_drone")                       # the drone's proposal is kept next to it
        write_plan(own, self.cfg, self.reserve, new, self.scores, [])
        if not self._upload(own):
            self.info["reason"] = "edited route NOT accepted by the autopilot; the drone's own route stays"
            own.rename(self.out / "plan_refused")
            (self.out / "plan_drone").rename(own)
            self._upload(own)
            return self.plan
        self.info["used"] = "station"
        self.info["reason"] = f"edited at the station ({answer['via']}): {len(pts)} points, {new.length_m / 1e3:.1f} km"
        self._say(f"HIMA PLAN EDITED {len(pts)}p {new.length_m / 1e3:.0f}km")
        return new

    def _upload(self, plan_dir: Path) -> bool:
        from hima.autonomy.export import read_waypoints
        items = read_waypoints(plan_dir / "mission.waypoints")
        ok = bool(self.ex.upload(items, 0))
        self.info.setdefault("uploads", []).append({"dir": plan_dir.name, "items": len(items), "accepted": ok})
        return ok

    def _report(self) -> None:
        self.log(f"route: the drone flies the {self.info['used']} route ({self.info['reason']})")
        if self.client is not None:
            try:
                self.client.call("POST", "/v1/plan/result", json.dumps({
                    "plan_id": self.plan_id, "used": self.info["used"], "reason": self.info["reason"],
                    "waited_s": self.info.get("waited_s")}).encode("utf-8"))
            except Exception:                                     # noqa: BLE001 - only information for the station
                pass
        if self.cfg.route_review.radio:
            self._say(f"HIMA PLAN USED {self.info['used'].upper()}")
