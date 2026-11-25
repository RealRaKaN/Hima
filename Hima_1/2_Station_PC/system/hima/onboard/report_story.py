"""The facts of the narrative landing report.

The aim: "the report is an explanation of the flight, not information and numbers only. 'Group 5 is 700 m
away, found 7 with it' is not a report, it is information." The groups table stays in the station dashboard; the
report tells the flight the way a ranger's patrol report does (SMART patrol reports, drone survey papers): a short
summary, what was found along the way in time order, each incident explained, and a closing with the limits.

story(summary) turns the summary's "detail" (report_facts) into the facts of each part of that report. It is a pure
function of the summary, so the station can show the same incident titles the model saw. Each part is
{"facts": what the writer and the judge see (plain words, rounded numbers), "check": what the fixed gate uses}.

  summary    the whole flight: minutes, km flown, usual height, the farthest point, species and where they were,
             the incidents by title, nothing_unusual.
  stages     the flight in time order. Groups (places) one after another; calm groups next to each other in the same
             direction are one stage ("4 calm places between about 150 m and 650 m"), any other group is a stage of
             its own. "leg" is outbound before the drone's farthest point, return after it.
  incidents  what needs attention, in time order: a group with an alert status (onboard/alerts.PRIORITY: severe
             disturbance, suspected injury, pacing alertness, possible hidden young), and every vehicle. A vehicle
             within [behaviour].vehicle_near_herd_m of a group belongs to that group's incident. Each incident has
             follow-up topics: the report may recommend only these, and only in incidents (the no-advice rule holds everywhere else).
  closing    calm or not, and the limits: counts are minimums, animals under cover may be missed.

Numbers: distances are rounded (about_m: 10 m under 100 m, 50 m under 1 km, 100 m above), minutes to whole minutes,
counts are "at least N" (report_facts seen_together). Keys keep their unit (_m, _km, _min) for the gate.
"""
from __future__ import annotations

from typing import Any

from hima.onboard.alerts import PRIORITY
from hima.onboard.report_facts import SPECIES_WORDS

NOTABLE = ("RAPID_CORRIDOR_TRANSIT", "INTERMITTENT_OCCLUSION")       # told in the flight, not incidents
MEANING = {
    "SEVERE_DRONE_DISTURBANCE": "animals ran (severe disturbance)",
    "SUSPECTED_INJURY_OR_ILLNESS": "an animal stayed alone and still for a long time (suspected injury or illness)",
    "POSSIBLE_HIDDEN_YOUNG": "a small animal lay alone and still (possibly a young one left hidden by its mother)",
    "PROXIMITY_PACING_ALERTNESS": "an animal stayed alert with the drone close for much of the time",
    "RAPID_CORRIDOR_TRANSIT": "an animal crossed quickly on its own, away from the others; it was not running from the drone",
    "INTERMITTENT_OCCLUSION": "animals went in and out of cover (under trees) at times",
}
TITLE_WORDS = {"Arabian oryx": "oryx", "reem gazelle": "gazelle"}


def about_m(d: float | None) -> int | None:
    if d is None:
        return None
    step = 10 if d < 100 else 50 if d < 1000 else 100
    return int(round(d / step) * step)


def _min(x: float | None) -> int | None:
    return None if x is None else int(round(x))


def species_word(sp: str) -> str:
    return SPECIES_WORDS.get(sp, sp)


def _at_least(g: dict[str, Any]) -> dict[str, int]:
    tog = g.get("seen_together") or {}
    order = sorted(g["by_species"], key=lambda sp: (-tog.get(sp, 1), sp))
    return {species_word(sp): max(1, tog.get(sp, 1)) for sp in order}


def herd_words(at_least: dict[str, int]) -> str:
    """'a mixed herd of at least 4 reem gazelle and 3 Arabian oryx', 'a herd of at least 5 Arabian oryx',
    'a single Arabian oryx' (one seen at a time)."""
    items = list(at_least.items())
    if len(items) == 1 and items[0][1] == 1:
        return f"a single {items[0][0]}"
    parts = [f"{n} {sp}" for sp, n in items]
    return ("a mixed herd of at least " if len(items) > 1 else "a herd of at least ") + " and ".join(parts)


def _place(dist_m: int | None, direction: str | None) -> str:
    if dist_m is None or direction is None:
        return "at a place that is not known"
    if direction == "at the launch point" or dist_m == 0:
        return "at the launch point"
    return f"about {dist_m} m {direction} of take-off" if dist_m < 1000 else \
        f"about {dist_m / 1000:.1f} km {direction} of take-off"


def _quiet(g: dict[str, Any]) -> bool:
    return all(st == "BASELINE" for st in g["statuses"])


def _follow_ups(statuses: set[str], cause: str | None, vehicle: dict[str, Any] | None) -> list[str]:
    out = []
    if vehicle is not None:
        out.append(f"check whether the {vehicle['kind']} was allowed in the reserve at that place and time")
    if "SEVERE_DRONE_DISTURBANCE" in statuses:
        out.append("look at this herd again on a later flight" if cause == "a vehicle" else
                   "fly the drone higher or farther from this herd on the next flight")
    if "SUSPECTED_INJURY_OR_ILLNESS" in statuses:
        out.append("send a ranger to check the animal")
    if "POSSIBLE_HIDDEN_YOUNG" in statuses:
        out.append("avoid low passes over this place")
    if "PROXIMITY_PACING_ALERTNESS" in statuses:
        out.append("fly the drone farther from this herd on the next flight")
    return list(dict.fromkeys(out))


def _title(statuses: set[str], at_least: dict[str, int] | None, vehicle: dict[str, Any] | None,
           top_species: list[str] | None = None) -> str:
    """A short name for an incident, made by code: 'Truck in a mixed herd', 'Oryx alert with the drone close'."""
    sp = None if not at_least else " and ".join(TITLE_WORDS.get(s, s) for s in at_least)
    herd = None if at_least is None else ("a mixed herd" if len(at_least) > 1 else
                                          f"an {sp} herd" if sp[0] in "aeiou" else f"a {sp} herd")
    if vehicle is not None:
        kind = vehicle["kind"][0].upper() + vehicle["kind"][1:]
        if herd is None:
            return f"{kind} away from the herds"
        return f"{kind} {'in' if vehicle['inside'] else 'near'} {herd}"
    top = min(statuses, key=lambda s: PRIORITY.get(s, 9))
    if top_species:                                    # the species of the tracks with that status
        sp = " and ".join(TITLE_WORDS.get(species_word(x), x) for x in top_species)
    return {"SEVERE_DRONE_DISTURBANCE": f"Severe disturbance in {herd}",
            "SUSPECTED_INJURY_OR_ILLNESS": f"Suspected injured {sp}",
            "POSSIBLE_HIDDEN_YOUNG": f"Possible hidden young {sp}",
            "PROXIMITY_PACING_ALERTNESS": f"{sp[0].upper()}{sp[1:]} alert with the drone close"}[top]


def story(summary: dict[str, Any]) -> dict[str, Any]:
    d = summary["detail"]
    groups = d["groups"]
    route = d.get("route") or {}
    near_m = d.get("vehicle_near_herd_m")
    close_m = d.get("vehicle_close_m") or 0.0
    far_min = route.get("farthest_min")
    present = sorted({a["species"] for a in summary["animals"]})

    # -- incidents: groups with an alert status, and vehicles (in the group's incident when near it) --------------
    veh_of: dict[str, list[dict[str, Any]]] = {}
    lone_veh: list[dict[str, Any]] = []
    for v in d.get("vehicles") or []:
        if v["nearest_group"] is not None and near_m is not None and v["distance_to_group_m"] <= near_m:
            veh_of.setdefault(v["nearest_group"], []).append(v)
        else:
            lone_veh.append(v)
    raw_inc: list[tuple[float, str | None, dict[str, Any], dict[str, Any]]] = []
    causes: dict[str, str] = {}
    for a in d.get("alert_animals", {}).values():
        if a.get("cause") and causes.get(a["group"]) != "vehicle":      # the flight's own alert said vehicle: it wins
            causes[a["group"]] = a["cause"]
    for g in groups:
        alert = {st for st in g["statuses"] if st in PRIORITY}
        vs = veh_of.get(g["group"], [])
        if not alert and not vs:
            continue
        v = vs[0] if vs else None
        veh = None if v is None else {"kind": v["kind"], "at_min": _min(v["seen_min"]), "sightings": len(vs),
                                      "inside": v["distance_to_group_m"] <= close_m}
        if veh is not None:
            veh["where"] = "inside the herd" if veh["inside"] else f"about {about_m(v['distance_to_group_m'])} m from it"
            if not veh["inside"]:
                veh["distance_to_herd_m"] = about_m(v["distance_to_group_m"])
        cause = {"vehicle": "a vehicle", "drone": "the drone"}.get(causes.get(g["group"], ""), None) if alert else None
        at_least = _at_least(g)
        what = [MEANING[st] for st in sorted(alert, key=lambda s: PRIORITY[s])] or             ["no animal in the herd showed an alert status"]
        top = min(alert, key=lambda s: PRIORITY[s]) if alert else None
        top_sp = sorted({a["species"] for a in summary["animals"]
                         if a["track_id"] in g["track_ids"] and a["status"] == top}) if top else None
        facts = {"title": _title(alert, at_least, veh, top_sp), "at_min": _min(v["seen_min"]) if v else
                 _min(g["first_seen_min"]), "from_min": _min(g["first_seen_min"]), "to_min": _min(g["last_seen_min"]),
                 "distance_m": about_m(g["distance_from_launch_m"]), "direction": g["direction_from_launch"],
                 "place": _place(about_m(g["distance_from_launch_m"]), g["direction_from_launch"]),
                 "herd": herd_words(at_least), "at_least": at_least, "what_happened": what, "cause": cause,
                 "vehicle": veh, "follow_up_topics": _follow_ups(alert, cause, veh)}
        chk = {"statuses": sorted(alert), "species": sorted(g["by_species"]),
               "vehicle": veh is not None or cause == "a vehicle"}
        raw_inc.append((v["seen_min"] if v else g["first_seen_min"], g["group"], facts, chk))
    for v in lone_veh:
        veh = {"kind": v["kind"], "at_min": _min(v["seen_min"]), "sightings": 1, "inside": False}
        facts = {"title": _title(set(), None, veh), "at_min": _min(v["seen_min"]),
                 "distance_m": about_m(v.get("distance_from_launch_m")), "direction": v.get("direction_from_launch"),
                 "place": _place(about_m(v.get("distance_from_launch_m")), v.get("direction_from_launch")),
                 "nearest_herd": None, "vehicle": veh, "follow_up_topics": _follow_ups(set(), None, veh)}
        if v["nearest_group"] is not None:
            g = next(x for x in groups if x["group"] == v["nearest_group"])
            facts["nearest_herd"] = herd_words(_at_least(g))
            facts["distance_to_nearest_herd_m"] = about_m(v["distance_to_group_m"])
        raw_inc.append((v["seen_min"], None, facts, {"statuses": [], "species": sorted(g["by_species"]) if
                                                     v["nearest_group"] is not None else [], "vehicle": True}))
    raw_inc.sort(key=lambda r: r[0])
    incidents: list[dict[str, Any]] = []
    group_inc: dict[str, dict[str, Any]] = {}          # which incident a group's stage points to
    for n, (_, gname, facts, chk) in enumerate(raw_inc, 1):
        incidents.append({"facts": {"incident": f"I{n}", **facts}, "check": chk})
        if gname is not None:
            group_inc[gname] = incidents[-1]

    # -- stages: the flight in time order --------------------------------------------------------------------------
    stages: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def leg(g: dict[str, Any]) -> str:
        return "outbound" if far_min is None or g["first_seen_min"] <= far_min else "return"

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            single(run[0])
        else:
            sp = sorted({s for g in run for s in g["by_species"]})
            largest = max(n for g in run for n in _at_least(g).values())
            facts = {"leg": leg(run[0]), "from_min": _min(run[0]["first_seen_min"]),
                     "to_min": _min(max(g["last_seen_min"] for g in run)), "calm_places": len(run),
                     "direction": run[0]["direction_from_launch"],
                     "from_m": about_m(min(g["distance_from_launch_m"] for g in run)),
                     "to_m": about_m(max(g["distance_from_launch_m"] for g in run)),
                     "species": [species_word(s) for s in sp], "largest_at_least": largest,
                     "what_happened": ["all calm: no animal showed an unusual status"]}
            stages.append({"facts": facts, "check": {"statuses": [], "species": sp, "vehicle": False}})
        run.clear()

    def single(g: dict[str, Any]) -> None:
        notable = [st for st in g["statuses"] if st in NOTABLE]
        inc = group_inc.get(g["group"])
        what = [MEANING[st] for st in notable] or (["all calm: no animal showed an unusual status"] if inc is None
                                                   else [])
        facts = {"leg": leg(g), "from_min": _min(g["first_seen_min"]), "to_min": _min(g["last_seen_min"]),
                 "distance_m": about_m(g["distance_from_launch_m"]), "direction": g["direction_from_launch"],
                 "place": _place(about_m(g["distance_from_launch_m"]), g["direction_from_launch"]),
                 "herd": herd_words(_at_least(g)), "at_least": _at_least(g), "what_happened": what}
        chk = {"statuses": sorted(notable), "species": sorted(g["by_species"]), "vehicle": False}
        if inc is not None:
            fi = inc["facts"]
            facts["incident"] = fi["incident"]
            facts["incident_title"] = fi["title"]
            facts["incident_at_min"] = fi["at_min"]
            facts["incident_what"] = ([f"a {fi['vehicle']['kind']} was seen {fi['vehicle']['where']}"]
                                      if fi.get("vehicle") else []) + \
                [w for w in fi["what_happened"] if not w.startswith("no animal")]
            chk["statuses"] = sorted(set(chk["statuses"]) | set(inc["check"]["statuses"]))
            chk["vehicle"] = inc["check"]["vehicle"]
        stages.append({"facts": facts, "check": chk})

    for g in groups:
        if _quiet(g) and g["group"] not in group_inc and run and \
                run[-1]["direction_from_launch"] == g["direction_from_launch"] and leg(run[-1]) == leg(g):
            run.append(g)
            continue
        flush()
        if _quiet(g) and g["group"] not in group_inc:
            run.append(g)
        else:
            single(g)
    flush()
    if not stages:
        stages.append({"facts": {"no_places": True, "tracks_seen": summary["tracks_seen"],
                                 "metrics_known": summary["metrics_known"],
                                 "species": [species_word(s) for s in present]},
                       "check": {"statuses": [], "species": present, "vehicle": False}})
    for n, s in enumerate(stages, 1):
        s["facts"] = {"stage": f"S{n}", **s["facts"]}
        if n == 1:
            s["facts"]["first_after_take_off"] = True
    first_return = next((s for s in stages if s["facts"].get("leg") == "return"), None)
    if first_return is not None:
        first_return["facts"]["first_after_turning_back"] = True

    # -- summary and closing ---------------------------------------------------------------------------------------
    by_sp = []
    for sp in present:
        gs = [g for g in groups if g["by_species"].get(sp)]
        if gs:
            by_sp.append({"species": species_word(sp), "places": len(gs),
                          "largest_at_least": max((g.get("seen_together") or {}).get(sp, 1) for g in gs)})
    fl = d.get("flight") or {}
    all_alert = sorted({st for g in groups for st in g["statuses"] if st in PRIORITY})
    all_notable = sorted({st for g in groups for st in g["statuses"] if st in NOTABLE})
    any_vehicle = bool(d.get("vehicles"))
    summ = {"duration_min": _min(fl.get("duration_min")),
            "distance_flown_km": None if fl.get("distance_flown_m") is None else round(fl["distance_flown_m"] / 1000, 1),
            "altitude_m": route.get("altitude_m"), "farthest_m": about_m(route.get("farthest_m")),
            "farthest_direction": route.get("farthest_direction"),
            "places_with_animals": len(groups), "species": by_sp,
            "incidents": [{"title": i["facts"]["title"], "at_min": i["facts"]["at_min"]} for i in incidents],
            "incidents_count": len(incidents), "nothing_unusual": d["nothing_unusual"],
            "vehicles_known": d["vehicles_known"], "metrics_known": summary["metrics_known"]}
    if not groups:
        summ["tracks_seen"] = summary["tracks_seen"]
    closing = {"incidents_count": len(incidents), "nothing_unusual": d["nothing_unusual"],
               "rest_calm": True, "counts_are_minimums": True,
               "under_cover": "INTERMITTENT_OCCLUSION" in all_notable,
               "vehicles_known": d["vehicles_known"], "metrics_known": summary["metrics_known"]}
    every = sorted(set(all_alert) | set(all_notable))
    return {"summary": {"facts": summ, "check": {"statuses": every, "species": present, "vehicle": any_vehicle}},
            "stages": stages, "incidents": incidents,
            "closing": {"facts": closing, "check": {"statuses": every, "species": present, "vehicle": any_vehicle}}}
