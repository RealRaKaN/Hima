"""The AI's own line: the YOLO line never waits for a model.

The YOLO line (camera -> tracker -> onboard analysis -> fixed alerts -> link) runs from the reserve entry to
the return home without a stop. When the periodic analysis or a camera window needs the AI, that line hands
a JOB to this line (the prompt, and copies of the images it already took) and goes on with the next frame.
One worker thread runs the jobs one at a time (one model call at a time, as before), camera windows before
periodic analyses. The YOLO line collects a finished job at the first frame where

    the job is done  and  t >= submit_t + queue_wait_s + call_latency_s

and only then reads the reply and queues the message (on a real flight mission time is wall time, so this
is "as soon as it is done"; in the slower-than-real-time simulation it keeps the message at the time a real
flight would allow, as before). Reading the reply (checks, message) runs on the YOLO line, so the managers'
state is touched by one thread only; the worker only waits for the model.

threaded=False runs each job at once, inside submit (the old behaviour): unit tests that need exact frames.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# call() -> (raw text | None, call latency s, error | None), as AiGateway.text/vision
Call = Callable[[], tuple[str | None, float, str | None]]
WINDOW, PERIODIC = 0, 1                       # job priority: lower runs first


@dataclass
class Job:
    n: int
    kind: str                                  # "window" | "periodic"
    t: float                                   # mission time it was handed over
    priority: int
    call: Call
    finish: Callable[[str | None, float | None, str | None, float], Any]   # raw, latency, err, queue_wait
    submitted: float = 0.0                     # wall clock
    started: float | None = None
    finished: float | None = None
    raw: str | None = None
    latency_s: float | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def queue_wait_s(self) -> float:
        return 0.0 if self.started is None else max(0.0, self.started - self.submitted)

    @property
    def release_t(self) -> float:
        return self.t + self.queue_wait_s + (self.latency_s or 0.0)


class AiLine:
    def __init__(self, threaded: bool = True, clock: Callable[[], float] = time.perf_counter):
        self.threaded = threaded
        self.clock = clock
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._pending: list[Job] = []
        self._thread: threading.Thread | None = None
        self.log: list[dict[str, Any]] = []    # one row per collected job

    # -- YOLO line side --------------------------------------------------------------------------
    def submit(self, kind: str, t: float, priority: int, call: Call,
               finish: Callable[[str | None, float | None, str | None, float], Any]) -> Job:
        job = Job(len(self.log) + len(self._pending) + 1, kind, t, priority, call, finish, self.clock())
        self._pending.append(job)
        if not self.threaded:
            self._run(job)
        else:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="hima-ai-line", daemon=True)
                self._thread.start()
            self._q.put((priority, next(self._seq), job))
        return job

    def busy(self, kind: str | None = None) -> bool:
        return any(kind is None or j.kind == kind for j in self._pending)

    def ready(self, t: float) -> list[tuple[Job, Any]]:
        """Jobs done whose message a real flight would have by t; their finish() runs here (YOLO line)."""
        due = [j for j in self._pending if j.done.is_set() and j.release_t <= t + 1e-9]
        return self._collect(due)

    def drain(self, timeout_s: float) -> list[tuple[Job, Any]]:
        """Landing: wait (at most timeout_s) for every job handed over, then collect them all."""
        end = self.clock() + timeout_s
        for j in list(self._pending):
            j.done.wait(max(0.0, end - self.clock()))
        return self._collect([j for j in self._pending if j.done.is_set()])

    def close(self) -> None:
        if self._thread is not None:
            self._q.put((99, next(self._seq), None))
            self._thread.join(timeout=5.0)
            self._thread = None

    def _collect(self, due: list[Job]) -> list[tuple[Job, Any]]:
        out = []
        for j in sorted(due, key=lambda j: (j.release_t, j.n)):
            self._pending.remove(j)
            res = j.finish(j.raw, j.latency_s, j.error, j.queue_wait_s)
            self.log.append({"n": j.n, "kind": j.kind, "t": round(j.t, 3), "queue_wait_s": round(j.queue_wait_s, 3),
                             "latency_s": None if j.latency_s is None else round(j.latency_s, 3),
                             "release_t": round(j.release_t, 3), "error": j.error})
            out.append((j, res))
        return out

    # -- AI line side ----------------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            _, _, job = self._q.get()
            if job is None:
                return
            self._run(job)

    def _run(self, job: Job) -> None:
        job.started = self.clock()
        try:
            job.raw, job.latency_s, job.error = job.call()
        except Exception as e:                  # noqa: BLE001 - a failed call is a result, never a crash
            job.raw, job.latency_s, job.error = None, self.clock() - job.started, f"{type(e).__name__}: {e}"[:300]
        job.finished = self.clock()
        job.done.set()
