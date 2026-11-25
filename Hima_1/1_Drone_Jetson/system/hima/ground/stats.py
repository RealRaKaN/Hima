"""Cross-mission statistics from the ground index. Computes from index rows only.

Explicit limit: we cannot tell whether an animal seen in two missions (or
under two tracker ids in one mission) is the same individual; that needs re-identification.
So every count here is TRACKS (tracker ids) per mission, never a herd census.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

CENSUS_NOTE = ("Counts are tracks (tracker ids) per mission, not individual animals: the same animal "
               "in two missions, or under two ids in one mission, is counted twice.")


def _add(total: Counter[str], part: dict[str, int]) -> None:
    total.update(part)


def mission_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    per_species: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    events: Counter[str] = Counter()
    uncertain: Counter[str] = Counter()
    days: dict[str, dict[str, Any]] = {}
    for e in entries:
        _add(per_species, e["tracks_by_species"])
        _add(statuses, e["statuses"])
        _add(events, e["events"])
        _add(uncertain, e["uncertain"])
        d = days.setdefault(e["flight_date"], {"missions": 0, "tracks": 0, "tracks_by_species": Counter(),
                                               "alert_tracks": 0, "duration_s": 0.0})
        d["missions"] += 1
        d["tracks"] += e["tracks_seen"]
        d["tracks_by_species"].update(e["tracks_by_species"])
        d["alert_tracks"] += len(e["alert_track_ids"])
        d["duration_s"] = round(d["duration_s"] + e["duration_s"], 1)
    return {
        "note": CENSUS_NOTE,
        "missions": len(entries),
        "flight_s": round(sum(e["duration_s"] for e in entries), 1),
        "tracks": sum(e["tracks_seen"] for e in entries),
        "tracks_by_species": dict(sorted(per_species.items())),
        "alert_tracks": sum(len(e["alert_track_ids"]) for e in entries),
        "statuses": dict(sorted(statuses.items())),
        "events": dict(sorted(events.items())),
        "uncertain": dict(sorted(uncertain.items())),
        "uncertain_with_image": sum(e["uncertain_with_image"] for e in entries),
        "report_sources": dict(sorted(Counter(e["report_source"] for e in entries).items())),
        "by_day": {day: {**d, "tracks_by_species": dict(sorted(d["tracks_by_species"].items()))}
                   for day, d in sorted(days.items())},
        "by_mission": [{"mission_id": e["mission_id"], "flight_date": e["flight_date"], "source": e["source"],
                        "duration_s": e["duration_s"], "tracks": e["tracks_seen"],
                        "tracks_by_species": e["tracks_by_species"], "alert_tracks": len(e["alert_track_ids"]),
                        "uncertain": sum(e["uncertain"].values()), "report_source": e["report_source"]}
                       for e in entries],
    }
