"""How long the drone may stay over one animal. Fixed rule, no LLM.

A "stay" is the time one track has been in view without a break longer than [dwell].max_gap_s. When a stay
reaches max_over_animal_s, one message advises the pilot to leave:

    "HIMA A<seq> T<id> <OR|GZ> DWELL <min>m @<t> LEAVE"      (at most 41 bytes, one STATUSTEXT)

once per track, and at most one message every repeat_s for the whole flight (a herd in view would raise one per
animal at the same moment). max_over_animal_s must be longer than injury.stage3_s (checked in config.py), so an
animal that lies still alone gets all three injury stages before the advice to leave.
Limits: the system does not fly the drone (ArduPilot does); this is advice to the pilot on the ground. A track that
the tracker loses and finds again under a new id starts a new stay (the time is under-counted, never over-counted).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hima.config import Config
from hima.onboard.alerts import SPECIES_CODE, AlertSeq

PRIORITY = 3
MAV_SEVERITY_WARNING = 4


@dataclass(frozen=True)
class DwellAlert:
    seq: int
    t: float
    track_id: int
    species: str
    stay_s: float
    priority: int
    severity: int
    text: str
    source: str = "dwell"


@dataclass
class DwellWatch:
    cfg: Config
    counter: AlertSeq
    alerts: list[DwellAlert] = field(default_factory=list)
    held: list[tuple[float, int, str]] = field(default_factory=list)    # (t, track, why)
    _start: dict[int, float] = field(default_factory=dict)
    _last: dict[int, float] = field(default_factory=dict)
    _done: set[int] = field(default_factory=set)
    _last_alert_t: float | None = None

    def on_frame(self, t: float, detections: list[dict[str, Any]]) -> list[DwellAlert]:
        d = self.cfg.dwell
        new: list[DwellAlert] = []
        for det in detections:
            tid = det["track_id"]
            if tid not in self._last or t - self._last[tid] > d.max_gap_s:
                self._start[tid] = t                  # a new stay
            self._last[tid] = t
            stay = t - self._start[tid]
            if stay < d.max_over_animal_s or tid in self._done:
                continue
            if self._last_alert_t is not None and t - self._last_alert_t < d.repeat_s:
                if not any(h[1] == tid for h in self.held):
                    self.held.append((round(t, 3), tid, "repeat_s"))
                continue
            self._done.add(tid)
            self._last_alert_t = t
            seq = self.counter.next()
            text = (f"HIMA A{seq} T{tid} {SPECIES_CODE[det['species']]} DWELL {int(stay // 60)}m "
                    f"@{t:.0f} LEAVE")
            if len(text.encode("ascii")) > self.cfg.alerts.max_message_bytes:
                raise ValueError(f"dwell text is too long: {text!r}")
            new.append(DwellAlert(seq, t, tid, det["species"], round(stay, 3), PRIORITY, MAV_SEVERITY_WARNING, text))
        self.alerts.extend(new)
        return new

    def stays(self, t: float) -> dict[int, float]:
        """Current stays (tracks seen within max_gap_s of t), for the flight log."""
        return {tid: round(t - self._start[tid], 3) for tid, last in self._last.items()
                if t - last <= self.cfg.dwell.max_gap_s}
