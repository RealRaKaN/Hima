"""Foreign objects from the general detector. Fixed rules, no LLM.

Every [objects].every_s of mission time the general model (COCO, yolo26s @1280) runs on the camera frame.
  - Only car / truck / bus / motorcycle (VEHICLE), person (PERSON) and dog (DOG) are kept.
  - Overlap rule: a PERSON or DOG box that overlaps an animal box of our own model (IoU >= 0.3, or its
    centre inside the animal box) is dropped: on our videos they were gazelles. VEHICLE is exempt:
    no vehicle box overlapped an animal on our videos, and in the desert clips the rule hid a real
    white car that our model called an oryx.
  - Alert only for VEHICLE, after k consecutive samples with a vehicle; one alert per run of samples;
    a new alert waits cooldown_s; an alert held by the cooldown stays pending while its run lasts.
  - PERSON and DOG are recorded in the flight log only: every one we checked was false.
Message: "HIMA A<seq> VEHICLE @<t>" (+ " x<n>" for several), one STATUSTEXT, MAV_SEVERITY_CRITICAL.
The frame with the boxes drawn is kept (JPEG) for the package: the image reaches the ground after landing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from hima.config import Config
from hima.onboard.alerts import AlertSeq

GROUPS = {"car": "VEHICLE", "truck": "VEHICLE", "bus": "VEHICLE", "motorcycle": "VEHICLE",
          "person": "PERSON", "dog": "DOG"}
ALERT_GROUPS = ("VEHICLE",)
OVERLAP_EXEMPT = ("VEHICLE",)
VEHICLE_PRIORITY = 2              # as an injury: a ranger should come (alerts.PRIORITY)
MAV_SEVERITY_CRITICAL = 2


def _xyxy(b) -> tuple[float, float, float, float]:
    cx, cy, w, h = b
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = _xyxy(a)
    bx0, by0, bx1, by1 = _xyxy(b)
    iw, ih = max(0.0, min(ax1, bx1) - max(ax0, bx0)), max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def overlaps(box, animal) -> bool:
    """IoU >= 0.3, or the centre of box inside the animal box."""
    x0, y0, x1, y1 = _xyxy(animal)
    return iou(box, animal) >= 0.3 or (x0 <= box[0] <= x1 and y0 <= box[1] <= y1)


@dataclass(frozen=True)
class ObjectBox:
    cls: str
    group: str
    conf: float
    bbox_xywh_px: tuple[float, float, float, float]
    on_animal: bool
    kept: bool                    # survives the overlap rule

    def to_dict(self) -> dict[str, Any]:
        return {"cls": self.cls, "group": self.group, "conf": round(self.conf, 4),
                "bbox_xywh_px": [round(v, 2) for v in self.bbox_xywh_px], "on_animal": self.on_animal,
                "kept": self.kept}


@dataclass(frozen=True)
class ObjectAlert:
    seq: int
    t: float
    group: str
    count: int
    boxes: tuple[ObjectBox, ...]
    run_start_t: float            # first sample of the run that raised it
    priority: int
    severity: int
    text: str
    image_jpeg: bytes = field(repr=False, compare=False, default=b"")


def draw_boxes(image: np.ndarray, animals: list[dict[str, Any]], objects: list[ObjectBox]) -> np.ndarray:
    """Copy of the frame with our model's animal boxes (green, track id + species) and the object boxes
    (red, class + confidence)."""
    out = image.copy()
    for d in animals:
        x0, y0, x1, y1 = (int(round(v)) for v in _xyxy(d["bbox_xywh_px"]))
        cv2.rectangle(out, (x0, y0), (x1, y1), (60, 200, 60), 2)
        sp = "oryx" if d["species"] == "arabian_oryx" else "gazelle"
        cv2.putText(out, f"T{d['track_id']} {sp}", (x0, max(12, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (60, 200, 60), 1, cv2.LINE_AA)
    for o in objects:
        x0, y0, x1, y1 = (int(round(v)) for v in _xyxy(o.bbox_xywh_px))
        cv2.rectangle(out, (x0, y0), (x1, y1), (40, 40, 230), 2)
        cv2.putText(out, f"{o.cls} {o.conf:.2f}", (x0, max(12, y0 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (40, 40, 230), 1, cv2.LINE_AA)
    return out


def jpeg(image: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def detect_objects(model: Any, image: np.ndarray, cfg: Config) -> list[tuple[str, float, tuple[float, ...]]]:
    """(class name, confidence, box xywh) of the kept classes, from an Ultralytics model."""
    r = model.predict(image, imgsz=cfg.objects.imgsz, conf=cfg.objects.conf, verbose=False)[0]
    b = r.boxes
    if b is None or len(b) == 0:
        return []
    names = r.names
    out = []
    for c, cf, xywh in zip(b.cls.tolist(), b.conf.tolist(), b.xywh.tolist()):
        n = names[int(c)]
        if n in GROUPS:
            out.append((n, float(cf), tuple(float(v) for v in xywh)))
    return out


@dataclass
class ObjectWatch:
    cfg: Config
    model: Any
    counter: AlertSeq
    samples: list[dict[str, Any]] = field(default_factory=list)      # every sample (flight log)
    alerts: list[ObjectAlert] = field(default_factory=list)
    held: list[tuple[float, str]] = field(default_factory=list)      # (t, why)
    compute_s: list[float] = field(default_factory=list)
    last_kept: list[ObjectBox] = field(default_factory=list)          # kept boxes of the latest sample
    _next_t: float | None = None
    _streak: int = 0
    _run_start: float | None = None
    _run_alerted: bool = False

    def vehicle_seen(self, t0: float, t1: float) -> bool:
        """At least objects.k samples in [t0, t1] with a kept VEHICLE box (the cause of a run)."""
        n = sum(1 for s in self.samples if t0 - 1e-9 <= s["t"] <= t1 + 1e-9
                and any(b["kept"] and b["group"] in ALERT_GROUPS for b in s["boxes"]))
        return n >= self.cfg.objects.k

    def due(self, t: float) -> bool:
        return self._next_t is None or t >= self._next_t - 1e-9

    def on_frame(self, t: float, image: np.ndarray, animals: list[dict[str, Any]]) -> list[ObjectAlert]:
        """Run the detector if a sample is due at mission time t; returns new alerts."""
        if not self.due(t):
            return []
        self._next_t = (t if self._next_t is None else self._next_t) + self.cfg.objects.every_s
        while self._next_t <= t + 1e-9:          # a gap in the frames skips the lost slots
            self._next_t += self.cfg.objects.every_s
        import time
        t0 = time.perf_counter()
        raw = detect_objects(self.model, image, self.cfg)
        self.compute_s.append(time.perf_counter() - t0)
        boxes = []
        for n, cf, b in raw:
            g = GROUPS[n]
            on = any(overlaps(b, a["bbox_xywh_px"]) for a in animals)
            boxes.append(ObjectBox(n, g, cf, b, on, g in OVERLAP_EXEMPT or not on))
        self.samples.append({"t": round(t, 3), "boxes": [o.to_dict() for o in boxes]})
        self.last_kept = [o for o in boxes if o.kept]
        return self._rules(t, image, animals, boxes)

    def _rules(self, t: float, image, animals, boxes: list[ObjectBox]) -> list[ObjectAlert]:
        vehicles = [o for o in boxes if o.kept and o.group in ALERT_GROUPS]
        if not vehicles:
            self._streak, self._run_start, self._run_alerted = 0, None, False
            return []
        self._streak += 1
        if self._run_start is None:
            self._run_start = t
        if self._streak < self.cfg.objects.k or self._run_alerted:
            return []
        last = self.alerts[-1] if self.alerts else None
        if last is not None and t - last.t < self.cfg.objects.cooldown_s:
            self.held.append((t, "cooldown"))
            return []
        seq = self.counter.next()
        text = f"HIMA A{seq} VEHICLE @{t:.0f}" + (f" x{len(vehicles)}" if len(vehicles) > 1 else "")
        if len(text.encode("ascii")) > self.cfg.alerts.max_message_bytes:
            raise ValueError(f"object alert is longer than {self.cfg.alerts.max_message_bytes} bytes: {text!r}")
        img = jpeg(draw_boxes(image, animals, boxes), self.cfg.triage.jpeg_quality)
        a = ObjectAlert(seq, t, "VEHICLE", len(vehicles), tuple(vehicles), self._run_start, VEHICLE_PRIORITY,
                        MAV_SEVERITY_CRITICAL, text, img)
        self._run_alerted = True
        self.alerts.append(a)
        return [a]
