"""Live alerts from the drone. Fixed rules, no LLM: an alert must be fast, predictable and
fit one MAVLink STATUSTEXT message (text is char[50], common.xml message 253).

Rules (one state machine per track, evaluated at every onboard refresh):
  - only the statuses in PRIORITY raise alerts;
  - a "run" is a stretch of refreshes with the same status; at most one alert per run;
  - no alert before the animal was actually seen for [alerts].min_visible_s;
  - after an alert for a track, a new alert for that track waits [alerts].cooldown_s,
    EXCEPT an escalation (a more urgent status), which is sent at once;
  - an alert held back by min_visible_s or the cooldown stays pending while its run lasts;
  - SUSPECTED_INJURY_OR_ILLNESS is staged by still_alone_s, the time the animal has been alone AND in
    one place without a break ([injury]): INJ1 at stage1_s (watch it, priority 3), INJ2 at stage2_s (suspected
    injury, priority 2, the camera window may open), INJ3 at stage3_s (strong suspicion, priority 2). Each new
    stage is an escalation (sent at once). If the animal moves, the stages start again (with the cooldown).
    min_visible_s does not apply to it: stage1_s is longer.
  - POSSIBLE_HIDDEN_YOUNG: a small animal the injury rule would flag; priority 3, the command moves the
    drone off (a mother may not come back to a young with a drone overhead). It opens a camera window: the
    AI is asked whether it is a young animal or an adult lying down; the reply never changes the status.
  - SEVERE carries the cause of the run ([cause]): SEVDRN when the drone came >= approach_m closer to where
    the animal stood in the approach_s before the run and no vehicle was seen; SEVVEH when a vehicle was seen
    (objects.k samples) from vehicle_pre_s before the run up to the alert and the drone did not approach;
    SEVUNK otherwise (both, neither, or no object watch to look for a vehicle).

The suggested command in the message is the deterministic policy default (advisor/policy.py)
at the drone's current altitude, never an LLM proposal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from hima.advisor.policy import default_decision
from hima.config import Config

# Lower number = more urgent. SEVERE: the drone is harming the animal now (the pilot can act
# in seconds). INJURY: a ranger should come. PROXIMITY: the animal is reacting to the drone.
PRIORITY = {
    "SEVERE_DRONE_DISTURBANCE": 1,
    "SUSPECTED_INJURY_OR_ILLNESS": 2,
    "PROXIMITY_PACING_ALERTNESS": 3,
    "POSSIBLE_HIDDEN_YOUNG": 3,
}
# MAV_SEVERITY values (common.xml): ALERT = 1, CRITICAL = 2, WARNING = 4.
MAV_SEVERITY = {1: 1, 2: 2, 3: 4}

INJ = "SUSPECTED_INJURY_OR_ILLNESS"
INJURY_PRIORITY = {1: 3, 2: 2, 3: 2}      # stage -> priority

STATUS_CODE = {"SEVERE_DRONE_DISTURBANCE": "SEVERE", "SUSPECTED_INJURY_OR_ILLNESS": "INJURY",
               "PROXIMITY_PACING_ALERTNESS": "PROXIM", "POSSIBLE_HIDDEN_YOUNG": "YOUNG"}
SEV = "SEVERE_DRONE_DISTURBANCE"
CAUSE_CODE = {"DRONE": "DRN", "VEHICLE": "VEH", "UNKNOWN": "UNK"}
SPECIES_CODE = {"arabian_oryx": "OR", "reem_gazelle": "GZ"}
COMMAND_CODE = {"MAINTAIN_OBSERVATION": "OBSERVE", "INCREASE_ALTITUDE": "CLIMB",
                "INCREASE_STANDOFF": "STANDOFF", "RETREAT": "RETREAT", "WIDEN_ORBIT": "WIDEN",
                "INSPECT_HIGH_ALTITUDE_ZOOM": "ZOOM", "REACQUIRE_TARGET": "REACQ"}


@dataclass(frozen=True)
class Alert:
    seq: int                       # 1, 2, 3 ... in order of creation
    t: float                       # mission time of the refresh that raised it
    track_id: int
    species: str
    status: str
    priority: int
    severity: int                  # MAV_SEVERITY
    visible_s: float               # seen this long when raised
    first_seen_s: float
    command: str | None            # policy default; None without telemetry
    altitude_m: float | None
    text: str                      # the ASCII message, <= max_message_bytes
    stage: int | None = None       # injury stage 1..3; None for the other statuses
    still_alone_s: float | None = None
    cause: str | None = None       # SEVERE only: DRONE | VEHICLE | UNKNOWN


def altitude_text(alt: float) -> str:
    """Whole metres when exact, else one decimal ROUNDED UP: a "do not go below" altitude
    must never be shown lower than computed."""
    if float(alt).is_integer():
        return f"{alt:.0f}m"
    return f"{math.ceil(alt * 10 - 1e-9) / 10:.1f}m"


def format_alert(seq: int, track_id: int, species: str, status: str, t: float,
                 command: str | None, altitude_m: float | None, max_bytes: int, stage: int | None = None,
                 cause: str | None = None) -> str:
    code = f"INJ{stage}" if status == INJ and stage else STATUS_CODE[status]
    if status == SEV and cause:
        code = "SEV" + CAUSE_CODE[cause]          # 6 characters, as SEVERE: the worst case still fits 50 bytes
    parts = [f"HIMA A{seq}", f"T{track_id}", SPECIES_CODE[species], code, f"@{t:.0f}"]
    if command is not None:
        parts += [COMMAND_CODE[command], altitude_text(altitude_m)]
    text = " ".join(parts)
    data = text.encode("ascii")                       # raises on non-ASCII by construction
    if len(data) > max_bytes:
        raise ValueError(f"alert text is {len(data)} bytes, limit is {max_bytes}: {text!r}")
    return text


@dataclass
class AlertSeq:
    """One numbering for every alert of a flight (animal alerts and object alerts share A1, A2, ...)."""
    last: int = 0

    def next(self) -> int:
        self.last += 1
        return self.last


@dataclass
class _TrackState:
    run_status: str | None = None
    run_alerted: bool = False
    stage: int = 0                 # highest injury stage alerted in the current still span
    last_alert: Alert | None = None


@dataclass
class AlertEngine:
    cfg: Config
    params_known: bool
    alerts: list[Alert] = field(default_factory=list)
    held: list[tuple[float, int, str, str]] = field(default_factory=list)   # (t, track, status, why)
    counter: AlertSeq = field(default_factory=AlertSeq)
    # (t0, t1) -> a vehicle was seen in [t0, t1] (ObjectWatch.vehicle_seen); None: nobody looked for one
    vehicle_seen: Callable[[float, float], bool] | None = None
    _tracks: dict[int, _TrackState] = field(default_factory=dict)

    def on_refresh(self, t: float, analysis: dict[str, Any], drone_agl_m: float) -> list[Alert]:
        new: list[Alert] = []
        for a in analysis["animals"]:
            st = self._tracks.setdefault(a["track_id"], _TrackState())
            status = a["status"]
            if status != st.run_status:
                st.run_status, st.run_alerted = status, False
            # The stages follow the still span, not the status run: a status that flickers for one refresh
            # does not resend a stage. Only a span shorter than stage1_s (it travelled, or was not alone, or was
            # not seen) starts the stages again.
            stage = self.injury_stage(a.get("still_alone_s"))
            if stage == 0:
                st.stage = 0
            if status not in PRIORITY:
                continue
            if status == INJ:
                if stage == 0:
                    self.held.append((t, a["track_id"], status, "still_short"))
                    continue
                if stage <= st.stage:
                    continue
            else:
                stage = 0
                if st.run_alerted:
                    continue
            if not stage and a["visible_s"] < self.cfg.alerts.min_visible_s:
                self.held.append((t, a["track_id"], status, "min_visible"))
                continue
            prio = INJURY_PRIORITY[stage] if stage else PRIORITY[status]
            last = st.last_alert
            escalation = last is not None and (prio < last.priority or 0 < st.stage < stage)
            if last is not None and not escalation and t - last.t < self.cfg.alerts.cooldown_s:
                self.held.append((t, a["track_id"], status, "cooldown"))
                continue
            alert = self._make(t, a, status, drone_agl_m, stage or None)
            st.run_alerted, st.last_alert = True, alert
            if stage:
                st.stage = stage
            new.append(alert)
        self.alerts.extend(new)
        return new

    def injury_stage(self, still_alone_s: float | None) -> int:
        j = self.cfg.injury
        if still_alone_s is None:
            return 0
        return sum(still_alone_s >= s for s in (j.stage1_s, j.stage2_s, j.stage3_s))

    def run_cause(self, t: float, a: dict[str, Any]) -> str:
        c = self.cfg.cause
        closing, start = a.get("drone_closing_m"), a.get("stress_start_s")
        drone = closing is not None and closing >= c.approach_m
        vehicle = (self.vehicle_seen is not None and start is not None
                   and self.vehicle_seen(start - c.vehicle_pre_s, t))
        if vehicle and not drone:
            return "VEHICLE"
        if drone and not vehicle:
            return "DRONE"
        return "UNKNOWN"

    def _make(self, t: float, a: dict[str, Any], status: str, drone_agl_m: float,
              stage: int | None = None) -> Alert:
        seq = self.counter.next()  # not len(self.alerts): several alerts can come from one refresh
        cmd = alt = None
        if self.params_known:
            d = default_decision(status, drone_agl_m, self.cfg.safety)
            cmd, alt = d["proposed_command"], d["proposed_altitude_m"]
        cause = self.run_cause(t, a) if status == SEV else None
        text = format_alert(seq, a["track_id"], a["species"], status, t, cmd, alt,
                            self.cfg.alerts.max_message_bytes, stage, cause)
        p = INJURY_PRIORITY[stage] if stage else PRIORITY[status]
        return Alert(seq, t, a["track_id"], a["species"], status, p, MAV_SEVERITY[p],
                     a["visible_s"], a["first_seen_s"], cmd, alt, text, stage, a.get("still_alone_s"), cause)
