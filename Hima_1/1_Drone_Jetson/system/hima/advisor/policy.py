"""Policy table: for each computed status, which drone commands are allowed, which one is
the safe default, and whether the drone may stay low / descend.

Altitude limits come from hima.toml [safety]; nothing numeric is hard-coded here.
"""
from __future__ import annotations

from dataclasses import dataclass

from hima.analytics.status import STATUSES
from hima.config import Safety


@dataclass(frozen=True)
class Rule:
    allowed: tuple[str, ...]
    default: str
    must_not_descend: bool       # proposed altitude must be >= current altitude
    prefer_default_alt: bool     # default altitude = max(current, alt_default_m)


POLICY: dict[str, Rule] = {
    "SEVERE_DRONE_DISTURBANCE": Rule(
        ("RETREAT", "INCREASE_ALTITUDE", "INCREASE_STANDOFF"), "RETREAT", True, True),
    "SUSPECTED_INJURY_OR_ILLNESS": Rule(
        ("MAINTAIN_OBSERVATION", "INSPECT_HIGH_ALTITUDE_ZOOM", "WIDEN_ORBIT"), "MAINTAIN_OBSERVATION", False, False),
    # A young lying out alone waits for its mother; a drone overhead may keep her away. Move off, never lower.
    "POSSIBLE_HIDDEN_YOUNG": Rule(
        ("INCREASE_ALTITUDE", "INCREASE_STANDOFF", "RETREAT", "WIDEN_ORBIT"), "INCREASE_ALTITUDE", True, True),
    "RAPID_CORRIDOR_TRANSIT": Rule(
        ("MAINTAIN_OBSERVATION", "WIDEN_ORBIT", "INCREASE_STANDOFF"), "MAINTAIN_OBSERVATION", False, False),
    "PROXIMITY_PACING_ALERTNESS": Rule(
        ("INCREASE_ALTITUDE", "INCREASE_STANDOFF", "RETREAT", "WIDEN_ORBIT"), "INCREASE_ALTITUDE", True, True),
    "INTERMITTENT_OCCLUSION": Rule(
        ("REACQUIRE_TARGET", "WIDEN_ORBIT", "MAINTAIN_OBSERVATION"), "REACQUIRE_TARGET", False, False),
    "BASELINE": Rule(
        ("MAINTAIN_OBSERVATION", "WIDEN_ORBIT"), "MAINTAIN_OBSERVATION", False, False),
}

assert set(POLICY) == set(STATUSES), "policy must cover every status"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def default_altitude(status: str, current_agl_m: float, safety: Safety) -> float:
    rule = POLICY[status]
    target = max(current_agl_m, safety.alt_default_m) if rule.prefer_default_alt else current_agl_m
    if rule.default == "MAINTAIN_OBSERVATION":
        target = max(target, safety.observe_min_m)     # Watching an animal never below observe_min_m
    return clamp(target, safety.alt_min_m, safety.alt_max_m)


def default_decision(status: str, current_agl_m: float, safety: Safety) -> dict:
    cmd = POLICY[status].default
    alt = default_altitude(status, current_agl_m, safety)
    # Already at/above the default altitude: "climb" would be a lie, so move away horizontally.
    if cmd == "INCREASE_ALTITUDE" and alt <= current_agl_m:
        cmd = "INCREASE_STANDOFF"
    assert cmd in POLICY[status].allowed
    return {"proposed_command": cmd, "proposed_altitude_m": alt, "source": "policy_default"}
