"""The onboard flight: onboard loop + live alerts + text link (+ foreign objects, periodic AI
analysis of the data, camera window for the AI at events).

fly(doc) replays a recorded session; OnboardFlight takes frames one at a time as they arrive (the
end-to-end run and the simulation feed it straight from the tracker). Both use the same wiring.

The AI has its own line (ai_line.AiLine): this YOLO line hands a job over and
never waits for a model; a camera window takes its images from the frames this line already has.

Order inside OnboardFlight.push(frame, image) at mission time t:
  1. AI results the AI line has finished, and that a real flight would have by t, are read and enter the
     link at their own end time;
  2. the onboard loop (analysis refresh every update_s) and the fixed animal alerts -> link;
     the time over each animal (dwell.DwellWatch): a DWELL message when a stay is too long -> link;
  3. with a camera image: the general detector (every objects.every_s) and its vehicle alert -> link;
     then a camera window may open for a new vehicle / SEVERE / INJURY alert, and takes its images;
  4. the periodic analysis slot (every periodic.every_s);
  5. the link sends what it can up to t.
An AI result enters the link at t + queue wait + latency (mission time = wall time of the call): the
simulation runs slower than real time, so this keeps the message at the time a real flight would allow.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from hima.analytics.identity import Resolver
from hima.config import Config
from hima.onboard.ai import AiGateway
from hima.onboard.ai_line import AiLine
from hima.onboard.dwell import DwellWatch
from hima.onboard.alerts import Alert, AlertEngine, AlertSeq
from hima.onboard.link import CHUNK_SEQ_BASE, Delivery, LinkSim, make_chunks
from hima.onboard.loop import OnboardLoop, ReplayResult, Snapshot, replay
from hima.onboard.objects import ObjectAlert, ObjectWatch
from hima.onboard.periodic import MAV_SEVERITY_INFO, Periodic
from hima.onboard.periodic import PRIORITY as PERIODIC_PRIORITY
from hima.onboard.window import MAV_SEVERITY_NOTICE, Trigger, WindowManager
from hima.onboard.window import PRIORITY as WINDOW_PRIORITY

WINDOW_STATUSES = {"SEVERE_DRONE_DISTURBANCE": "SEVERE", "SUSPECTED_INJURY_OR_ILLNESS": "INJURY",
                   "POSSIBLE_HIDDEN_YOUNG": "YOUNG"}          # Young, or an adult lying down?


@dataclass
class FlightResult:
    replay: ReplayResult
    alerts: list[Alert]
    held: list[tuple[float, int, str, str]]
    deliveries: list[Delivery]
    dwell: list[Any] = field(default_factory=list)     # DWELL messages

    @property
    def loop(self) -> OnboardLoop:
        return self.replay.loop

    @property
    def final(self) -> dict[str, Any]:
        return self.replay.final


def _wire(cfg: Config, params_known: bool, counter: AlertSeq | None = None,
          after: Callable[[list[Alert], Snapshot, dict[str, Any]], None] | None = None):
    engine = AlertEngine(cfg, params_known=params_known, counter=counter or AlertSeq())
    link = LinkSim(cfg.link.bytes_per_s, cfg.link.signed)

    def on_refresh(snap, analysis, frame):
        new = engine.on_refresh(snap.t, analysis, frame["drone"]["agl_m"])
        for a in new:
            link.send(a, snap.t)
        link.advance(snap.t)
        if after is not None:
            after(new, snap, analysis)

    return engine, link, on_refresh


def fly(doc: dict[str, Any], cfg: Config, *, speed: float | None = None, **replay_kw) -> FlightResult:
    engine, link, on_refresh = _wire(cfg, bool(doc["meta"]["camera"]["params_known"]))
    dwell = DwellWatch(cfg, engine.counter)            # the same time-over-animal rule as OnboardFlight
    ident = Resolver(doc["meta"], cfg)                 # registered animals only, as OnboardFlight

    def on_frame(f):
        ident.push(f)
        for a in dwell.on_frame(f["t"], ident.live_detections()):
            link.send(a, f["t"])
        link.advance(f["t"])
    res = replay(doc, cfg, speed=speed, on_refresh=on_refresh, on_frame=on_frame, **replay_kw)
    link.flush()                       # alerts still queued when the last frame arrived
    return FlightResult(res, engine.alerts, engine.held, link.delivered, dwell.alerts)


class OnboardFlight:
    """The onboard side for live frames: push(frame[, image]) as each frame arrives, land() at the end.

    general_model: the COCO model for foreign objects (None: no object watch).
    ai: AiGateway for the periodic analysis and the camera window (None: neither runs; periodic=True with
    ai=None or periodic_ai=False sends template sentences only).
    ai_threaded: True (the flight): the AI has its own line and this line never waits for it. False: each AI
    call runs inside push(), (unit tests that need exact frames)."""

    def __init__(self, meta: dict[str, Any], cfg: Config, *, clock: Callable[[], float] = time.perf_counter,
                 general_model: Any = None, ai: AiGateway | None = None, periodic: bool = False,
                 windows: bool = False, periodic_ai: bool = True, ai_threaded: bool = True):
        self.cfg = cfg
        self.counter = AlertSeq()
        self._new_animal: list[Alert] = []
        self.engine, self.link, on_refresh = _wire(cfg, bool(meta["camera"]["params_known"]), self.counter,
                                                   lambda new, snap, an: self._new_animal.extend(new))
        self.loop = OnboardLoop(meta, cfg, clock=clock, on_refresh=on_refresh)
        self.identity = Resolver(meta, cfg)        # the same rules as the analysis, one frame at a time
        self.ai = ai
        self.objects = ObjectWatch(cfg, general_model, self.counter) if general_model is not None else None
        if self.objects is not None:
            self.engine.vehicle_seen = self.objects.vehicle_seen       # cause of a run
        self.line = AiLine() if ai is not None and ai_threaded else None
        self.periodic = Periodic(cfg, ai if periodic_ai else None, self.line) if periodic else None
        self.windows = WindowManager(cfg, ai, self.line) if windows else None
        self.dwell = DwellWatch(cfg, self.counter)
        self._pending: list[tuple[float, list[Any]]] = []          # (release_t, link messages)
        self._chunk_seq = CHUNK_SEQ_BASE
        self._msg_id = 0
        self.texts: list[dict[str, Any]] = []                       # every long text queued (flight log)
        self._t = None

    # -- link helpers -----------------------------------------------------------------------------
    def _queue_text(self, text: str, release_t: float, priority: int, severity: int, source: str) -> None:
        self._msg_id = self._msg_id % 65535 + 1
        chunks = make_chunks(text, msg_id=self._msg_id, priority=priority, severity=severity, source=source,
                             first_seq=self._chunk_seq)
        self._chunk_seq += len(chunks)
        self._pending.append((release_t, chunks))
        self.texts.append({"source": source, "release_t": round(release_t, 3), "text": text,
                           "chunks": len(chunks), "msg_id": chunks[0].msg_id})

    def _release(self, t: float) -> None:
        due = sorted((p for p in self._pending if p[0] <= t + 1e-9), key=lambda p: p[0])
        self._pending = [p for p in self._pending if p[0] > t + 1e-9]
        for rt, msgs in due:
            for m in msgs:
                # An AI result can be due a few ms before a message the link already took (S51 at
                # 1535.205 s after a message at 1535.236 s): the link refuses time going back, so it goes now.
                self.link.send(m, max(rt, self.link._last_t))

    # -- frames ------------------------------------------------------------------------------------
    def _take_ai(self, done) -> None:
        for job, res in done:
            if job.kind == "window":
                for txt in res.texts:
                    self._queue_text(txt, res.release_t, WINDOW_PRIORITY, MAV_SEVERITY_NOTICE, "window")
            else:
                self._queue_text(res.text, res.release_t, PERIODIC_PRIORITY, MAV_SEVERITY_INFO, "periodic")

    def push(self, frame: dict[str, Any], image=None) -> Snapshot | None:
        t = frame["t"]
        self._t = t
        if self.line is not None:
            self._take_ai(self.line.ready(t))
        self._release(t)
        self._new_animal = []
        snap = self.loop.push(frame)
        new_objects: list[ObjectAlert] = []
        self.identity.push(frame)
        animals = self.identity.live_detections()      # registered animals only, with their animal ids
        new_dwell = self.dwell.on_frame(t, animals)
        for a in new_dwell:
            self.link.send(a, t)
        if image is not None and self.objects is not None:
            new_objects = self.objects.on_frame(t, image, animals)
            for a in new_objects:
                self.link.send(a, t)
        if self.periodic is not None:
            for a in self._new_animal + new_objects + new_dwell:
                self.periodic.note_alert(a.text)
        if self.windows is not None and image is not None:
            for a in new_objects:
                self.windows.trigger(Trigger("VEHICLE", t, a.text, None, a.boxes[0].bbox_xywh_px,
                                             self._facts(t, None)))
            for a in self._new_animal:
                if a.status in WINDOW_STATUSES and (a.stage is None or a.stage >= 2):   # INJ1 only watches
                    self.windows.trigger(Trigger(WINDOW_STATUSES[a.status], t, a.text, a.track_id, None,
                                                 self._facts(t, a.track_id)))
            res = self.windows.on_frame(t, image, animals, self.objects.last_kept if self.objects else [])
            if res is not None:
                for txt in res.texts:
                    self._queue_text(txt, res.release_t, WINDOW_PRIORITY, MAV_SEVERITY_NOTICE, "window")
        if self.periodic is not None:
            r = self.periodic.maybe(t, self.loop.latest)
            if r is not None:
                self._queue_text(r.text, r.release_t, PERIODIC_PRIORITY, MAV_SEVERITY_INFO, "periodic")
        self._release(t)
        self.link.advance(t)
        return snap

    def _facts(self, t: float, track_id: int | None) -> dict[str, Any]:
        an = self.loop.latest or {"animals": []}
        rows = [{"track_id": a["track_id"], "species": a["species"], "status": a["status"]} for a in an["animals"]]
        for r, a in zip(rows, an["animals"]):
            if a.get("body_length_m") is not None:                      # the size the YOUNG rule used
                r["body_length_m"] = a["body_length_m"]
                r["adult_length_m"] = self.cfg.young.adult_m(a["species"])
        f = {"track_ids": [r["track_id"] for r in rows], "species": sorted({r["species"] for r in rows})}
        if track_id is not None:
            f["track"] = next((r for r in rows if r["track_id"] == track_id), {"track_id": track_id})
        return f

    def land(self) -> dict[str, Any]:
        final = self.loop.land()
        if self.windows is not None and self._t is not None:
            res = self.windows.close_unfinished(self._t)
            if res is not None:
                for txt in res.texts:
                    self._queue_text(txt, res.release_t, WINDOW_PRIORITY, MAV_SEVERITY_NOTICE, "window")
        if self.line is not None:            # results still on the AI line: wait for them, then stop it
            wait = self.cfg.window.timeout_s + self.cfg.periodic.timeout_s + 10.0
            self._take_ai(self.line.drain(wait))
            self.line.close()
        for rt, msgs in sorted(self._pending, key=lambda p: p[0]):
            for m in msgs:
                self.link.send(m, max(rt, self.link._last_t))
        self._pending = []
        self.link.flush()
        return final

    # -- after landing ----------------------------------------------------------------------------
    def flight_log(self, **more: Any) -> dict[str, Any]:
        """Everything the onboard side did, for the package (flight_log.json). more: extra sections
        (telemetry, movement, ...) given by the caller."""
        def delivery(d: Delivery) -> dict[str, Any]:
            a = d.alert
            return {"seq": a.seq, "source": getattr(a, "source", "object" if isinstance(a, ObjectAlert) else "alert"),
                    "priority": a.priority, "text": a.text, "msg_id": getattr(a, "msg_id", 0),
                    "chunk_seq": getattr(a, "chunk_seq", 0), "queued_t": round(d.queued_t, 3),
                    "start_t": round(d.start_t, 3), "delivered_t": round(d.delivered_t, 3), "bytes": d.bytes}
        ob = self.objects
        log: dict[str, Any] = {
            "schema_version": "hima.flight_log/1",
            "alerts": [{"seq": a.seq, "t": a.t, "track_id": a.track_id, "species": a.species, "status": a.status,
                        "priority": a.priority, "stage": a.stage, "still_alone_s": a.still_alone_s,
                        "cause": a.cause, "text": a.text} for a in self.alerts],
            "cause_vehicle_checked": self.engine.vehicle_seen is not None,
            "dwell": {"alerts": [{"seq": a.seq, "t": a.t, "track_id": a.track_id, "species": a.species,
                                  "stay_s": a.stay_s, "text": a.text} for a in self.dwell.alerts],
                      "held": [list(h) for h in self.dwell.held]},
            "held_alerts": [list(h) for h in self.engine.held],
            "objects": None if ob is None else {
                "alerts": [{"seq": a.seq, "t": a.t, "text": a.text, "count": a.count, "run_start_t": a.run_start_t,
                            "boxes": [b.to_dict() for b in a.boxes]} for a in ob.alerts],
                "held": [list(h) for h in ob.held], "samples": ob.samples,
                "compute_s_mean": round(sum(ob.compute_s) / len(ob.compute_s), 4) if ob.compute_s else None},
            "periodic": None if self.periodic is None else {
                "results": [{"n": r.n, "t": r.t, "release_t": round(r.release_t, 3), "kind": r.kind, "text": r.text,
                             "rejected": [[s, list(e)] for s, e in r.rejected], "latency_s": r.latency_s,
                             "error": r.error, "facts": r.facts} for r in self.periodic.results],
                "skipped_busy": self.periodic.skipped_busy},
            "windows": None if self.windows is None else {"access_log": self.windows.log,
                                                          "refused": self.windows.refused},
            "ai_calls": [] if self.ai is None else self.ai.calls,
            "ai_line": None if self.line is None else {"threaded": self.line.threaded, "jobs": self.line.log},
            "texts": self.texts,
            "link": {"bytes_per_s": self.link.bytes_per_s, "deliveries": [delivery(d) for d in self.deliveries]},
        }
        log.update(more)
        return log

    def images(self) -> list[dict[str, Any]]:
        """Images for the package: one per vehicle alert, and every image shown to the AI in a window."""
        out = [{"purpose": "vehicle_alert", "t": a.t, "jpeg": a.image_jpeg, "alert": a.text, "window": None,
                "track_ids": []} for a in self.object_alerts if a.image_jpeg]
        for w in (self.windows.windows if self.windows is not None else []):
            for t, jpg, boxes in zip(w.image_t, w.images, w.image_boxes):
                out.append({"purpose": "window", "t": t, "jpeg": jpg, "alert": w.trigger.alert_text, "window": w.n,
                            "track_ids": [b["track_id"] for b in boxes if "track_id" in b]})
        return out

    @property
    def alerts(self) -> list[Alert]:
        return self.engine.alerts

    @property
    def object_alerts(self) -> list[ObjectAlert]:
        return self.objects.alerts if self.objects is not None else []

    @property
    def deliveries(self) -> list[Delivery]:
        return self.link.delivered
