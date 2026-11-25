"""Periodic AI analysis of the flight DATA, no images.

Every [periodic].every_s of mission time:
  - nothing changed since the last analysis (no new track, no status change, no alert): the code sends a
    fixed line "HIMA S<n> NO CHANGE <k> TRK" and the AI is not called;
  - else the AI gets the facts (counts, statuses, what changed, alerts; JSON, no images) and writes at most
    max_sentences English sentences. Each passes ai.check_sentence; a rejected sentence is replaced by a
    template sentence written by code from the same facts.
  - one call at a time: a slot that comes while the previous call is still running (in mission time:
    call time + its latency) is skipped and counted, so no queue builds up.
The message = a header written by code ("HIMA S<n> @<t> <k>GZ <m>OR ...") + the sentences, split into
STATUSTEXT chunks; it enters the link when the call ends (mission time t + latency), priority 5: after
urgent alerts (1-3) and camera-window results (4).
With an AiLine the call runs on the AI's own line and maybe() returns None for it; the result comes
from the line when the YOLO line collects it. A slot that comes while it is not collected is skipped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from hima.config import Config
from hima.onboard.ai import AiGateway, check_sentence
from hima.onboard.ai_line import PERIODIC, AiLine
from hima.onboard.alerts import STATUS_CODE

PRIORITY = 5
MAV_SEVERITY_INFO = 6
SCHEMA = {"type": "object", "properties": {"sentences": {"type": "array", "items": {"type": "string"}}},
          "required": ["sentences"]}
SPECIES_NAME = {"arabian_oryx": "oryx", "reem_gazelle": "gazelle"}
SHORT_STATUS = {"BASELINE": "BASE", "INTERMITTENT_OCCLUSION": "OCCL", "RAPID_CORRIDOR_TRANSIT": "TRANSIT",
                **{k: v for k, v in STATUS_CODE.items()}}


def facts_at(t: float, analysis: dict[str, Any], prev: dict[int, str], alerts: list[str]) -> dict[str, Any]:
    animals = analysis["animals"]
    by_species: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for a in animals:
        by_species[a["species"]] = by_species.get(a["species"], 0) + 1
        statuses[a["status"]] = statuses.get(a["status"], 0) + 1
    changes = [{"track_id": a["track_id"], "species": a["species"], "from": prev[a["track_id"]], "to": a["status"]}
               for a in animals if a["track_id"] in prev and prev[a["track_id"]] != a["status"]]
    new = [{"track_id": a["track_id"], "species": a["species"], "status": a["status"]}
           for a in animals if a["track_id"] not in prev]
    notable = [{"track_id": a["track_id"], "species": a["species"], "status": a["status"],
                "seen_s": int(round(a["visible_s"]))} for a in animals if a["status"] != "BASELINE"]
    return {"mission_time_s": int(t), "tracks_so_far": len(animals), "tracks_by_species": by_species,
            "statuses": statuses, "new_tracks": new, "status_changes": changes, "alerts_sent": list(alerts),
            "tracks_not_baseline": notable}


def header(n: int, t: float, facts: dict[str, Any]) -> str:
    sp = facts["tracks_by_species"]
    parts = [f"HIMA S{n}", f"@{t:.0f}", f"{sp.get('reem_gazelle', 0)}GZ", f"{sp.get('arabian_oryx', 0)}OR"]
    parts += [f"{c}{SHORT_STATUS[s]}" for s, c in sorted(facts["statuses"].items()) if s != "BASELINE"]
    return " ".join(parts)


def template_sentences(facts: dict[str, Any]) -> list[str]:
    out = []
    sp = facts["tracks_by_species"]
    counts = " and ".join(f"{n} {SPECIES_NAME[s]}" for s, n in sorted(sp.items()))
    out.append(f"{facts['tracks_so_far']} tracks so far: {counts}." if counts else "No animal tracks so far.")
    for c in facts["status_changes"][:2]:
        out.append(f"Track {c['track_id']} changed from {c['from']} to {c['to']}.")
    for a in facts["new_tracks"][:2]:
        out.append(f"New track {a['track_id']} ({SPECIES_NAME[a['species']]}), status {a['status']}.")
    return out


def build_prompt(facts: dict[str, Any], max_sentences: int) -> str:
    return (
        "You are the onboard assistant of a wildlife survey drone watching Arabian oryx and reem gazelles.\n"
        f"Write at most {max_sentences} short English sentences for the ground station that summarise ONLY the "
        "facts below: what is being watched and what changed. A track is one animal followed by the camera; "
        "track counts are not individual counts.\n"
        "Rules: use only numbers that appear in the facts; do not give advice, commands or recommendations; "
        "do not guess causes; plain ASCII.\n"
        f"Facts (JSON):\n{json.dumps(facts, sort_keys=True)}\n"
        'Answer as JSON: {"sentences": ["..."]}')


@dataclass(frozen=True)
class PeriodicResult:
    n: int
    t: float                      # slot (mission time)
    release_t: float              # when the message enters the link
    kind: str                     # no_change | llm | llm_repaired | template
    text: str
    sentences: tuple[str, ...]
    rejected: tuple[tuple[str, tuple[str, ...]], ...]
    latency_s: float | None
    facts: dict[str, Any] = field(compare=False)
    error: str | None = None


@dataclass
class Periodic:
    cfg: Config
    ai: AiGateway | None          # None: no AI (template sentences only)
    line: AiLine | None = None    # the AI's own line (None: the call runs inside maybe())
    results: list[PeriodicResult] = field(default_factory=list)
    skipped_busy: list[float] = field(default_factory=list)
    _next_t: float | None = None
    _busy_until: float = -1.0
    _prev: dict[int, str] = field(default_factory=dict)
    _alerts_since: list[str] = field(default_factory=list)
    _n: int = 0

    def note_alert(self, text: str) -> None:
        self._alerts_since.append(text)

    def maybe(self, t: float, analysis: dict[str, Any] | None) -> PeriodicResult | None:
        every = self.cfg.periodic.every_s
        if self._next_t is None:
            self._next_t = every
        if t < self._next_t - 1e-9:
            return None
        while self._next_t <= t + 1e-9:
            self._next_t += every
        if t < self._busy_until - 1e-9 or (self.line is not None and self.line.busy("periodic")):
            self.skipped_busy.append(round(t, 3))
            return None
        if analysis is None:
            return None
        self._n += 1
        facts = facts_at(t, analysis, self._prev, self._alerts_since)
        changed = bool(facts["new_tracks"] or facts["status_changes"] or facts["alerts_sent"])
        self._prev = {a["track_id"]: a["status"] for a in analysis["animals"]}
        self._alerts_since = []
        if not changed:
            text = f"HIMA S{self._n} NO CHANGE {facts['tracks_so_far']} TRK"
            r = PeriodicResult(self._n, t, t, "no_change", text, (), (), None, facts)
        elif self.ai is not None and self.line is not None:
            prompt, n = build_prompt(facts, self.cfg.periodic.max_sentences), self._n
            model, timeout = self.cfg.periodic.model, self.cfg.periodic.timeout_s
            self.line.submit("periodic", t, PERIODIC, lambda: self.ai.text(t, prompt, SCHEMA, model, timeout),
                             lambda raw, latency, err, wait: self._add(self._finish(n, t, facts, raw, latency, err,
                                                                                     wait)))
            return None
        else:
            raw = latency = err = None
            if self.ai is not None:
                raw, latency, err = self.ai.text(t, build_prompt(facts, self.cfg.periodic.max_sentences), SCHEMA,
                                                 self.cfg.periodic.model, self.cfg.periodic.timeout_s)
            r = self._finish(self._n, t, facts, raw, latency, err, 0.0, called=self.ai is not None)
            self._busy_until = r.release_t
        self.results.append(r)
        return r

    def _add(self, r: PeriodicResult) -> PeriodicResult:
        self.results.append(r)
        return r

    def _finish(self, n: int, t: float, facts: dict[str, Any], raw: str | None, latency: float | None,
                err: str | None, wait: float, called: bool = True) -> PeriodicResult:
        species = set(facts["tracks_by_species"])
        templates = template_sentences(facts)
        sentences: list[str] = []
        rejected = []
        kind = "template"
        if called:
            got = None
            if raw is not None:
                try:
                    obj = json.loads(raw)
                    got = obj.get("sentences") if isinstance(obj, dict) else None
                except ValueError:
                    err = "reply is not JSON"
                if got is not None and not (isinstance(got, list) and all(isinstance(s, str) for s in got)):
                    got, err = None, "reply has no list of sentences"
            if got is not None:
                kind = "llm"
                for s in got[: self.cfg.periodic.max_sentences]:
                    e = check_sentence(s.strip(), facts, species)
                    if e:
                        rejected.append((s, tuple(e)))
                        kind = "llm_repaired"
                    else:
                        sentences.append(s.strip())
        if kind == "template":
            sentences = templates[: self.cfg.periodic.max_sentences]
        else:
            spare = [x for x in templates if x not in sentences]
            for _ in rejected:
                if spare and len(sentences) < self.cfg.periodic.max_sentences:
                    sentences.append(spare.pop(0))
        text = header(n, t, facts) + " | " + " ".join(sentences)
        release = t + wait + (latency or 0.0)
        return PeriodicResult(n, t, release, kind, text, tuple(sentences),
                              tuple((s, e) for s, e in rejected), latency, facts, err)
