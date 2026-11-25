"""The only door from the onboard code to the AI models.

AiGateway.text() never carries an image. AiGateway.vision() carries images and is REFUSED (an exception,
in code) unless a camera window is open; the window is opened and closed by code (window.py), never by the
model. Every call is logged: kind, model, number of images, window, latency, error.

check_sentence() is the fixed check every AI sentence passes before it may be sent (as for the
landing report): ASCII only (the link is ASCII), no advice or command words (analysis only), every
number must be a number of the facts, and a species may be named only if it is in the facts.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from hima.config import Config

# generate(prompt, schema, model, timeout_s, images_b64) -> (text, latency_s); raises on failure
GenerateFn = Callable[[str, dict[str, Any], str, float, list[str]], tuple[str, float]]

ADVICE = re.compile(r"\b(should|must|recommend\w*|advis\w*|suggest\w*|ought|needs? to|please|climb|descend|retreat)\b",
                    re.IGNORECASE)
NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
SPECIES_WORDS = {"arabian_oryx": re.compile(r"\boryx\w*", re.IGNORECASE),
                 "reem_gazelle": re.compile(r"\bgazelles?\b|\breem\b", re.IGNORECASE)}


class CameraAccessDenied(RuntimeError):
    """An image was about to reach the AI outside an open camera window."""


def ollama_generate(cfg: Config) -> GenerateFn:
    """Real calls to the local Ollama server ([llm].url), structured output, temperature and seed of [llm].
    A call with images (camera window) sets the context size [window].num_ctx. Every call keeps its model
    loaded [onboard].ai_keep_alive_s (with the server's 5 min default the window model went cold)."""
    from hima.advisor.ollama_client import generate

    def fn(prompt: str, schema: dict[str, Any], model: str, timeout_s: float, images: list[str]) -> tuple[str, float]:
        llm = replace(cfg.llm, model=model, timeout_s=timeout_s)
        r = generate(prompt, schema, llm, images=images or None, num_ctx=cfg.window.num_ctx if images else None,
                     keep_alive_s=cfg.onboard.ai_keep_alive_s)
        return r.text, r.latency_s
    return fn


def flight_models(cfg: Config, periodic: bool, windows: bool) -> list[str]:
    return list(dict.fromkeys(([cfg.periodic.model] if periodic else []) + ([cfg.window.model] if windows else [])))


def prepare_flight_models(cfg: Config, models: list[str], **http: Any) -> dict[str, Any]:
    """Before take-off. With [onboard].ai_flight_only: unload every loaded model that the flight does not
    use (the GPU is shared with YOLO), then load each flight model and keep it [onboard].ai_keep_alive_s, so the
    first camera window does not wait for a cold load. Never raises: a failure is written down and the flight
    goes on (the calls themselves still try). http: get=/post= for tests."""
    from hima.advisor import ollama_client as oc

    out: dict[str, Any] = {"models": list(models), "unloaded": [], "loaded": [], "resident": None, "error": None}
    get = {"get": http["get"]} if "get" in http else {}
    post = {"post": http["post"]} if "post" in http else {}
    try:
        if cfg.onboard.ai_flight_only:
            for m in oc.loaded_models(cfg.llm, **get):
                if m["name"] not in models and m["name"].removesuffix(":latest") not in models:
                    oc.set_loaded(m["name"], cfg.llm, 0, **post)
                    out["unloaded"].append(m["name"])
        for name in models:
            try:
                out["loaded"].append({"model": name, "load_s": round(oc.set_loaded(
                    name, cfg.llm, cfg.onboard.ai_keep_alive_s, **post), 3), "error": None})
            except oc.LlmUnavailable as e:
                out["loaded"].append({"model": name, "load_s": None, "error": str(e)[:300]})
        out["resident"] = oc.loaded_models(cfg.llm, **get)
    except oc.LlmUnavailable as e:
        out["error"] = str(e)[:300]
    return out


def release_flight_models(cfg: Config, models: list[str], **http: Any) -> dict[str, Any]:
    """After landing: unload the flight models, so the landing report model has the memory. Never raises."""
    from hima.advisor import ollama_client as oc

    post = {"post": http["post"]} if "post" in http else {}
    out: dict[str, Any] = {"unloaded": [], "error": None}
    for name in models:
        try:
            oc.set_loaded(name, cfg.llm, 0, **post)
            out["unloaded"].append(name)
        except oc.LlmUnavailable as e:
            out["error"] = str(e)[:300]
    return out


def b64_jpeg(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class AiGateway:
    generate: GenerateFn
    calls: list[dict[str, Any]] = field(default_factory=list)
    window_open: int | None = None             # id of the open camera window, set only by window.py

    def _call(self, kind: str, t: float, prompt: str, schema: dict[str, Any], model: str, timeout_s: float,
              images: list[str]) -> tuple[str | None, float, str | None]:
        t0 = time.perf_counter()
        try:
            text, latency = self.generate(prompt, schema, model, timeout_s, images)
            err = None
        except Exception as e:                  # noqa: BLE001 - any model failure is logged, never fatal
            text, latency, err = None, time.perf_counter() - t0, f"{type(e).__name__}: {e}"[:300]
        self.calls.append({"t": round(t, 3), "kind": kind, "model": model, "images": len(images),
                           "window": self.window_open if images else None,
                           "latency_s": round(latency, 3), "error": err})
        return text, latency, err

    def text(self, t: float, prompt: str, schema: dict[str, Any], model: str, timeout_s: float):
        return self._call("text", t, prompt, schema, model, timeout_s, [])

    def vision(self, t: float, prompt: str, schema: dict[str, Any], model: str, timeout_s: float, images: list[str]):
        if not images:
            raise ValueError("vision() without images: use text()")
        if self.window_open is None:
            self.calls.append({"t": round(t, 3), "kind": "vision", "model": model, "images": len(images),
                               "window": None, "latency_s": 0.0, "error": "refused: no camera window open"})
            raise CameraAccessDenied("images may reach the AI only inside an open camera window")
        return self._call("vision", t, prompt, schema, model, timeout_s, images)


_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
          "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_WORD_NUM = re.compile(r"\b(" + "|".join(_TENS[2:]) + r")(?:[- ](" + "|".join(_UNITS[1:10]) + r"))?\b|\b("
                       + "|".join(_UNITS) + r")\b", re.IGNORECASE)
# A model wrote "Three ... tracks" (the facts said 4) and "Sixteen Arabian oryx": number words passed the gate
STATUS_WORDS = {"SEVERE_DRONE_DISTURBANCE": re.compile(r"\bsevere|\bdisturb", re.IGNORECASE),
                "SUSPECTED_INJURY_OR_ILLNESS": re.compile(r"\binjur|\billness|\bill\b", re.IGNORECASE),
                "RAPID_CORRIDOR_TRANSIT": re.compile(r"\btransit|\bcorridor", re.IGNORECASE),
                "INTERMITTENT_OCCLUSION": re.compile(r"\boccl", re.IGNORECASE),
                "PROXIMITY_PACING_ALERTNESS": re.compile(r"\bproximity|\bpacing", re.IGNORECASE),
                "POSSIBLE_HIDDEN_YOUNG": re.compile(r"\byoung|\bhidden|\bfawn|\bcalf|\bcalves", re.IGNORECASE)}
ANIMAL_COUNT = re.compile(r"\b\d+\s+(?:arabian\s+)?(?:oryx\w*|(?:reem\s+)?gazelles?)\b", re.IGNORECASE)


def words_to_digits(s: str) -> str:
    """'twenty-four tracks' -> '24 tracks' (0-99), so the number check sees numbers written as words."""
    def rep(m):
        if m.group(3):
            return str(_UNITS.index(m.group(3).lower()))
        return str(_TENS.index(m.group(1).lower()) * 10 + (_UNITS.index(m.group(2).lower()) if m.group(2) else 0))
    return _WORD_NUM.sub(rep, s)


def facts_numbers(facts: Any) -> set[float]:
    """Every number in the facts, plus the length of every list (a count the facts give, e.g. 2 new tracks)."""
    out: set[float] = set()
    if isinstance(facts, bool):
        return out
    if isinstance(facts, (int, float)):
        out.add(float(facts))
    elif isinstance(facts, str):
        out.update(float(x) for x in NUMBER.findall(facts))
    elif isinstance(facts, dict):
        for k, v in facts.items():
            out |= facts_numbers(k) | facts_numbers(v)
    elif isinstance(facts, (list, tuple)):
        out.add(float(len(facts)))
        for v in facts:
            out |= facts_numbers(v)
    return out


def _species_status(facts: Any, out: set[tuple[str, str]]) -> set[tuple[str, str]]:
    if isinstance(facts, dict):
        if "species" in facts:
            for k in ("status", "to"):
                if isinstance(facts.get(k), str):
                    out.add((facts["species"], facts[k]))
        for v in facts.values():
            _species_status(v, out)
    elif isinstance(facts, (list, tuple)):
        for v in facts:
            _species_status(v, out)
    return out


def check_sentence(s: str, facts: dict[str, Any], species_present: set[str], max_chars: int = 200) -> list[str]:
    errs = []
    if not s.strip():
        return ["empty"]
    if len(s) > max_chars:
        errs.append(f"longer than {max_chars} characters")
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        errs.append("not ASCII")
    if ADVICE.search(s):
        errs.append(f"advice word {ADVICE.search(s).group(0)!r} (analysis only)")
    d = words_to_digits(s)
    allowed = facts_numbers(facts)
    for x in NUMBER.findall(d):
        if not any(abs(float(x) - a) < 0.051 for a in allowed):
            errs.append(f"number {x} is not in the facts")
    named = {sp for sp, rx in SPECIES_WORDS.items() if rx.search(s)}
    for sp in sorted(named):
        if sp not in species_present:
            errs.append(f"names {sp} but no {sp} is in the facts")
    if "statuses" in facts:                       # periodic facts: every non-baseline track with its species
        pairs = _species_status(facts, set())
        for st, rx in STATUS_WORDS.items():
            if rx.search(s):
                for sp in sorted(named & species_present):
                    if (sp, st) not in pairs:
                        errs.append(f"says a {sp} track is {st}; the facts do not")
    if "tracks_so_far" in facts and ANIMAL_COUNT.search(d) and "track" not in s.lower():
        errs.append("counts animals; the facts count tracks (one animal can have several tracks)")
    return errs
