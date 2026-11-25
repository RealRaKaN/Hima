"""Per-animal status from ratios of visible time (first matching rule wins).

Thresholds come from hima.toml [status]; none are hard-coded here.
"""
from __future__ import annotations

from dataclasses import dataclass

from hima.config import Status

STATUSES = (
    "SEVERE_DRONE_DISTURBANCE",
    "SUSPECTED_INJURY_OR_ILLNESS",
    "POSSIBLE_HIDDEN_YOUNG",
    "RAPID_CORRIDOR_TRANSIT",
    "PROXIMITY_PACING_ALERTNESS",
    "INTERMITTENT_OCCLUSION",
    "BASELINE",
)


@dataclass(frozen=True)
class StatusInputs:
    metrics_valid: bool
    visibility_ratio: float
    stress: float | None = None
    isolation: float | None = None
    resting: float | None = None
    running: float | None = None
    proximity: float | None = None
    still_alone_s: float | None = None     # alone and in one place without a break, up to now
    small_body: bool = False               # clearly smaller than an adult of its species ([young])
    hidden_s: float | None = None          # time hidden under cover (behavior.hidden_spans)


def classify_status(x: StatusInputs, th: Status, still_min_s: float | None = None,
                    hide_min_s: float | None = None) -> str:
    """still_min_s: [injury].stage1_s. An animal alone and still that long is a suspected injury even when its
    ratios over the whole track are not (it walked earlier). The ratios alone can also give the status; the live
    alert then still waits for stage1_s (onboard/alerts.py).
    A small animal (young of the year) that would be called injured is POSSIBLE_HIDDEN_YOUNG instead:
    gazelle fawns and oryx calves lie alone and still for hours while the mother is away.
    hide_min_s: [hiding].min_s. With camera parameters INTERMITTENT_OCCLUSION means "hidden under cover at least
    once": the share of seen time (r_vis) also fell for an animal the detector missed in the open."""
    if x.metrics_valid:
        if x.stress >= th.r_stress:
            return "SEVERE_DRONE_DISTURBANCE"
        injured = (x.isolation >= th.r_iso and x.resting >= th.r_rest) or (
            still_min_s is not None and x.still_alone_s is not None and x.still_alone_s >= still_min_s)
        if injured:
            return "POSSIBLE_HIDDEN_YOUNG" if x.small_body else "SUSPECTED_INJURY_OR_ILLNESS"
        if x.isolation >= th.r_iso_transit and x.running >= th.r_run and x.stress == 0:
            return "RAPID_CORRIDOR_TRANSIT"
        if x.proximity >= th.r_prox:
            return "PROXIMITY_PACING_ALERTNESS"
        if hide_min_s is not None and x.hidden_s is not None:
            return "INTERMITTENT_OCCLUSION" if x.hidden_s >= hide_min_s else "BASELINE"
    # Without camera parameters only the visibility rule can be evaluated.
    if x.visibility_ratio < th.r_vis:
        return "INTERMITTENT_OCCLUSION"
    return "BASELINE"
