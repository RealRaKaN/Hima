"""Build the session's "advice" section: one LLM proposal per animal, each passed
through the gate. Rejected or missing proposals fall back to the policy default."""
from __future__ import annotations

import bisect
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable

from hima.advisor import gate, ollama_client
from hima.advisor.policy import default_decision
from hima.advisor.prompt import build_prompt, format_schema
from hima.config import Config

GenerateFn = Callable[[str, dict[str, Any]], ollama_client.LlmReply]


def drone_agl_at(frames: list[dict[str, Any]], t: float) -> float:
    """Drone altitude at the last frame with time <= t."""
    ts = [f["t"] for f in frames]
    i = max(0, bisect.bisect_right(ts, t) - 1)
    return frames[i]["drone"]["agl_m"]


def advise(doc: dict[str, Any], cfg: Config, *, generate_fn: GenerateFn | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    if "analysis" not in doc:
        raise ValueError("session has no 'analysis' section; run analyze first")
    if not doc["meta"]["camera"]["params_known"]:
        # The drone altitude in such a file is a placeholder; flight advice built on it is unsafe.
        raise ValueError("session has no camera parameters / drone telemetry (params_known = false); "
                         "flight advice needs the real drone altitude")
    if generate_fn is None:
        def generate_fn(prompt, schema):
            return ollama_client.generate(prompt, schema, cfg.llm)

    schema = format_schema()
    down: str | None = None           # after the first connection failure, stop calling
    out = []
    for a in doc["analysis"]["animals"]:
        agl = drone_agl_at(doc["frames"], a["last_seen_s"])
        prompt = build_prompt(a, agl, cfg.safety)
        raw = latency = None
        if down is None:
            try:
                rep = generate_fn(prompt, schema)
                raw, latency = rep.text, round(rep.latency_s, 3)
            except ollama_client.LlmUnavailable as e:
                down = str(e)
        g = gate.check(raw, a["status"], agl, cfg.safety)
        reasons = [f"llm: unavailable: {down}"] if raw is None and down else g.reasons
        if g.accepted:
            final = {"proposed_command": g.parsed["proposed_command"],
                     "proposed_altitude_m": g.parsed["proposed_altitude_m"], "source": "llm"}
        else:
            final = default_decision(a["status"], agl, cfg.safety)
        out.append({
            "track_id": a["track_id"],
            "llm_raw": raw,
            "parsed": g.parsed,
            "gate": {"accepted": g.accepted, "reasons": reasons},
            "final": final,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "latency_s": latency,
        })
    now = now or datetime.now(timezone.utc)
    return {"model": cfg.llm.model, "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "animals": out}
