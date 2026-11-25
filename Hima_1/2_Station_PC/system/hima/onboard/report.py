"""Short mission report at landing. The LLM proposes, the fact gate decides (as for the advice).

The model sees ONLY mission_summary(): computed statuses, counts and event counts. No
frames, no pixel boxes, no ground_truth. Everything sent to and returned by the model is
English. The report uses its own model ([report].model): it is written after landing, so it
may be slower than anything that runs in flight.

The report is an ANALYSIS of the flight only: a headline and one note per finding. It gives no
recommendations or ranger actions (urgent warnings are the live
alerts in flight, onboard/alerts.py, without any LLM). The one exception is the follow-up of an
incident in the narrative report (see "Narrative" below).

Fact gate (headline and notes); every reason starts with a code. Codes marked * concern one
sentence: that sentence alone is replaced (see "Repair"); the others reject the whole reply.
  "llm:"            model unreachable / empty reply
  "json:"           not a JSON object
  "schema:"         does not match report.schema.json
  "unknown_track:"  a finding names a track that is not in the mission
  "duplicate:"      a track appears in two findings
  "wrong_status:"   a finding's status differs from the computed one
  "missing_alert:"  an animal in an alert status has no finding (the 20 most urgent when there are more)
  "too_many:"       more findings than max(report.max_findings, number of alert animals), never over 20
  "length:"         headline + notes together are longer than report.max_chars
* "species:"        a finding's note names the other species (oryx <-> gazelle), or the
                    headline names a species that was not seen in the mission
* "language:"       a letter outside the Latin script (the report must be English)
* "number:"         headline: a number that is not a value of the summary (or that value
                    rounded); a number followed by "min..." must be a *_s value / 60; the mission
                    id is ignored. Notes: each number must be THAT animal's value in the stated
                    unit (note_number_errors).
* "duration:"       a note's duration ("for / lasted / took N seconds|minutes") must be that
                    track's visible_s: it is the only duration a track has ("Isolation event
                    detected for 618 seconds" was last_seen_s; the number check let it through).
* "advice:"         a recommendation word (should, must, recommend...): the report only describes.
Wording is not checked by code: "detected at 166 s" passes. That is the judge's job.

Judge (accuracy over time at landing). BEFORE the fixed gate,
a second LLM ([report].judge_model) checks ONE sentence per call against the facts of that part
only: the headline against mission facts, each note against its own track (judge_parts). The
first design (whole summary + whole report in one call) rejected 4 of 4 correct reports and mixed
animals up. It can only reject; its codes are all *:
* "judge:"          the judge marked a sentence unsupported (its problem sentence follows)
* "judge_invalid:"  the judge reply is unusable (not JSON, not judge.schema.json)
  "judge_llm:"      the judge model is unreachable: nothing was checked, the template is used

Narrative (replaces the older "sightings" / "vehicles" parts): with
link_m (the packager gives [behaviour].herd_link_m) the summary also carries "detail" (onboard/report_facts.py), and
the report explains the flight like a patrol report instead of listing data: headline = a short summary, flight =
one text per stage in time order, incidents = text + follow-up for each, closing. The facts of each part come from
onboard/report_story.py; the codes and checks of these parts are with narrative_errors below. Recommendations are
allowed only in an incident's follow-up, and only its follow_up_topics (the no-advice rule stays for every
other part). A summary without "detail" (old packages) gives the report as before (headline + findings).
  "missing_part:"   a narrative reply without flight / incidents / closing (whole reply)

Repair (replaces an older retry, which fixed 0 of 6 reports and twice made them worse):
every sentence rejected by the judge or by a * code is replaced by the template sentence for the
same part (template_headline / template_note: correct by construction); the rest of the model's
report is kept (source "llm_repaired"). Then the fixed gate checks the repaired report once more
and keeps the last word. Whole-reply errors, or every sentence rejected, give the template.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from hima.advisor import ollama_client
from hima.config import Config
from hima.contract.io import load_schema, schema_errors
from hima.onboard.alerts import PRIORITY
from hima.onboard import report_story
from hima.onboard.report_facts import SPECIES_WORDS, detail as detail_facts

from jsonschema import Draft202012Validator

REPORT_SCHEMA = "report.schema.json"


def schema_max_findings() -> int:
    """The report schema's own cap on findings (20; its first form)."""
    return int(load_schema(REPORT_SCHEMA)["anyOf"][0]["properties"]["findings"]["maxItems"])


def required_alerts(summary: dict[str, Any]) -> list[int]:
    """Alert tracks the report must cover: all of them, or the most urgent ones when there are more than the schema
    allows (in SITL, 21 animals in alert statuses and a cap of 20 stopped the landing before the package).
    alert_track_ids is in priority order, so the cut keeps the urgent ones; the package keeps every animal."""
    return summary["alert_track_ids"][:schema_max_findings()]


def findings_limit(summary: dict[str, Any], cfg: Config) -> int:
    return min(schema_max_findings(), max(cfg.report.max_findings, len(summary["alert_track_ids"])))
JUDGE_SCHEMA = "judge.schema.json"
GenerateFn = Callable[[str, dict[str, Any]], ollama_client.LlmReply]
_SPECIES_WORDS = {"arabian_oryx": re.compile(r"oryx", re.IGNORECASE),
                  "reem_gazelle": re.compile(r"gazelle", re.IGNORECASE)}
_NUM = re.compile(r"(\d+(?:\.\d+)?)(\s*min)?", re.IGNORECASE)


def _r1(x: float | None) -> float | None:
    return None if x is None else round(float(x), 1)


def mission_summary(doc: dict[str, Any], analysis: dict[str, Any], flight_log: dict[str, Any] | None = None,
                    link_m: float | None = None, near_m: float | None = None,
                    close_m: float | None = None) -> dict[str, Any]:
    """The facts the report may use. With link_m: also "detail" (report_facts.detail; flight_log optional; near_m /
    close_m = [behaviour].vehicle_near_herd_m / vehicle_close_m)."""
    frames = doc["frames"]
    animals = sorted(analysis["animals"], key=lambda a: (PRIORITY.get(a["status"], 9), a["track_id"]))
    by_species: dict[str, int] = {}
    for a in animals:
        by_species[a["species"]] = by_species.get(a["species"], 0) + 1
    out = []
    for a in animals:
        ev: dict[str, int] = {}
        for e in a["events"]:
            ev[e["type"]] = ev.get(e["type"], 0) + 1
        out.append({"track_id": a["track_id"], "species": a["species"], "status": a["status"],
                    "visible_s": _r1(a["visible_s"]), "first_seen_s": _r1(a["first_seen_s"]),
                    "last_seen_s": _r1(a["last_seen_s"]), "events": ev,
                    "min_distance_to_drone_m": _r1(a["min_slant_distance_to_drone_m"])})
    return {
        "mission_id": doc["meta"]["mission_id"],
        "source": doc["meta"]["source"],
        "duration_s": _r1(frames[-1]["t"] - frames[0]["t"]),
        "metrics_known": bool(doc["meta"]["camera"]["params_known"]),
        "tracks_seen": len(animals),
        "tracks_by_species": by_species,
        "alert_track_ids": [a["track_id"] for a in animals if a["status"] in PRIORITY],
        "tracks_needing_attention": sum(a["status"] in PRIORITY for a in animals),
        "alerts_listed": min(sum(a["status"] in PRIORITY for a in animals), schema_max_findings()),
        "animals": out,
    } | ({} if link_m is None else {"detail": detail_facts(doc, analysis, flight_log, link_m, near_m, close_m)})


def has_detail(summary: dict[str, Any]) -> bool:
    return "detail" in summary


def llm_schema(summary: dict[str, Any]) -> dict[str, Any]:
    """The schema sent to the writer: the narrative one with detail, else report.schema.json's first form."""
    if has_detail(summary):
        return narrative_schema(story_of(summary))
    s = format_schema()
    return {k: v for k, v in s.items() if k != "anyOf"} | s["anyOf"][0]


def animal_facts(summary: dict[str, Any], tid: int) -> dict[str, Any] | None:
    """One track's facts: its summary row, plus its detail (group, cause, behaviour) when it is an alert animal."""
    a = next((x for x in summary["animals"] if x["track_id"] == tid), None)
    if a is None:
        return None
    extra = summary.get("detail", {}).get("alert_animals", {}).get(str(tid))
    return {**a, **extra} if extra else dict(a)


def head_facts(summary: dict[str, Any]) -> dict[str, Any]:
    head = {k: summary[k] for k in ("mission_id", "duration_s", "metrics_known", "tracks_seen",
                                    "tracks_by_species", "tracks_needing_attention", "alerts_listed",
                                    "alert_track_ids")}
    head["tracks"] = [{"track_id": a["track_id"], "species": a["species"], "status": a["status"]}
                      for a in summary["animals"]]
    return head


def format_schema(name: str = REPORT_SCHEMA) -> dict[str, Any]:
    s = dict(load_schema(name))
    for k in ("$schema", "$id", "title", "description"):
        s.pop(k, None)
    return s


def build_prompt(summary: dict[str, Any], cfg: Config) -> str:
    limit = findings_limit(summary, cfg)
    must = required_alerts(summary)
    if has_detail(summary):
        return _narrative_prompt(summary, cfg)
    return f"""You write the short landing report of a wildlife-monitoring drone mission over Arabian oryx
and reem gazelle. Software already computed everything below; trust it and do not invent anything.

Mission summary (JSON):
{json.dumps(summary, ensure_ascii=False, indent=1)}

Rules:
- headline: one sentence about the whole mission.
- findings: one per animal you mention, at most {limit}. Copy track_id and status EXACTLY from the summary.
- Every one of these track_ids MUST have a finding, first and in this order: {json.dumps(must)}.
- Do not mention any track_id that is not in the summary.
- note: one short sentence per finding. Do not give instructions or recommendations.
- Use only numbers that appear in the summary. Do not compute new numbers.
- tracks_seen counts tracker ids, not individual animals.
{'' if summary['metrics_known'] else '- metrics_known is false: there was no drone telemetry, so distances and speeds are unknown.'}
- All text together at most {cfg.report.max_chars} characters. English only.

Reply with ONE JSON object only, matching this JSON schema:
{json.dumps(llm_schema(summary), ensure_ascii=False)}
"""


_JUDGE_RULES = """The sentence is unsupported only if it:
1. uses a number with a different meaning or unit than in the facts,
2. states something the facts do not give (a cause, behaviour, health detail, place, other animals,
   what will happen),
3. gives an instruction or a recommendation.
Otherwise it is supported, even if it is short, vague or leaves things out. Do not judge style."""

_TRACK_MEANING = """What the facts mean:
- visible_s: how long this track was visible in total, in seconds.
- first_seen_s / last_seen_s: the mission time, in seconds, when this track was first / last seen.
  They are moments, not durations.
- events: how many times each event type was detected. These are counts; no event has a duration.
- min_distance_to_drone_m: the closest distance between the drone and this track, in metres.
- status: computed by software; saying it in plain words is supported.
- group: the group of tracks found in the same place (G1, G2 ...).
- cause: why the animal was disturbed: "vehicle" (a vehicle was seen at that time) or "drone".
- behaviour_s: seconds spent resting, walking and running while visible.
- distance_travelled_m: how far the track moved while visible, in metres; max_speed_mps: its top speed in m/s."""

# Examples use a made-up track / mission, worded differently from the labelled test set.
_TRACK_EXAMPLES = """Examples for a different track with facts {"track_id": 7, "species": "reem_gazelle",
"status": "PROXIMITY_PACING_ALERTNESS", "visible_s": 50.0, "first_seen_s": 10.0, "last_seen_s": 70.0,
"events": {"proximity": 2, "stress": 3}, "min_distance_to_drone_m": 40.0}:
- "Seen for 50 s; 2 proximity events and 3 stress events." -> supported
- "Reacted to the drone and came within 40 metres." -> supported
- "Stress was observed for 3 seconds." -> unsupported: 3 is the number of stress events, not seconds.
- "Its proximity events took 50 seconds." -> unsupported: 50 s is the time visible, not the length of an event.
- "It stayed in view for 70 seconds." -> unsupported: 70 s is when it was last seen, not how long it was visible.
- "It was limping because of a leg wound." -> unsupported: the facts give no cause or wound.
- "Keep the drone higher next time." -> unsupported: it is a recommendation.
For a track with "events": {} and "visible_s": 80.0:
- "No events detected for 80 seconds." -> supported: 80 s is the time visible, and no event happened in it."""

_HEADLINE_MEANING = """What the facts mean:
- tracks_seen and tracks_by_species count tracker ids, NOT individual animals: one animal can get
  several ids. Calling them a number of animals, individuals, gazelles or oryx is unsupported;
  "tracks" or "tracks of oryx" is supported.
- tracks_needing_attention: how many tracks are in an alert status (alert_track_ids).
- alerts_listed: how many of them the report lists (the most urgent, when there are too many to list).
- tracks: the status of each track; saying a status in plain words is supported.
- duration_s: the mission length in seconds."""

_HEADLINE_EXAMPLES = """Examples for a different mission with facts {"mission_id": "m1", "duration_s": 900.0,
"tracks_seen": 5, "tracks_by_species": {"reem_gazelle": 3, "arabian_oryx": 2}, "tracks_needing_attention": 2}:
- "2 of 5 tracks need attention." -> supported
- "Mission m1 saw 5 tracks: 3 reem gazelle tracks and 2 Arabian oryx tracks." -> supported
- "Five animals were counted." -> unsupported: tracks are tracker ids, not animals.
- "Everything was calm during the mission." -> unsupported: 2 tracks need attention.
- "The flight lasted 2 hours." -> unsupported: duration_s is 900 seconds."""


def judge_parts(summary: dict[str, Any], report: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(part, sentence, facts) for each sentence; facts are ONLY what that sentence may use: the
    headline gets mission facts, a note gets its own track (with the whole
    summary, llama mixed animals up and rejected correct sentences). A finding about an
    unknown track gets no judge call; the fixed gate rejects it (unknown_track)."""
    if has_detail(summary):
        return [(part, text, {**facts, "metrics_known": summary["metrics_known"]})
                for part, facts, _ in story_parts(story_of(summary))
                if (text := part_text(report, part)) is not None]
    out = [("headline", report["headline"], head_facts(summary))]
    for f in report["findings"]:
        a = animal_facts(summary, f["track_id"])
        if a is not None:
            out.append((f"track {f['track_id']}", f["note"], {**a, "metrics_known": summary["metrics_known"]}))
    return out


def build_judge_prompt(part: str, sentence: str, facts: dict[str, Any]) -> str:
    kind = part.split(" ", 1)[0]
    rules = _JUDGE_RULES
    if "title" in facts or "stage" in facts or "follow_up_topics" in facts or "rest_calm" in facts or \
            "places_with_animals" in facts:                       # a narrative part
        meaning, examples = _STORY_MEANING[kind], _STORY_EXAMPLES[kind]
        rules = _STORY_RULES.format(follow=" (a follow-up may recommend its follow_up_topics only)"
                                    if kind == "follow-up" else "")
    else:
        meaning, examples = (_HEADLINE_MEANING, _HEADLINE_EXAMPLES) if part == "headline" else \
            (_TRACK_MEANING, _TRACK_EXAMPLES)
    no_tel = "" if facts["metrics_known"] else \
        "\n- metrics_known is false: there was no telemetry, so any distance or speed is unsupported."
    return f"""You check ONE sentence of the landing report of a wildlife-monitoring drone mission over
Arabian oryx and reem gazelle. The facts below were computed by software and are the only truth.
Decide whether the sentence is supported by these facts.

Part: {part}
Facts (JSON):
{json.dumps(facts, ensure_ascii=False, indent=1)}

{meaning}{no_tel}

{rules}

{examples}

Sentence to check:
{json.dumps(sentence, ensure_ascii=False)}

- summary_values: copy the facts that the sentence relies on
- verdict: "supported" or "unsupported"
- problem: "" if supported; otherwise one short sentence saying what is wrong

Reply with ONE JSON object only, matching this JSON schema:
{json.dumps(format_schema(JUDGE_SCHEMA), ensure_ascii=False)}
"""


def judge_errors(raw: str | None, part: str) -> list[str]:
    """The judge's verdict on one sentence; [] = supported. An unusable reply is a rejection
    (judge_invalid), never a pass."""
    if raw is None or not raw.strip():
        return [f"judge_invalid: {part}: no reply"]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return [f"judge_invalid: {part}: not valid JSON ({e.msg} at char {e.pos})"]
    if not isinstance(obj, dict):
        return [f"judge_invalid: {part}: expected an object, got {type(obj).__name__}"]
    errs = schema_errors(Draft202012Validator(load_schema(JUDGE_SCHEMA)), obj)
    if errs:
        return [f"judge_invalid: {part}: schema: {e.path or '<root>'}: {e.message}" for e in errs]
    if obj["verdict"] == "unsupported":
        return [f"judge: {part}: {obj['problem'].strip() or 'marked unsupported without a reason'}"]
    return []


def _allowed_numbers(summary: dict[str, Any], minutes: bool = False) -> list[float]:
    vals: list[float] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            if not minutes:
                vals.append(float(node))
            elif key.endswith("_s"):
                vals.append(float(node) / 60.0)          # seconds given as minutes
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
    walk(summary)
    return vals


def unsupported_numbers(text: str, summary: dict[str, Any]) -> list[str]:
    text = text.replace(summary["mission_id"], " ")
    for run in sorted(re.findall(r"\d{4,}", summary["mission_id"]), key=len, reverse=True):
        text = text.replace(run, " ")                  # the id written another way ("flight 20261115 ...")
    allowed = {False: _allowed_numbers(summary), True: _allowed_numbers(summary, minutes=True)}
    bad = []
    for m in _NUM.finditer(text):
        s = m.group(1)
        n = float(s)
        dec = len(s.split(".")[1]) if "." in s else 0
        if not any(abs(round(v, dec) - n) < 1e-9 for v in allowed[m.group(2) is not None]):
            bad.append(s)
    return bad


_NOTE_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(m/s|mps\b|metres? per second|meters? per second|km/h\b|"
                       r"seconds?|secs?|s\b|minutes?|mins?\b|min\b|kilomet(?:er|re)s?|km\b|meters?|metres?|m\b)?",
                       re.IGNORECASE)
_NAMES = re.compile(r"\b[GVT]\d+\b")              # group, vehicle and track names: G5, V1, T31


def _unit(u: str) -> str:
    u = u.lower()
    if u in ("m/s", "mps") or "per second" in u or u == "km/h":
        return "mps" if u != "km/h" else "kmh"
    if u.startswith("s"):
        return "s"
    if u.startswith("min"):
        return "min"
    if u.startswith("k"):
        return "km"
    if u.startswith("m"):
        return "m"
    return ""


def _values_by_unit(facts: Any) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {"s": [], "min": [], "m": [], "km": [], "mps": [], "": []}

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            v = float(node)
            if key.endswith("_s"):
                out["s"].append(v)
                out["min"].append(v / 60)
            elif key.endswith("_min"):
                out["min"].append(v)
            elif key.endswith("_m"):
                out["m"].append(v)
                out["km"].append(v / 1000)
            elif key.endswith("_km"):
                out["km"].append(v)
                out["m"].append(v * 1000)
            elif key.endswith("_mps"):
                out["mps"].append(v)
            else:
                out[""].append(v)
        elif isinstance(node, dict):
            inherit = key.endswith(("_s", "_min", "_m", "_km", "_mps"))     # behaviour_s: {"running": 3.7} are seconds
            for k, v in node.items():
                walk(v, key if inherit else k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
    walk(facts)
    return out


_STATUS_WORDS = {
    "SEVERE_DRONE_DISTURBANCE": re.compile(r"severe[ _]drone[ _]disturbance|disturb", re.IGNORECASE),
    "SUSPECTED_INJURY_OR_ILLNESS": re.compile(r"injur|illness|sick", re.IGNORECASE),
    "POSSIBLE_HIDDEN_YOUNG": re.compile(r"hidden[ _]young|\byoung\b|\bcalf\b|\bfawn\b", re.IGNORECASE),
    "RAPID_CORRIDOR_TRANSIT": re.compile(r"rapid[ _]corridor|corridor[ _]transit", re.IGNORECASE),
    "PROXIMITY_PACING_ALERTNESS": re.compile(r"proximity[ _]pacing|pacing", re.IGNORECASE),
    "INTERMITTENT_OCCLUSION": re.compile(r"intermittent[ _]occlusion|occlusion|occluded", re.IGNORECASE),
}


def part_number_errors(text: str, facts: dict[str, Any]) -> list[str]:
    """Each number of one part must be a value of THAT part's facts in its unit (seconds, minutes, metres or km,
    m/s; no unit = a count). Group / vehicle / track names (G5, V1, T31) and the mission id are not numbers."""
    text = _NAMES.sub(" ", text.replace(str(facts.get("mission_id", "\0")), " ")) if facts.get("mission_id") else text
    vals = _values_by_unit(facts)
    bad = []
    for m in _NOTE_NUM.finditer(text):
        s, u = m.group(1), _unit(m.group(2) or "")
        dec = len(s.split(".")[1]) if "." in s else 0
        if not any(abs(round(v, dec) - float(s)) < 1e-9 for v in vals.get(u, [])):
            bad.append(f"{s}{' ' + (m.group(2) or '') if m.group(2) else ''}")
    return bad


def note_number_errors(note: str, animal: dict[str, Any]) -> list[str]:
    """Numbers in one animal's note must be values of THAT animal, in the stated unit:
    seconds -> its visible/first/last seen times; minutes -> the same / 60; metres -> its
    closest distance; no unit -> its track id or an event count. ("isolation events
    for 2 seconds" passed the mission-wide check although 2 was the number of events.)"""
    times = [animal["visible_s"], animal["first_seen_s"], animal["last_seen_s"],
             *(animal.get("behaviour_s") or {}).values()]
    metres = [animal["min_distance_to_drone_m"], animal.get("distance_travelled_m")]
    note = re.sub(r"\bG\d+\b", " ", note)
    bad = []
    for m in _NOTE_NUM.finditer(note):
        s, unit = m.group(1), _unit(m.group(2) or "")
        if unit == "s":
            vals = times
        elif unit == "min":
            vals = [t / 60 for t in times if t is not None]
        elif unit == "m":
            vals = metres
        elif unit == "km":
            vals = [x / 1000 for x in metres if x is not None]
        elif unit == "mps":
            vals = [animal.get("max_speed_mps")]
        elif unit == "kmh":
            vals = []
        else:
            vals = [animal["track_id"], *animal["events"].values()]
        dec = len(s.split(".")[1]) if "." in s else 0
        if not any(v is not None and abs(round(v, dec) - float(s)) < 1e-9 for v in vals):
            raw = (m.group(2) or "").lower()
            bad.append(f"{s}{' ' + raw if raw else ''}")
    return bad


def _texts(rep: dict[str, Any]) -> list[str]:
    if "findings" not in rep:                                   # narrative
        return ([rep["headline"]] + [x["text"] for x in rep.get("flight", [])]
                + [t for i in rep.get("incidents", []) for t in (i["text"], i["follow_up"])]
                + ([rep["closing"]] if "closing" in rep else []))
    return [rep["headline"]] + [f["note"] for f in rep["findings"]]


def _non_latin(text: str) -> list[str]:
    return sorted({c for c in text if c.isalpha() and "LATIN" not in unicodedata.name(c, "")})


@dataclass
class ReportGate:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    parsed: dict[str, Any] | None = None


def parse_report(raw: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """JSON + report.schema.json only. Returns (report, []) or (None, reasons)."""
    if raw is None or not raw.strip():
        return None, ["llm: no reply"]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, [f"json: not valid JSON ({e.msg} at char {e.pos})"]
    if not isinstance(obj, dict):
        return None, [f"json: expected an object, got {type(obj).__name__}"]
    errs = schema_errors(Draft202012Validator(load_schema(REPORT_SCHEMA)), obj)
    if errs:
        return None, [f"schema: {e.path or '<root>'}: {e.message}" for e in errs]
    return obj, []


_DURATION = re.compile(r"\b(?:for|lasted|lasting|took|during)\s+(?:about\s+|around\s+|nearly\s+|almost\s+)?"
                       r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s\b|minutes?|mins?\b)", re.IGNORECASE)
_BEHAVIOUR = re.compile(r"\b(ran|run|running|walked|walk|walking|rested|rest|resting|lay|lying)\b(?:\s+for)?"
                        r"\s+(?:about\s+|around\s+)?(\d+(?:\.\d+)?)\s*(seconds?|secs?|s\b)", re.IGNORECASE)
_BEHAVIOUR_KEY = {"ran": "running", "run": "running", "running": "running", "walked": "walking", "walk": "walking",
                  "walking": "walking", "rested": "resting", "rest": "resting", "resting": "resting",
                  "lay": "resting", "lying": "resting"}
_ADVICE = re.compile(r"\b(should|must|recommend\w*|advis\w*|suggest\w*|ought|needs? to|please)\b", re.IGNORECASE)


def _same(value: float | None, s: str) -> bool:
    dec = len(s.split(".")[1]) if "." in s else 0
    return value is not None and abs(round(value, dec) - float(s)) < 1e-9


def sentence_errors(obj: dict[str, Any], summary: dict[str, Any]) -> dict[str, list[str]]:
    """Fixed checks that concern ONE sentence: {part: reasons}. Findings on unknown tracks are
    left to the whole-reply check (unknown_track)."""
    if has_detail(summary):
        return narrative_errors(obj, summary)
    present = {a["species"] for a in summary["animals"]}
    out: dict[str, list[str]] = {}

    def common(part: str, text: str) -> list[str]:
        r = []
        bad_letters = _non_latin(text)
        if bad_letters:
            r.append(f"language: {part}: non-Latin letters {''.join(bad_letters[:10])!r}")
        m = _ADVICE.search(text)
        if m:
            r.append(f"advice: {part}: gives a recommendation ({m.group(1)!r}); the report only describes")
        return r

    head = common("headline", obj["headline"])
    absent = [sp for sp, rx in _SPECIES_WORDS.items() if sp not in present and rx.search(obj["headline"])]
    if absent:
        head.append(f"species: the headline names {absent[0]}, which was not seen in this mission")
    bad_nums = unsupported_numbers(_NAMES.sub(" ", obj["headline"]), summary)
    if bad_nums:
        head.append(f"number: {', '.join(bad_nums[:10])} not in the mission summary")
    if head:
        out["headline"] = head
    for f in obj["findings"]:
        tid, note = f["track_id"], f["note"]
        a = animal_facts(summary, tid)
        if a is None:
            continue
        part = f"track {tid}"
        r = common(part, note)
        other = [sp for sp, rx in _SPECIES_WORDS.items() if sp != a["species"] and rx.search(note)]
        if other:
            r.append(f"species: track {tid} is {a['species']}, its note names {other[0]}")
        wrong = note_number_errors(note, a)
        if wrong:
            r.append(f"number: track {tid}'s note says {', '.join(wrong)}, which is not this animal's value")
        beh = a.get("behaviour_s")
        if beh is not None:                         # "ran 18.0 s" was the time visible, not running
            for m in _BEHAVIOUR.finditer(note):
                key = _BEHAVIOUR_KEY[m.group(1).lower()]
                if not _same(beh.get(key, 0.0), m.group(2)):
                    r.append(f"behaviour: track {tid}'s note says {m.group(1)} {m.group(2)} s; its {key} time is "
                             f"{_num(beh.get(key, 0.0))} s")
        for m in _DURATION.finditer(note):
            s, unit = m.group(1), m.group(2).lower()
            v = a["visible_s"] if not unit.startswith("min") or a["visible_s"] is None else a["visible_s"] / 60
            if not _same(v, s):
                r.append(f"duration: track {tid}'s note gives a duration of {s} {unit}; its only duration is "
                         f"visible_s = {_num(a['visible_s'])} s")
        if r:
            out.setdefault(part, []).extend(r)
    return out


def whole_errors(obj: dict[str, Any], summary: dict[str, Any], cfg: Config) -> list[str]:
    """Fixed checks on the reply as a whole (cannot be repaired sentence by sentence)."""
    if has_detail(summary):
        return narrative_whole_errors(obj, summary, cfg)
    reasons = []
    if "findings" not in obj:
        return ["missing_part: the reply has no findings"]
    status_of = {a["track_id"]: a["status"] for a in summary["animals"]}
    seen: set[int] = set()
    for f in obj["findings"]:
        tid = f["track_id"]
        if tid not in status_of:
            reasons.append(f"unknown_track: track {tid} is not in this mission")
            continue
        if tid in seen:
            reasons.append(f"duplicate: track {tid} has more than one finding")
        seen.add(tid)
        if f["status"] != status_of[tid]:
            reasons.append(f"wrong_status: track {tid} is {status_of[tid]}, the report says {f['status']}")
    for tid in required_alerts(summary):
        if tid not in seen:
            reasons.append(f"missing_alert: track {tid} ({status_of[tid]}) has no finding")
    limit = findings_limit(summary, cfg)
    if len(obj["findings"]) > limit:
        reasons.append(f"too_many: {len(obj['findings'])} findings, limit {limit}")
    n = sum(len(t) for t in _texts(obj))
    if n > cfg.report.max_chars:
        reasons.append(f"length: {n} characters, limit {cfg.report.max_chars}")
    return reasons


def check(raw: str | None, summary: dict[str, Any], cfg: Config) -> ReportGate:
    """The fixed fact gate (no LLM): whole-reply and per-sentence checks together."""
    obj, errs = parse_report(raw)
    if obj is None:
        return ReportGate(False, errs)
    reasons = whole_errors(obj, summary, cfg)
    for rs in sentence_errors(obj, summary).values():
        reasons += rs
    return ReportGate(not reasons, reasons, obj)


def _num(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _species_list(by_species: dict[str, int]) -> str:
    parts = [f"{n} {SPECIES_WORDS.get(sp, sp)}" for sp, n in sorted(by_species.items(), key=lambda kv: -kv[1])]
    return " and ".join(parts) if len(parts) < 3 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _status_words(st: str) -> str:
    return st.lower().replace("_", " ")


def template_headline(summary: dict[str, Any]) -> str:
    if has_detail(summary):
        return template_summary(story_of(summary)["summary"]["facts"])
    alerts, mid, n = summary["alert_track_ids"], summary["mission_id"], summary["tracks_seen"]
    listed = len(required_alerts(summary))
    more = f" The {listed} most urgent are listed." if listed < len(alerts) else ""
    return (f"{len(alerts)} of {n} tracks need attention in mission {mid}.{more}" if alerts else
            f"No track needs attention in mission {mid}; {n} tracks seen.")


def template_note(animal: dict[str, Any]) -> str:
    if "behaviour_s" not in animal:
        return f"{animal['species']} seen for {_num(animal['visible_s'])} s."
    s = SPECIES_WORDS.get(animal["species"], animal["species"])
    s = s[0].upper() + s[1:]
    out = f"{s}" + (f" in {animal['group']}" if animal.get("group") else "") + \
        f", seen for {_num(animal['visible_s'])} s"
    beh = [f"{k} {_num(v)} s" for k, v in sorted(animal["behaviour_s"].items(), key=lambda kv: -kv[1])]
    if beh:
        out += ": " + ", ".join(beh)
    if animal.get("distance_travelled_m") is not None:
        out += f"; moved {_num(animal['distance_travelled_m'])} m"
    if animal.get("min_distance_to_drone_m") is not None:
        out += f"; the drone came within {_num(animal['min_distance_to_drone_m'])} m"
    if animal.get("cause"):
        out += "; cause: " + ("a vehicle" if animal["cause"] == "vehicle" else "the drone" if animal["cause"] == "drone"
                              else animal["cause"])
    return out + "."


def template_report(summary: dict[str, Any], cfg: Config) -> dict[str, Any]:
    alerts = required_alerts(summary)
    headline = template_headline(summary)
    limit = findings_limit(summary, cfg)
    findings = [{"track_id": a["track_id"], "status": a["status"], "note": template_note(animal_facts(summary, a["track_id"]))}
                for a in summary["animals"][:limit]]            # alert animals come first
    rep = {"headline": headline, "findings": findings}
    if has_detail(summary):                                    # the narrative report
        rep = template_narrative(summary)
    while "findings" in rep and sum(map(len, _texts(rep))) > cfg.report.max_chars and len(rep["findings"]) > len(alerts):
        rep["findings"].pop()                                  # drop calm animals first, from the end
    n_chars = sum(map(len, _texts(rep)))
    if n_chars > cfg.report.max_chars:
        raise ValueError(f"report.max_chars = {cfg.report.max_chars} is too small: the template needs "
                         f"{n_chars} characters for the {len(alerts)} alert animal(s) alone")
    return rep


def _ollama_fn(cfg: Config, model: str) -> GenerateFn:
    llm = replace(cfg.llm, model=model, timeout_s=cfg.report.timeout_s)
    return lambda p, s: ollama_client.generate(p, s, llm, num_ctx=cfg.report.num_ctx)


def make_report(summary: dict[str, Any], cfg: Config, *, use_llm: bool = True,
                generate_fn: GenerateFn | None = None,
                judge_fn: GenerateFn | None = None) -> dict[str, Any]:
    """The report record stored in the package (report.json).

    writer -> judge, one sentence per call (if [report].judge) -> fixed checks -> every rejected
    sentence replaced by its template sentence -> the fixed gate on the result (last word).
    generate_fn / judge_fn replace the Ollama calls in tests; when only generate_fn is given it
    serves both roles (it can tell them apart by the schema it receives)."""
    if generate_fn is None:
        generate_fn = _ollama_fn(cfg, cfg.report.model)
        judge_fn = judge_fn or _ollama_fn(cfg, cfg.report.judge_model)
    judge_fn = judge_fn or generate_fn
    prompt = build_prompt(summary, cfg)
    raw = writer_s = judge_s = prompt_tokens = None
    judged: list[dict[str, Any]] = []
    reasons: list[str] = []
    repaired: list[str] = []
    report = None
    if not use_llm:
        reasons = ["llm: not used (--no-llm)"]
    else:
        try:
            rep = generate_fn(prompt, llm_schema(summary))
            raw, writer_s = rep.text, round(rep.latency_s, 3)
            prompt_tokens = getattr(rep, "prompt_tokens", None)
            if prompt_tokens is not None and prompt_tokens >= cfg.report.num_ctx - 16:
                reasons = [f"llm: the prompt filled the whole context ({prompt_tokens} of [report].num_ctx "
                           f"{cfg.report.num_ctx} tokens): the model did not read all of it"]
                raw = None
        except ollama_client.LlmUnavailable as e:
            reasons = [f"llm: unavailable: {e}"]
    parsed, errs = parse_report(raw) if raw is not None else (None, [])
    reasons += errs
    if parsed is not None:
        by_part: dict[str, list[str]] = {}
        judge_down = False
        if cfg.report.judge:                                   # 1) the judge: meaning, one sentence per call
            took = 0.0
            for part, sentence, facts in judge_parts(summary, parsed):
                try:
                    jr = judge_fn(build_judge_prompt(part, sentence, facts), format_schema(JUDGE_SCHEMA))
                except ollama_client.LlmUnavailable as e:
                    reasons.append(f"judge_llm: unavailable: {e}")
                    judge_down = True
                    break
                took += jr.latency_s
                rs = judge_errors(jr.text, part)
                judged.append({"part": part, "raw": jr.text, "reasons": rs})
                if rs:
                    by_part.setdefault(part, []).extend(rs)
            judge_s = round(took, 3)
        whole = whole_errors(parsed, summary, cfg)             # 2) the fixed checks
        for part, rs in sentence_errors(parsed, summary).items():
            by_part.setdefault(part, []).extend(rs)
        reasons += whole + [r for rs in by_part.values() for r in rs]
        n_parts = len(story_parts(story_of(summary))) if has_detail(summary) else 1 + len(parsed.get("findings", []))
        if not judge_down and not whole and len(by_part) < n_parts:
            fixed = repair(parsed, summary, by_part)           # 3) swap only the rejected sentences
            g = check(json.dumps(fixed), summary, cfg)         # 4) the fixed gate keeps the last word
            if g.accepted:
                report, repaired = fixed, [p for p in by_part]
            else:
                reasons += [f"after repair: {r}" for r in g.reasons]
    if report is not None:
        source = "llm_repaired" if repaired else "llm"
    else:
        report, source = template_report(summary, cfg), "template"
        own = check(json.dumps(report), summary, cfg)
        if not own.accepted:                                   # a bug in the template, never silent
            raise ValueError("template report fails the fact gate: " + "; ".join(own.reasons))
    times = [t for t in (writer_s, judge_s) if t is not None]
    return {"schema_version": "hima.report/1", "mission_id": summary["mission_id"], "source": source,
            "model": cfg.report.model if use_llm else None,
            "judge_model": cfg.report.judge_model if use_llm and cfg.report.judge else None,
            "latency_s": round(sum(times), 3) if times else None,   # writer + every judge call
            "writer_latency_s": writer_s, "judge_latency_s": judge_s, "prompt_tokens": prompt_tokens,
            "gate": {"accepted": source != "template", "reasons": reasons, "repaired": repaired},
            "llm_raw": raw, "judge": judged,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "summary": summary, "report": report}


def repair(report: dict[str, Any], summary: dict[str, Any], bad: dict[str, list[str]]) -> dict[str, Any]:
    """The model's report with every sentence named in `bad` replaced by its template sentence."""
    if has_detail(summary):
        return repair_narrative(report, summary, bad)
    out = {"headline": template_headline(summary) if "headline" in bad else report["headline"]}
    out["findings"] = []
    for f in report["findings"]:
        part = f"track {f['track_id']}"
        note = template_note(animal_facts(summary, f["track_id"])) if part in bad else f["note"]
        out["findings"].append({"track_id": f["track_id"], "status": f["status"], "note": note})
    return out


# ---- the narrative report ---------------------------------------------------
# "The report is an explanation of the flight, not information and numbers only." With "detail" the model writes
# the report the way a patrol report reads: headline = a short summary of the whole flight; flight = one text per
# stage, in time order; incidents = one text and one follow-up per incident; closing. The facts of every part come
# from report_story.story(summary): plain words and rounded numbers only. Each part is checked alone, like a
# sentence before: numbers by unit against THAT part's facts, species, statuses, vehicles, language, advice
# (allowed only in a follow-up), the judge with that part's facts only; a rejected part gets its template text.
#   "stage_order:"     the stages are not exactly the story's stages, in order (whole reply)
#   "incident_order:"  the incidents are not exactly the story's incidents, in order (whole reply)
# * "status:"          a part names a status (disturbance, injury, young, alert, corridor, cover) its facts do not have
# * "vehicle:"         a stage names a vehicle although none was at that place
# * "place:"           "N m / km <direction>" whose N is not a place distance (from take-off) of that part, or whose
#                      direction is not one of its places ("went 2.3 km north" was the distance flown)
# * "fact_word:"       a fact's key or a JSON word copied into the text ("under cover true", "first_after_take_off")

_STORY_STATUS = {
    "SEVERE_DRONE_DISTURBANCE": re.compile(r"disturb|\bfled\b|\bflee|panic|stampede", re.IGNORECASE),
    "SUSPECTED_INJURY_OR_ILLNESS": re.compile(r"injur|\bill\b|illness|sick|wound|\blame\b|limp", re.IGNORECASE),
    "POSSIBLE_HIDDEN_YOUNG": re.compile(r"\byoung\b|\bcalf\b|\bcalves\b|\bfawns?\b", re.IGNORECASE),
    "PROXIMITY_PACING_ALERTNESS": re.compile(r"pacing|\balert(?!\s+status)", re.IGNORECASE),
    "RAPID_CORRIDOR_TRANSIT": re.compile(r"corridor|cross(?:ed|ing|es)? quickly|passed through quickly",
                                         re.IGNORECASE),
    "INTERMITTENT_OCCLUSION": re.compile(r"occlu|under (?:the )?trees?|\btrees?\b|\bcover\b", re.IGNORECASE),
}
_VEHICLE_WORD = re.compile(r"\b(vehicles?|trucks?|cars?|pickups?|jeeps?|lorr(?:y|ies)|motorbikes?|motorcycles?)\b",
                           re.IGNORECASE)
_STORY_NAMES = re.compile(r"\b[GVTSI]\d+\b")
_COMPASS_RX = r"(north-east|north-west|south-east|south-west|north|south|east|west)"
_PLACE = re.compile(r"(\d+(?:\.\d+)?)\s*(km|kilomet(?:er|re)s?|metres?|meters?|m)\b\s+(?:to the\s+|out\s+)?"
                    + _COMPASS_RX + r"\b", re.IGNORECASE)
_PLACE_KEYS = ("distance_m", "from_m", "to_m", "farthest_m")
_FACT_WORD = re.compile(r"\b(?:true|false|null|[a-z]+_[a-z_]+)\b")
_NEGATED = re.compile(r"\b(?:no|not|without|nor|never|none)\b[^.;,]{0,40}", re.IGNORECASE)


def narrative(summary: dict[str, Any]) -> bool:
    return has_detail(summary)


def story_of(summary: dict[str, Any]) -> dict[str, Any]:
    return report_story.story(summary)


def story_parts(st: dict[str, Any]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(part, facts, check) for every part of the narrative report, in report order."""
    out = [("headline", st["summary"]["facts"], st["summary"]["check"])]
    out += [(f"stage {s['facts']['stage']}", s["facts"], s["check"]) for s in st["stages"]]
    for i in st["incidents"]:
        out.append((f"incident {i['facts']['incident']}", i["facts"], i["check"]))
        out.append((f"follow-up {i['facts']['incident']}", i["facts"], i["check"]))
    out.append(("closing", st["closing"]["facts"], st["closing"]["check"]))
    return out


def part_text(report: dict[str, Any], part: str) -> str | None:
    if part in ("headline", "closing"):
        return report.get(part)
    kind, pid = part.split(" ", 1)
    if kind == "stage":
        return next((s["text"] for s in report.get("flight", []) if s["stage"] == pid), None)
    item = next((i for i in report.get("incidents", []) if i["incident"] == pid), None)
    return None if item is None else item["text" if kind == "incident" else "follow_up"]


def narrative_schema(st: dict[str, Any]) -> dict[str, Any]:
    """What the writer must return: exactly the story's stages and incidents, in order."""
    sids = [s["facts"]["stage"] for s in st["stages"]]
    iids = [i["facts"]["incident"] for i in st["incidents"]]
    text = {"type": "string", "minLength": 1}
    return {"type": "object", "additionalProperties": False, "required": ["headline", "flight", "incidents", "closing"],
            "properties": {
                "headline": {**text, "maxLength": 500},
                "flight": {"type": "array", "minItems": len(sids), "maxItems": len(sids),
                           "items": {"type": "object", "additionalProperties": False, "required": ["stage", "text"],
                                     "properties": {"stage": {"enum": sids}, "text": {**text, "maxLength": 400}}}},
                "incidents": {"type": "array", "minItems": len(iids), "maxItems": len(iids),
                              "items": {"type": "object", "additionalProperties": False,
                                        "required": ["incident", "text", "follow_up"],
                                        "properties": {"incident": {"enum": iids} if iids else {"type": "string"},
                                                       "text": {**text, "maxLength": 500},
                                                       "follow_up": {**text, "maxLength": 300}}}},
                "closing": {**text, "maxLength": 400}}}


def _narrative_prompt(summary: dict[str, Any], cfg: Config) -> str:
    st = story_of(summary)
    facts = {"summary": st["summary"]["facts"], "stages": [s["facts"] for s in st["stages"]],
             "incidents": [i["facts"] for i in st["incidents"]], "closing": st["closing"]["facts"]}
    return f"""You write the landing report of a wildlife-monitoring drone flight over Arabian oryx and reem gazelle, for
the reserve's rangers. It explains the flight in plain words, like a ranger's patrol report: what the drone did, what
it found along the way, what happened and why. It is not a list of data: the station shows the tables. Software
already computed everything below; trust it and do not invent anything.

Facts of each part (JSON):
{json.dumps(facts, ensure_ascii=False, indent=1)}

Write these parts, in the past tense:
- headline: 2 or 3 sentences that sum up the whole flight from the "summary" facts: how long and how far it went,
  what it found, and the incidents by name (or that nothing unusual was seen).
- flight: one text per stage, in the same order, with the same stage names. 1 or 2 sentences each, so that the texts
  read one after another as one story of the flight. Use "place" for where, "herd" for what was found and
  "what_happened" for what the animals did. A stage with several calm_places is told in one sentence (the range of
  distances and the largest herd), not place by place. first_after_take_off / first_after_turning_back: say so. A
  stage with an "incident" mentions it in a few words; the incident itself explains it.
- incidents: one per incident, same order and names. text: 2 or 3 sentences: what happened, when (at_min), where
  (place), which animals (herd), what they did (what_happened) and the cause when "cause" is given. follow_up: ONE
  sentence that recommends what follow_up_topics say, in your own words, and nothing else.
- closing: 1 or 2 sentences: the rest of the flight was calm (or nothing unusual), and the limits: counts are
  minimums (the most animals seen together in one picture); under_cover true: animals under trees may be missed.
Example of the style, for a DIFFERENT flight (never copy its places or numbers):
- headline: "A 40-minute flight at about 60 m that went 1.5 km east of take-off. The drone found Arabian oryx in 3
  places and reem gazelle in 2; one incident needs attention: a car near a gazelle herd at minute 22."
- stage: "After take-off the drone flew east and passed 2 calm places with Arabian oryx between about 300 m and
  700 m out, the largest herd at least 6."
- stage: "At about 1.5 km east of take-off it found a herd of at least 5 reem gazelle; a car was close to them."
- incident: "At minute 22 a car was seen about 150 m from a herd of at least 5 reem gazelle, 1.5 km east of
  take-off. No animal in the herd showed an alert status."
- follow_up: "Rangers should check whether the car was allowed in the reserve at that time."
Rules:
- Counts are minimums: write "at least N" as in the facts. Never write "tracks", track numbers or group names.
- Use only numbers from the facts of THAT part, with their unit (_m metres, _km kilometres, _min minutes). Do not
  add, subtract or compute new numbers.
- distance_flown_km is the length of the whole path, not how far from take-off; only place, distance_m, from_m,
  to_m and farthest_m are distances from take-off.
- Write plain English: never copy a fact's key, true, false or null into the text.
- Recommendations only in follow_up. Everything else describes.
{'' if summary['metrics_known'] else '- metrics_known is false: there was no drone telemetry, so places and distances are unknown.'}
- All text together at most {cfg.report.max_chars} characters. English only.

Reply with ONE JSON object only, matching this JSON schema:
{json.dumps(narrative_schema(st), ensure_ascii=False)}
"""


_STORY_RULES = """The text is unsupported only if it:
1. uses a number with a different meaning or unit than in the facts,
2. states something the facts do not give (a cause, behaviour, health detail, place, other animals, what will
   happen), or gives an animal or herd a status the facts do not give it,
3. gives an instruction or a recommendation{follow}.
Otherwise it is supported, even if it is short, vague or leaves things out. Do not judge style."""

_STORY_MEANING = {
    "headline": """What the facts mean:
- duration_min, distance_flown_km, altitude_m: the flight's length, distance and usual height above ground.
- farthest_m / farthest_direction: the farthest the drone went from take-off.
- species: where each species was found: places = how many places, largest_at_least = the most animals of it seen
  together in one place. Counts are minimums ("at least").
- incidents: the incidents by title and minute. nothing_unusual: true only with no incident.""",
    "stage": """What the facts mean:
- one stage of the flight, in time order. from_min / to_min: the minutes it covers. leg: outbound (before the
  farthest point) or return.
- place / distance_m / direction: where the herd was, from take-off. herd / at_least: what was found there; counts
  are minimums ("at least N").
- calm_places with from_m / to_m / largest_at_least: several calm places told together.
- what_happened: what the animals did, in plain words.
- incident, incident_title, incident_at_min, incident_what: an incident happened here: its name, its minute, and
  what happened in it (a vehicle there, what the animals did); saying that briefly is supported.""",
    "incident": """What the facts mean:
- one incident. at_min: the minute it happened; from_min / to_min: when the herd was watched.
- place: where, from take-off. herd / at_least: the animals there (minimums).
- what_happened: what the animals did. cause: why, as computed ("a vehicle" or "the drone"); null = not known.
- vehicle: a vehicle seen there: kind, at_min, where (inside the herd or how far from it).""",
    "follow-up": """What the facts mean:
- follow_up_topics: the ONLY things the report may recommend for this incident. A follow-up that recommends one
  or more of them, in any words, is supported; recommending anything else is unsupported.""",
    "closing": """What the facts mean:
- incidents_count / nothing_unusual: how many incidents the flight had. rest_calm: everything else was calm.
- counts_are_minimums: counts are the most animals seen together in one picture.
- under_cover: animals went under trees at times, so some may have been missed.
- vehicles_known false: there is no vehicle data.""",
}

_STORY_EXAMPLES = {
    "headline": """Examples for different facts {"duration_min": 40, "altitude_m": 60, "farthest_m": 1500,
"farthest_direction": "east", "species": [{"species": "Arabian oryx", "places": 3, "largest_at_least": 6}],
"incidents": [{"title": "Car near a gazelle herd", "at_min": 22}], "nothing_unusual": false}:
- "A 40-minute flight at about 60 m, out to 1.5 km east. Oryx in 3 places; one incident: a car near gazelle." -> supported
- "The drone counted 6 oryx in total." -> unsupported: 6 is the largest herd, not a total.
- "Nothing unusual was seen." -> unsupported: there was an incident.""",
    "stage": """Examples for different facts {"place": "about 700 m east of take-off", "herd": "a herd of at least
5 reem gazelle", "what_happened": ["all calm: no animal showed an unusual status"], "from_min": 9, "to_min": 12}:
- "Between minute 9 and 12 it found at least 5 reem gazelle about 700 m east, all calm." -> supported
- "It found 5 gazelle grazing by a water hole." -> unsupported: the facts give no grazing or water hole.
- "The gazelle ran from the drone." -> unsupported: they were calm.""",
    "incident": """Examples for different facts {"at_min": 22, "place": "about 1.5 km east of take-off", "herd": "a
herd of at least 5 reem gazelle", "what_happened": ["animals ran (severe disturbance)"], "cause": "the drone"}:
- "At minute 22 the gazelle herd 1.5 km east ran; the cause was the drone." -> supported
- "A poacher chased the gazelle at minute 22." -> unsupported: the facts give no poacher.
- "The herd ran for 22 minutes." -> unsupported: 22 is the minute it happened, not a duration.""",
    "follow-up": """Examples for different facts {"follow_up_topics": ["keep the drone higher or farther from this
herd on the next flight"]}:
- "On the next flight the drone should keep higher above this herd." -> supported
- "Rangers should arrest the driver." -> unsupported: not one of the follow_up_topics.""",
    "closing": """Examples for different facts {"incidents_count": 1, "under_cover": true, "counts_are_minimums": true}:
- "Apart from the incident, the flight was calm. Counts are minimums, and animals under trees may be missed." -> supported
- "Nothing unusual happened." -> unsupported: there was one incident.""",
}


def story_number_errors(text: str, facts: dict[str, Any]) -> list[str]:
    """Numbers of one narrative part, by unit, against that part's facts. "minute 17" and "a 28-minute flight" are
    minutes, and so are "minutes 11 and 18" and "minute 2, 11 and 19"; in "150 to 650 m" both numbers are metres;
    S1, I2 (names) are not numbers."""
    t = _STORY_NAMES.sub(" ", text)
    t = re.sub(r"(\d)-(?=(?:minute|min|metre|meter|m\b|km|kilomet))", r"\1 ", t, flags=re.IGNORECASE)
    t = re.sub(r"\bminutes?\s+(\d+(?:\.\d+)?(?:\s*(?:,|and|to|or|-|\u2013)\s*\d+(?:\.\d+)?)*)",
               lambda m: re.sub(r"(\d+(?:\.\d+)?)", r"\1 min", m.group(1)), t, flags=re.IGNORECASE)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(?:to|and|-|–)\s*(?=(?:about\s+)?\d+(?:\.\d+)?\s*(min\b|minutes?|km\b|"
               r"kilomet(?:er|re)s?|metres?|meters?|m\b))", lambda m: f"{m.group(1)} {m.group(2)} to ", t,
               flags=re.IGNORECASE)
    return part_number_errors(t, facts)


def narrative_errors(obj: dict[str, Any], summary: dict[str, Any]) -> dict[str, list[str]]:
    """The fixed checks of each part of a narrative report: {part: reasons}."""
    out: dict[str, list[str]] = {}
    for part, facts, chk in story_parts(story_of(summary)):
        text = part_text(obj, part)
        if text is None:
            continue
        r = []
        bad_letters = _non_latin(text)
        if bad_letters:
            r.append(f"language: {part}: non-Latin letters {''.join(bad_letters[:10])!r}")
        if not part.startswith("follow-up"):
            m = _ADVICE.search(text)
            if m:
                r.append(f"advice: {part}: gives a recommendation ({m.group(1)!r}); only a follow-up may")
        other = [sp for sp, rx in _SPECIES_WORDS.items() if sp not in chk["species"] and rx.search(text)]
        if other:
            r.append(f"species: {part} names {other[0]}, which is not in its facts")
        wrong = story_number_errors(text, facts)
        if wrong:
            r.append(f"number: {part} says {', '.join(wrong[:10])}, not a value of its facts")
        if not part.startswith("follow-up"):
            said = _NEGATED.sub(" ", text)                 # "no sign of disturbance" names no status
            for st, rx in _STORY_STATUS.items():
                if rx.search(said) and st not in chk["statuses"]:
                    r.append(f"status: {part} says {rx.search(said).group(0)!r} ({st}), which its facts do not have")
        for m in _PLACE.finditer(_STORY_NAMES.sub(" ", text)):
            r += _place_errors(part, m, facts)
        fw = _FACT_WORD.search(text)
        if fw:
            r.append(f"fact_word: {part} copies {fw.group(0)!r} from the facts")
        if part.startswith(("stage", "incident")) and not chk["vehicle"] and _VEHICLE_WORD.search(text):
            r.append(f"vehicle: {part} names a {_VEHICLE_WORD.search(text).group(1)}, but none was there")
        if r:
            out[part] = r
    return out


def _place_errors(part: str, m: re.Match, facts: dict[str, Any]) -> list[str]:
    """One "N m/km <direction>" of a narrative part: N must be a distance from take-off of that part (distance_m,
    from_m, to_m, farthest_m), and the direction one of its directions."""
    dists: list[float] = []
    dirs: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _PLACE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    dists.append(float(v))
                elif k in ("direction", "farthest_direction") and isinstance(v, str):
                    dirs.add(v.lower())
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(facts)
    n, unit, way = m.group(1), m.group(2).lower(), m.group(3).lower()
    vals = [d / 1000 for d in dists] if unit.startswith("k") else dists
    out = []
    if not any(_same(v, n) for v in vals):
        out.append(f"place: {part} puts something {n} {unit} {way}, which is not a distance from take-off of its facts")
    elif dirs and way not in dirs:
        out.append(f"place: {part} says {way}; its places are {', '.join(sorted(dirs))}")
    return out


def narrative_whole_errors(obj: dict[str, Any], summary: dict[str, Any], cfg: Config) -> list[str]:
    reasons = [f"missing_part: the reply has no {k}" for k in ("flight", "incidents", "closing") if k not in obj]
    if reasons:
        return reasons
    st = story_of(summary)
    want = [s["facts"]["stage"] for s in st["stages"]]
    got = [s["stage"] for s in obj["flight"]]
    if got != want:
        reasons.append(f"stage_order: the flight has stages {got}, the story has {want}")
    want = [i["facts"]["incident"] for i in st["incidents"]]
    got = [i["incident"] for i in obj["incidents"]]
    if got != want:
        reasons.append(f"incident_order: the report has incidents {got}, the story has {want}")
    n = sum(len(t) for t in _texts(obj))
    if n > cfg.report.max_chars:
        reasons.append(f"length: {n} characters, limit {cfg.report.max_chars}")
    return reasons


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _low(s: str) -> str:
    return s[:1].lower() + s[1:] if not s[:2].isupper() else s


def template_summary(f: dict[str, Any]) -> str:
    if not f["places_with_animals"]:
        n = f.get("tracks_seen", 0)
        head = f"The drone flew for {f['duration_min']} minutes" if f.get("duration_min") is not None else \
            "The drone flew"
        return head + (" and saw no animal." if not n else
                       ", but the places of what it saw are not known (no camera parameters).")
    s = f"A {f['duration_min']}-minute flight" if f.get("duration_min") is not None else "A flight"
    if f.get("altitude_m") is not None:
        s += f" at about {f['altitude_m']} m"
    if f.get("farthest_m"):
        s += (f", out to about {f['farthest_m']} m {f['farthest_direction']} of take-off" if f["farthest_m"] < 1000
              else f", out to about {f['farthest_m'] / 1000:.1f} km {f['farthest_direction']} of take-off")
    s += "."
    sp = [f"{x['species']} in {x['places']} place{'s' if x['places'] != 1 else ''} ("
          + (f"the largest herd at least {x['largest_at_least']}" if x["places"] > 1 else
             f"at least {x['largest_at_least']}") + ")" for x in f["species"]]
    s += " The drone found " + " and ".join(sp) + "."
    if f["nothing_unusual"]:
        s += " Nothing unusual was seen."
    elif f["incidents"]:
        k = f["incidents_count"]
        s += (f" {k} incident{'s' if k != 1 else ''} need{'' if k != 1 else 's'} attention: "
              + "; ".join(f"{_low(i['title'])} at minute {i['at_min']}" for i in f["incidents"]) + ".")
    else:
        s += " No incident was seen."
    return s


def template_stage(f: dict[str, Any]) -> str:
    if f.get("no_places"):
        return ("No animal was seen." if not f["tracks_seen"] else
                "The drone saw animals, but without camera parameters their places are not known.")
    first, back = f.get("first_after_take_off"), f.get("first_after_turning_back")
    if "calm_places" in f:
        where = f"between about {f['from_m']} m and {f['to_m']} m {f['direction']} of take-off"
        body = (f"it passed {f['calm_places']} calm places with {' and '.join(f['species'])}, the largest herd at "
                f"least {f['largest_at_least']}.")
        if first:
            return f"After take-off the drone flew {f['direction']}; {where} {body}"
        lead = "Turning back" if back else "On the way back" if f["leg"] == "return" else "Further on"
        return f"{lead}, {where}, {body}"
    if f["place"].startswith("at "):                         # "at the launch point", "at a place that is not known"
        lead = ("Right after take-off" if first else "Turning back" if back
                else "On the way back" if f["leg"] == "return" else "Further on")
        s = f"{lead}, {f['place']}, it found {f['herd']}."
    else:
        lead = ("After take-off the drone flew " + f["direction"] + "; at " if first else "Turning back, at " if back
                else "On the way back, at " if f["leg"] == "return" else "Further on, at ")
        s = f"{lead}{f['place']} it found {f['herd']}."
    for w in f["what_happened"]:
        s += " " + (("It was calm." if f["herd"].startswith("a single") else "All were calm.")
                    if w.startswith("all calm") else _cap(w) + ".")
    if f.get("incident"):
        s += f" See the incident below: {_low(f['incident_title'])}."
    return s


def template_incident(f: dict[str, Any]) -> str:
    v = f.get("vehicle")
    if "herd" not in f:                                      # a vehicle away from the herds
        s = f"At minute {f['at_min']} a {v['kind']} was seen {f['place']}"
        if f.get("nearest_herd"):
            d = f["distance_to_nearest_herd_m"]
            s += (f", about {d} m from the nearest herd ({f['nearest_herd']})" if d < 1000 else
                  f", about {d / 1000:.1f} km from the nearest herd ({f['nearest_herd']})")
        return s + "."
    if v is not None:
        s = (f"At minute {v['at_min']} a {v['kind']} was seen " +
             (f"inside {f['herd']}" if v["inside"] else f"about {v['distance_to_herd_m']} m from {f['herd']}") +
             f", {f['place']}.")
    else:
        s = f"Between minute {f['from_min']} and minute {f['to_min']} the drone watched {f['herd']}, {f['place']}."
    for w in f["what_happened"]:
        s += " " + _cap(w) + "."
    if f.get("cause"):
        s += " The cause, as computed, was " + (f"the {v['kind']}." if f["cause"] == "a vehicle" and v is not None
                                                else f"{f['cause']}.")
    return s


def template_follow_up(f: dict[str, Any]) -> str:
    t = f["follow_up_topics"]
    return _cap(", and ".join(t) if len(t) <= 2 else "; ".join(t)) + "."


def template_closing(f: dict[str, Any]) -> str:
    s = ("Nothing unusual was seen on this flight." if f["nothing_unusual"] else
         f"Apart from the incident{'s' if f['incidents_count'] != 1 else ''} above, the flight was calm."
         if f["incidents_count"] else "No incident was seen.")
    s += " Counts are minimums: the most animals seen together in one picture, so a herd may be larger."
    if f["under_cover"]:
        s += " Animals under trees may have been missed."
    if not f["vehicles_known"]:
        s += " There is no vehicle data for this flight."
    if not f["metrics_known"]:
        s += " Without camera parameters, places and distances are not known."
    return s


def template_part(part: str, facts: dict[str, Any]) -> str:
    kind = part.split(" ", 1)[0]
    return {"headline": template_summary, "stage": template_stage, "incident": template_incident,
            "follow-up": template_follow_up, "closing": template_closing}[kind](facts)


def template_narrative(summary: dict[str, Any]) -> dict[str, Any]:
    st = story_of(summary)
    return {"headline": template_summary(st["summary"]["facts"]),
            "flight": [{"stage": s["facts"]["stage"], "text": template_stage(s["facts"])} for s in st["stages"]],
            "incidents": [{"incident": i["facts"]["incident"], "text": template_incident(i["facts"]),
                           "follow_up": template_follow_up(i["facts"])} for i in st["incidents"]],
            "closing": template_closing(st["closing"]["facts"])}


def repair_narrative(report: dict[str, Any], summary: dict[str, Any], bad: dict[str, list[str]]) -> dict[str, Any]:
    st = story_of(summary)
    facts = {p: f for p, f, _ in story_parts(st)}

    def pick(part: str) -> str:
        return template_part(part, facts[part]) if part in bad else part_text(report, part)
    return {"headline": pick("headline"),
            "flight": [{"stage": s["stage"], "text": pick(f"stage {s['stage']}")} for s in report["flight"]],
            "incidents": [{"incident": i["incident"], "text": pick(f"incident {i['incident']}"),
                           "follow_up": pick(f"follow-up {i['incident']}")} for i in report["incidents"]],
            "closing": pick("closing")}
