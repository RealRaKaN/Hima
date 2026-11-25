"""Build the LLM prompt from ONE animal's computed summary.

The model never sees raw frames, pixel boxes or ground_truth: only ~15 numbers that
Python already computed, the computed status, and the allowed choices.
"""
from __future__ import annotations

import json
from typing import Any

from hima.advisor.policy import POLICY
from hima.config import Safety
from hima.contract.io import ADVICE_SCHEMA, load_schema

_STRIP = ("$schema", "$id", "title")


def format_schema(strip_meta: bool = True) -> dict[str, Any]:
    """advice.schema.json as sent in Ollama's `format` field."""
    s = dict(load_schema(ADVICE_SCHEMA))
    if strip_meta:
        for k in _STRIP:
            s.pop(k, None)
    return s


def summarize(animal: dict[str, Any], drone_agl_now_m: float) -> dict[str, Any]:
    ev: dict[str, dict[str, float]] = {}
    for e in animal["events"]:
        d = ev.setdefault(e["type"], {"count": 0, "total_s": 0.0})
        d["count"] += 1
        d["total_s"] = round(d["total_s"] + e["duration_s"], 1)
    return {
        "track_id": animal["track_id"],
        "species": animal["species"],
        "computed_status": animal["status"],
        "visible_s": animal["visible_s"],
        "visibility_ratio": animal["visibility_ratio"],
        "isolation_ratio": animal["isolation_ratio"],
        "time_ratios": animal["ratios"],
        "max_ground_speed_mps": animal["max_ground_speed_mps"],
        "mean_ground_speed_mps": animal["mean_ground_speed_mps"],
        "distance_travelled_m": animal["distance_travelled_m"],
        "min_distance_to_drone_m": animal["min_slant_distance_to_drone_m"],
        "events": ev,
        "drone_altitude_now_m": drone_agl_now_m,
        "metrics_valid": animal["metrics_valid"],
    }


def build_prompt(animal: dict[str, Any], drone_agl_now_m: float, safety: Safety) -> str:
    rule = POLICY[animal["status"]]
    summary = summarize(animal, drone_agl_now_m)
    alt_rule = (f"- proposed_altitude_m must be between {safety.alt_min_m} and {safety.alt_max_m} metres.")
    if rule.must_not_descend:
        alt_rule += f"\n- The animal is disturbed: proposed_altitude_m must NOT be below the current {drone_agl_now_m} m."
    return f"""You advise a wildlife-monitoring drone pilot. The numbers below were already computed
by software from the drone's camera; trust them and do not invent new numbers.

Animal summary (JSON):
{json.dumps(summary, ensure_ascii=False, indent=1)}

Rules:
- computed_status is final; explain it, do not change it.
- proposed_command must be exactly one of: {", ".join(rule.allowed)}.
{alt_rule}
- If you choose INCREASE_ALTITUDE, proposed_altitude_m must be above {drone_agl_now_m}.
- Write every text field in English only.
- interpretation: one or two short sentences explaining computed_status.
- Each text field at most 400 characters.

Reply with ONE JSON object only, matching this JSON schema:
{json.dumps(format_schema(), ensure_ascii=False)}
"""
