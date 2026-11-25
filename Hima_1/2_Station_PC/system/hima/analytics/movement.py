"""Where each animal was found, after landing.

No animal paths, on the map or in the package; one point per animal, where it was
registered (seen identity.confirm_s in a row), with its GPS position when the autopilot's HOME is known, plus the
drone's own path (the session frames). The numbers per animal are copied from the analysis, so there is one source
for each. Only with known camera parameters (metric positions); otherwise {"metric": false} and nothing else.
"""
from __future__ import annotations

from typing import Any


def movement_summary(doc: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    if not doc["meta"]["camera"]["params_known"]:
        return {"metric": False, "tracks": [], "note": "camera parameters unknown: no ground positions"}
    tracks = [{"track_id": a["track_id"], "species": a["species"], "status": a["status"],
               "sighting": a.get("sighting"), "distance_travelled_m": a.get("distance_travelled_m"),
               "mean_ground_speed_mps": a.get("mean_ground_speed_mps"), "ethogram_s": a.get("ethogram_s")}
              for a in analysis["animals"]]
    return {"metric": True, "tracks": tracks,
            "note": "one point per animal: where it was found (box centre projected to flat ground when it had "
                    "been seen 1 s in a row); GPS from the autopilot's HOME"}
