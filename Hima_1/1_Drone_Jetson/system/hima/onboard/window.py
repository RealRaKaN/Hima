"""Camera window for the AI at an event.

Triggers (opened by CODE, after the urgent alert was queued): a vehicle alert, or an animal alert with
SEVERE_DRONE_DISTURBANCE, SUSPECTED_INJURY_OR_ILLNESS (stage 2 or 3) or POSSIBLE_HIDDEN_YOUNG (is it a young
animal or an adult lying down? are long horns visible? Adults of both species carry horns, young of the year have
none or short ones; the size rule alone cannot tell a calf from a lying adult seen from above).
Limits: one window at a time, at most max_per_flight per flight, cooldown_s per trigger type; a trigger
that is refused is logged with the reason.
While open: max_images frames are taken, one every image_every_s from the trigger frame on. Each image is the
frame with our boxes drawn (animal: track id + species; object: class + confidence), cropped around the
subject of the event (a square of crop_px, or three times the subject box if larger).
Then ONE vision call (ai.AiGateway.vision, refused in code outside a window) with the facts and the
images; the reply is a JSON object with fixed fields whose values come from a list. The window closes when
the call returns; the next AI call is text only.
With an AiLine the call runs on the AI's own line: the YOLO line hands over copies of the images and
goes on; the window stays open (new triggers refused, as before) until the reply is collected.
Message (code writes it from the fields; numbers come from the data, never from the model):
  "HIMA E<n> VEHICLE AI:<visible> <type>" or "HIMA E<n> T<id> <status> AI:LYING <x> CAUSE <y>"
  or "HIMA E<n> T<id> YOUNG AI:<YOUNG|ADULT|UNSURE> HORNS <YES|NO|UNSURE>"
  then "E<n> UNCONFIRMED: <note>" if the note passes the checks. What the model says about the image is not
checked by code and is marked unconfirmed; the images go into the package for a human after landing.
The animal question is neutral and the facts carry no alert text, status or cause (the model echoed
them back as findings). A vehicle or person cause, or a note naming one, needs a kept VEHICLE or PERSON
detector box in the window's images; without one the cause becomes UNSURE and the note is refused
(log field claim_errors).
The reply never changes the status, the alert or the command: the rules decide; the reply is information.
Every window is written to the access log.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hima.config import Config
from hima.onboard.ai import AiGateway, CameraAccessDenied, b64_jpeg, check_sentence
from hima.onboard.ai_line import WINDOW, AiLine
from hima.onboard.alerts import STATUS_CODE
from hima.onboard.objects import ObjectBox, draw_boxes, jpeg

PRIORITY = 4
MAV_SEVERITY_NOTICE = 5
YNU = ["yes", "no", "unsure"]
SCHEMAS = {
    "VEHICLE": {"type": "object", "properties": {
        "vehicle_visible": {"enum": YNU}, "vehicle_type": {"enum": ["car", "pickup", "truck", "other", "unsure"]},
        "moving": {"enum": YNU}, "people_visible": {"enum": YNU}, "note": {"type": "string"}},
        "required": ["vehicle_visible", "vehicle_type", "moving", "people_visible", "note"]},
    "ANIMAL": {"type": "object", "properties": {
        "animal_visible": {"enum": YNU}, "lying_down": {"enum": YNU},
        "cause_visible": {"enum": ["vehicle", "person", "other_animal", "none", "unsure"]},
        "note": {"type": "string"}},
        "required": ["animal_visible", "lying_down", "cause_visible", "note"]},
    "YOUNG": {"type": "object", "properties": {
        "animal_visible": {"enum": YNU}, "young_or_adult": {"enum": ["young", "adult_lying", "unsure"]},
        "long_horns_visible": {"enum": YNU}, "note": {"type": "string"}},
        "required": ["animal_visible", "young_or_adult", "long_horns_visible", "note"]},
}
QUESTIONS = {
    "VEHICLE": "Our object detector reported a vehicle (red box). Is a vehicle really visible? What type? Is it "
               "moving across the images? Are people visible near it?",
    "ANIMAL": "Look at the green box labelled with the track id given in the facts. Is there really an animal "
              "inside that box, or is it ground, a rock, a bush or a shadow? If there is an animal, is it lying "
              "down? Anywhere in the images, do you see a vehicle, a person, or an animal of another kind?",
    "YOUNG": "Our rules think the animal with the green box of the track named in the facts may be a young animal "
             "hiding: its body looks smaller than an adult's and it has stayed alone and still for minutes. Seen "
             "from above, an adult lying down with its legs folded can also look small. Is it a young animal or an "
             "adult lying down? Are long horns visible on it (adult Arabian oryx have long straight horns, adult "
             "reem gazelles have ringed horns; young of the year have none or short stubs)?",
}
YOUNG_CODE = {"young": "YOUNG", "adult_lying": "ADULT", "unsure": "UNSURE"}
CAUSE_CODE = {"vehicle": "VEHICLE", "person": "PERSON", "other_animal": "ANIMAL", "none": "NONE", "unsure": "UNSURE"}
TRACK_REF = re.compile(r"\b(?:T|track\s*)(\d+)\b", re.IGNORECASE)
# A claim about the image that a detector can check: the model must not see a vehicle or a person that no
# detector box shows. In a test gemma read "SEVVEH" in the alert and answered "cause: vehicle" on an image without one.
CLAIM_GROUPS = {"vehicle": "VEHICLE", "person": "PERSON"}
CLAIM_WORDS = {"VEHICLE": re.compile(r"\b(vehicles?|cars?|trucks?|pickups?|jeeps?|4x4)\b", re.IGNORECASE),
               "PERSON": re.compile(r"\b(person|people|persons|man|men|woman|women|human|humans|poachers?)\b",
                                    re.IGNORECASE)}


def _schema_kind(trigger_kind: str) -> str:
    return trigger_kind if trigger_kind in ("VEHICLE", "YOUNG") else "ANIMAL"


def _square(box, w: int, h: int, side_min: int) -> tuple[int, int, int, int]:
    cx, cy, bw, bh = box
    side = int(min(max(side_min, 3 * max(bw, bh)), w, h))
    x0 = int(min(max(0, cx - side / 2), w - side))
    y0 = int(min(max(0, cy - side / 2), h - side))
    return x0, y0, x0 + side, y0 + side


@dataclass
class Trigger:
    kind: str                     # VEHICLE | SEVERE | INJURY | YOUNG
    t: float
    alert_text: str
    track_id: int | None = None
    subject_box: tuple[float, float, float, float] | None = None
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class Window:
    n: int
    trigger: Trigger
    opened_t: float
    image_t: list[float] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)          # JPEG, kept for the package
    image_boxes: list[list[dict[str, Any]]] = field(default_factory=list)
    next_image_t: float = 0.0


@dataclass(frozen=True)
class WindowResult:
    n: int
    release_t: float
    texts: tuple[str, ...]
    log: dict[str, Any]


class WindowManager:
    def __init__(self, cfg: Config, ai: AiGateway | None, line: AiLine | None = None):
        """line: the AI's own line (None: the call runs inside on_frame and its result is returned)."""
        self.cfg, self.ai, self.line = cfg, ai, line
        self.asking: Window | None = None                     # images taken, reply not collected yet
        self.open: Window | None = None
        self.windows: list[Window] = []
        self.log: list[dict[str, Any]] = []                     # access log (one entry per window)
        self.refused: list[dict[str, Any]] = []
        self._last_by_kind: dict[str, float] = {}

    def trigger(self, tr: Trigger) -> bool:
        w = self.cfg.window
        why = None
        if self.ai is None:
            why = "no AI configured"
        elif self.open is not None:
            why = f"window {self.open.n} is open"
        elif len(self.windows) >= w.max_per_flight:
            why = f"max_per_flight {w.max_per_flight} reached"
        elif tr.kind in self._last_by_kind and tr.t - self._last_by_kind[tr.kind] < w.cooldown_s:
            why = f"cooldown {w.cooldown_s:g} s for {tr.kind}"
        if why:
            self.refused.append({"t": round(tr.t, 3), "kind": tr.kind, "alert": tr.alert_text, "why": why})
            return False
        win = Window(len(self.windows) + 1, tr, tr.t, next_image_t=tr.t)
        self.windows.append(win)
        self._last_by_kind[tr.kind] = tr.t
        self.open = win
        self.ai.window_open = win.n
        return True

    def on_frame(self, t: float, image: np.ndarray, animals: list[dict[str, Any]],
                 objects: list[ObjectBox]) -> WindowResult | None:
        win = self.open
        if win is None or t < win.next_image_t - 1e-9:
            return None
        box = win.trigger.subject_box
        if win.trigger.track_id is not None:
            box = next((tuple(d["bbox_xywh_px"]) for d in animals if d["track_id"] == win.trigger.track_id), box)
        elif objects:
            box = max(objects, key=lambda o: o.conf).bbox_xywh_px
        h, w = image.shape[:2]
        x0, y0, x1, y1 = _square(box or (w / 2, h / 2, w, h), w, h, self.cfg.window.crop_px)
        drawn = draw_boxes(image, animals, objects)[y0:y1, x0:x1]
        win.images.append(jpeg(drawn, self.cfg.triage.jpeg_quality))
        win.image_t.append(round(t, 3))
        win.image_boxes.append([{"track_id": d["track_id"], "species": d["species"],
                                 "bbox_xywh_px": d["bbox_xywh_px"]} for d in animals] +
                               [o.to_dict() for o in objects])
        win.next_image_t = t + self.cfg.window.image_every_s
        if len(win.images) < self.cfg.window.max_images:
            return None
        return self._ask(t, win)

    def close_unfinished(self, t: float) -> WindowResult | None:
        """Landing while a window is collecting: ask with the images taken so far."""
        if self.open is not None and self.open.images and self.asking is None:
            return self._ask(t, self.open)
        if self.open is not None and self.asking is None:
            self.ai.window_open = None
            self.open = None
        return None

    def _ask(self, t: float, win: Window) -> WindowResult | None:
        """Without a line: call now and return the result. With a line: hand the call over, return None; the
        result comes from the line's finish (WindowManager._finish) when the YOLO line collects it."""
        prompt, schema, images = self._prompt(win)
        model, timeout = self.cfg.window.model, self.cfg.window.timeout_s

        def call():
            return self.ai.vision(t, prompt, schema, model, timeout, images)
        if self.line is None:
            try:
                raw, latency, err = call()
            finally:
                self.ai.window_open = None            # closed by code when the call returns (or fails)
                self.open = None
            return self._finish(t, win, raw, latency, err, 0.0)
        self.asking = win
        win.next_image_t = float("inf")              # no more images for this window
        self.line.submit("window", t, WINDOW, call,
                         lambda raw, latency, err, wait: self._closed(t, win, raw, latency, err, wait))
        return None

    def _closed(self, t: float, win: Window, raw, latency, err, wait: float) -> WindowResult:
        self.ai.window_open = None
        self.open = self.asking = None
        return self._finish(t, win, raw, latency, err, wait)

    @staticmethod
    def _model_facts(tr: Trigger) -> dict[str, Any]:
        """What the model is told: which box to look at, never the alert, its status or its cause. A model
        told "our rules raised an alert" answered "visible: yes" on 7 of 8 empty images (in testing), and one
        told "SEVVEH" saw a vehicle that was not there (in testing)."""
        f: dict[str, Any] = {"mission_time_s": int(tr.t), "species_in_data": tr.facts.get("species", [])}
        row = tr.facts.get("track")
        if tr.track_id is not None and row is not None:
            f["track"] = {k: row[k] for k in ("track_id", "species", "body_length_m", "adult_length_m") if k in row}
        return f

    def _prompt(self, win: Window) -> tuple[str, dict[str, Any], list[str]]:
        tr = win.trigger
        kind = _schema_kind(tr.kind)
        facts = self._model_facts(tr)
        prompt = (f"You see {len(win.images)} images from a wildlife survey drone camera looking straight down, "
                  "cropped around the event and taken about "
                  f"{self.cfg.window.image_every_s:g} s apart. Green boxes: animals found by our detector, labelled "
                  "with track id and species. Red boxes: objects found by a general detector.\n"
                  f"{QUESTIONS[kind]}\nFacts from the onboard data (JSON): {json.dumps(facts, sort_keys=True)}\n"
                  "Answer as JSON with the fields of the schema. Use 'unsure' when the image does not show it. "
                  "The note is one short English sentence about what the images show, with no advice and no "
                  "numbers except track ids.")
        return prompt, SCHEMAS[kind], [b64_jpeg(b) for b in win.images]

    def _finish(self, t: float, win: Window, raw: str | None, latency: float | None, err: str | None,
                wait: float) -> WindowResult:
        tr = win.trigger
        kind = _schema_kind(tr.kind)
        facts = {"event": tr.kind, "alert": tr.alert_text, "mission_time_s": int(tr.t), **tr.facts}
        verdict, reply, note_ok, note_errs = "error" if err else "invalid", None, False, []
        claim_errs: list[str] = []
        boxed = {b["group"] for boxes in win.image_boxes for b in boxes if b.get("kept") and "group" in b}
        if raw is not None:
            try:
                reply = json.loads(raw)
            except ValueError:
                reply = None
            if isinstance(reply, dict) and all(
                    k in reply and (k == "note" or reply[k] in SCHEMAS[kind]["properties"][k]["enum"])
                    for k in SCHEMAS[kind]["required"]):
                verdict = "ok"
            else:
                reply, err = None, err or "reply does not follow the schema"
        texts = []
        if verdict == "ok":
            if kind == "VEHICLE":
                texts.append(f"HIMA E{win.n} VEHICLE AI:{reply['vehicle_visible'].upper()} "
                             f"{reply['vehicle_type'].upper()}")
            elif kind == "YOUNG":
                texts.append(f"HIMA E{win.n} T{tr.track_id} YOUNG AI:{YOUNG_CODE[reply['young_or_adult']]} "
                             f"HORNS {reply['long_horns_visible'].upper()}")
            else:
                cause = reply["cause_visible"]
                if cause in CLAIM_GROUPS and CLAIM_GROUPS[cause] not in boxed:
                    claim_errs.append(f"cause {cause}: no {cause} box in the images")
                    cause = "unsure"
                texts.append(f"HIMA E{win.n} T{tr.track_id} {tr.kind} AI:LYING {reply['lying_down'].upper()} "
                             f"CAUSE {CAUSE_CODE[cause]}")
            note = str(reply.get("note", "")).strip()
            known_tracks = {int(x) for x in facts.get("track_ids", [])} | ({tr.track_id} if tr.track_id else set())
            known_tracks |= {int(b["track_id"]) for boxes in win.image_boxes for b in boxes if "track_id" in b}
            ids = {int(x) for x in TRACK_REF.findall(note)}
            stripped = TRACK_REF.sub("", note)
            note_errs = check_sentence(stripped, {}, set(facts.get("species", [])))
            note_errs = [e for e in note_errs if not e.startswith("names ")]     # species in images
            note_errs += [f"track {i} is not in the data" for i in sorted(ids - known_tracks)]
            note_errs += [f"mentions a {g.lower()}; no {g.lower()} box in the images"
                          for g, rx in CLAIM_WORDS.items() if rx.search(note) and g not in boxed]
            if note and not note_errs:
                note_ok = True
                texts.append(f"E{win.n} UNCONFIRMED: {note}")
        else:
            texts.append(f"HIMA E{win.n} {tr.kind} AI:NO ANSWER")
        release = t + wait + (latency or 0.0)
        entry = {"window": win.n, "trigger": {"kind": tr.kind, "t": round(tr.t, 3), "alert": tr.alert_text,
                                              "track_id": tr.track_id},
                 "opened_t": round(win.opened_t, 3), "closed_t": round(release, 3), "image_t": win.image_t,
                 "images": len(win.images), "model": self.cfg.window.model,
                 "latency_s": None if latency is None else round(latency, 3), "queue_wait_s": round(wait, 3),
                 "verdict": verdict,
                 "error": err, "reply": reply, "raw": None if verdict == "ok" or raw is None else raw[:500],
                 "note_ok": note_ok, "note_errors": note_errs, "claim_errors": claim_errs, "sent": texts}
        self.log.append(entry)
        return WindowResult(win.n, release, tuple(texts), entry)


__all__ = ["WindowManager", "Trigger", "WindowResult", "CameraAccessDenied", "SCHEMAS", "STATUS_CODE"]
