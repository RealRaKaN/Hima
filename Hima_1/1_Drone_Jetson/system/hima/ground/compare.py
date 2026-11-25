"""Compare archived flights or flight days side by side, and filter the archive by date (ground
dashboard). Pure functions over ground index rows: every number is mission_stats() of the selected
rows, so the Compare page and the Archive page can never disagree.

Every comparison carries its limits as notes, shown with the numbers:
- counts are tracks (tracker ids) per mission, not individual animals (CENSUS_NOTE);
- flights of very different length: read "Tracks per flight hour", not the raw counts (the rate is
  left empty below MIN_RATE_S of flight, where one track would look like a large rate);
- a mission without camera telemetry has no ground speeds, so speed-based statuses cannot appear
  in it and its status counts are not comparable with a mission that has telemetry;
- synthetic and video missions mixed.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from hima.analytics.status import STATUSES
from hima.ground.stats import CENSUS_NOTE, mission_stats

BY = ("flight", "day")
LONG_RATIO = 2.0              # flight times differing by more than this: point to the per-hour row
MIN_RATE_S = 300.0            # no per-hour rate below 5 min of flight: 1 track in a 5 s clip is not "720 per hour"
NOTE_TELEMETRY = ("Telemetry differs between the selections: without camera parameters there are no ground "
                  "speeds, so speed-based statuses cannot appear and status counts are not comparable.")
NOTE_SOURCES = "The selections mix synthetic and video missions."
NOTE_LENGTH = "Flight times differ a lot: compare 'Tracks per flight hour', not the raw counts."


def in_dates(entries: list[dict[str, Any]], start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    """Index rows whose flight_date (YYYY-MM-DD) lies in [start, end], both inclusive; None = open."""
    return [e for e in entries
            if (start is None or e["flight_date"] >= start) and (end is None or e["flight_date"] <= end)]


def choices(entries: list[dict[str, Any]], by: str) -> list[str]:
    """What can be selected: mission ids in index order (date, id), or the flight days."""
    if by == "flight":
        return [e["mission_id"] for e in entries]
    if by == "day":
        return sorted({e["flight_date"] for e in entries})
    raise ValueError(f"by must be one of {BY}, got {by!r}")


def label(entries: list[dict[str, Any]], by: str, key: str) -> str:
    if by == "day":
        return key
    e = next(e for e in entries if e["mission_id"] == key)
    return f"{e['flight_date']} · {key}"


def _subset(entries: list[dict[str, Any]], by: str, key: str) -> list[dict[str, Any]]:
    return [e for e in entries if (e["mission_id"] if by == "flight" else e["flight_date"]) == key]


def status_order(names: Iterable[str]) -> list[str]:
    """Statuses in rule order (most serious first), unknown names last."""
    names = set(names)
    return [s for s in STATUSES if s in names] + sorted(names - set(STATUSES))


def _yes_no(flags: Iterable[bool]) -> str:
    s = {bool(f) for f in flags}
    return "yes" if s == {True} else "no" if s == {False} else "mixed"


def _per_hour(s: dict[str, Any]) -> float | None:
    return round(s["tracks"] / (s["flight_s"] / 3600), 1) if s["flight_s"] >= MIN_RATE_S else None


def _share(s: dict[str, Any]) -> float | None:
    return round(100 * s["alert_tracks"] / s["tracks"], 1) if s["tracks"] else None


def compare(entries: list[dict[str, Any]], by: str, keys: list[str]) -> dict[str, Any]:
    """Side-by-side numbers for the selected flights (by="flight", keys = mission ids) or days
    (by="day", keys = YYYY-MM-DD). With exactly two keys, "change" = second minus first."""
    if by not in BY:
        raise ValueError(f"by must be one of {BY}, got {by!r}")
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("give one or more different keys")
    groups: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    for k in keys:
        sub = _subset(entries, by, k)
        if not sub:
            raise KeyError(f"no archived mission for {by} {k!r}")
        groups[k] = (sub, mission_stats(sub))
    stats = {k: s for k, (_, s) in groups.items()}
    species = sorted({sp for s in stats.values() for sp in s["tracks_by_species"]})
    statuses = status_order(st for s in stats.values() for st in s["statuses"])
    events = sorted({ev for s in stats.values() for ev in s["events"]})
    rows: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
        ("Flights", lambda s: s["missions"]),
        ("Flight time (min)", lambda s: round(s["flight_s"] / 60, 1)),
        ("Tracks", lambda s: s["tracks"]),
        ("Tracks per flight hour", _per_hour),
        *[(f"Tracks: {sp}", lambda s, sp=sp: s["tracks_by_species"].get(sp, 0)) for sp in species],
        ("Tracks in alert", lambda s: s["alert_tracks"]),
        ("Share in alert (%)", _share),
        *[(f"Status: {st}", lambda s, st=st: s["statuses"].get(st, 0)) for st in statuses],
        *[(f"Event: {ev}", lambda s, ev=ev: s["events"].get(ev, 0)) for ev in events],
        ("Uncertain detections", lambda s: sum(s["uncertain"].values())),
        ("Uncertain with image", lambda s: s["uncertain_with_image"]),
    ]
    table = [{"metric": name, **{k: f(stats[k]) for k in keys}} for name, f in rows]
    if len(keys) == 2:
        a, b = keys
        for r in table:
            r["change"] = None if r[a] is None or r[b] is None else round(r[b] - r[a], 1)
    context = {k: {"dates": sorted({e["flight_date"] for e in sub}),
                   "sources": sorted({e["source"] for e in sub}),
                   "telemetry": _yes_no(e["metrics_known"] for e in sub),
                   "reports": sorted({e["report_source"] for e in sub}),
                   "headline": sub[0]["report_headline"] if by == "flight" else None}
               for k, (sub, _) in groups.items()}
    notes = [CENSUS_NOTE]
    tele = {c["telemetry"] for c in context.values()}
    if len(tele) > 1 or "mixed" in tele:
        notes.append(NOTE_TELEMETRY)
    if len({src for c in context.values() for src in c["sources"]}) > 1:
        notes.append(NOTE_SOURCES)
    flight = [stats[k]["flight_s"] for k in keys]
    if len(keys) > 1 and min(flight) > 0 and max(flight) / min(flight) > LONG_RATIO:
        notes.append(NOTE_LENGTH)
    return {
        "by": by, "keys": list(keys), "table": table, "context": context, "notes": notes,
        "status_order": statuses,
        "species": [{"selection": k, "species": sp, "tracks": stats[k]["tracks_by_species"].get(sp, 0)}
                    for k in keys for sp in species],
        "statuses": [{"selection": k, "status": st, "tracks": stats[k]["statuses"].get(st, 0)}
                     for k in keys for st in statuses],
        "events": [{"selection": k, "event": ev, "count": stats[k]["events"].get(ev, 0)}
                   for k in keys for ev in events],
        "per_hour": [{"selection": k, "tracks_per_hour": _per_hour(stats[k])} for k in keys],
    }


def trend(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Rows per flight day for the Archive page charts, from mission_stats()["by_day"]."""
    by_day = mission_stats(entries)["by_day"]
    species = sorted({sp for d in by_day.values() for sp in d["tracks_by_species"]})
    return {
        "species": [{"date": day, "species": sp, "tracks": d["tracks_by_species"].get(sp, 0)}
                    for day, d in by_day.items() for sp in species],
        "days": [{"date": day, "missions": d["missions"], "flight_min": round(d["duration_s"] / 60, 1),
                  "tracks": d["tracks"], "alert_tracks": d["alert_tracks"]} for day, d in by_day.items()],
    }
