"""Gate: decide whether one LLM reply may be used.

Every reason starts with a code so callers and tests can tell failures apart:
  "llm:"          model unreachable / empty reply
  "json:"         reply is not a JSON object
  "schema:"       does not match advice.schema.json (missing/extra field, type, enum)
  "policy:"       command not allowed for the computed status
  "altitude:"     outside [alt_min_m, alt_max_m], or a descent where the status forbids it
  "consistency:"  INCREASE_ALTITUDE that does not increase the altitude
On rejection the caller uses policy.default_decision().
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from hima.advisor.policy import POLICY
from hima.config import Safety
from hima.contract.io import validate_advice


@dataclass
class GateResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    parsed: dict[str, Any] | None = None


def check(raw: str | None, status: str, current_agl_m: float, safety: Safety) -> GateResult:
    if raw is None or not raw.strip():
        return GateResult(False, ["llm: no reply"])
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return GateResult(False, [f"json: not valid JSON ({e.msg} at char {e.pos})"])
    if not isinstance(obj, dict):
        return GateResult(False, [f"json: expected an object, got {type(obj).__name__}"])

    errors = validate_advice(obj)
    if errors:
        return GateResult(False, [f"schema: {e.path or '<root>'}: {e.message}" for e in errors], None)

    reasons = []
    rule = POLICY[status]
    cmd, alt = obj["proposed_command"], obj["proposed_altitude_m"]
    if cmd not in rule.allowed:
        reasons.append(f"policy: {cmd} is not allowed for {status} (allowed: {', '.join(rule.allowed)})")
    if not safety.alt_min_m <= alt <= safety.alt_max_m:
        reasons.append(f"altitude: {alt} m is outside [{safety.alt_min_m}, {safety.alt_max_m}] m")
    if cmd == "MAINTAIN_OBSERVATION" and alt < safety.observe_min_m:
        reasons.append(f"altitude: {alt} m is below observe_min_m {safety.observe_min_m} m for MAINTAIN_OBSERVATION")
    if rule.must_not_descend and alt < current_agl_m:
        reasons.append(f"altitude: {alt} m is below the current {current_agl_m} m, but {status} forbids descending")
    if cmd == "INCREASE_ALTITUDE" and alt <= current_agl_m:
        reasons.append(f"consistency: INCREASE_ALTITUDE but {alt} m is not above the current {current_agl_m} m")
    return GateResult(not reasons, reasons, obj)
