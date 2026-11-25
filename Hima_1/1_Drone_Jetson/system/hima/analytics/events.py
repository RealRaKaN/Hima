"""Events as time intervals, not packet counters.

Steps for one track and one event type:
  1. raw runs: consecutive flagged observations that touch in time
     (end of one observation == start of the next) form one interval
     [t_first, t_last + dt_last);
  2. merge intervals whose gap is shorter than merge_gap_s;
  3. drop intervals shorter than min_event_s.
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True)
class Interval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def flag_intervals(ts: list[float], dts: list[float], flags: list[bool], *,
                   min_event_s: float, merge_gap_s: float) -> list[Interval]:
    raw: list[Interval] = []
    for t, dt, f in zip(ts, dts, flags, strict=True):
        if not f:
            continue
        if raw and abs(raw[-1].end - t) <= EPS:
            raw[-1] = Interval(raw[-1].start, t + dt)
        else:
            raw.append(Interval(t, t + dt))

    merged: list[Interval] = []
    for iv in raw:
        if merged and iv.start - merged[-1].end < merge_gap_s - EPS:
            merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)

    return [iv for iv in merged if iv.duration >= min_event_s - EPS]


def total_duration(intervals: list[Interval]) -> float:
    return sum(iv.duration for iv in intervals)
