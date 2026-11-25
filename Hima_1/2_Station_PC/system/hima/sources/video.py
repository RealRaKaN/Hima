"""Video file -> YOLO + tracker -> FlightMissionRecorder -> session file.

A plain video has no drone telemetry and no camera calibration, so the session says
params_known = false. The drone pose and hfov written in the file are PLACEHOLDERS
(the schema needs values); the analysis then skips every metric value and only
visibility is computed. Pixel boxes are real.

Tracking follows the documented Ultralytics pattern: an OpenCV loop that calls
model.track(frame, persist=True) on consecutive frames.
https://docs.ultralytics.com/modes/track/ (checked 2026-11-12)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

from hima.config import Config
from hima.sources.recorder import FlightMissionRecorder, RawDetection

PLACEHOLDER_HFOV_DEG = 90.0
PLACEHOLDER_POSE = {"x_m": 0.0, "y_m": 0.0, "agl_m": 1.0, "heading_deg": 0.0}


@dataclass
class VideoStats:
    frames: int
    detections: int          # written (tracked) detections
    untracked: int
    video_fps: float
    load_s: float            # model load (not part of the per-frame rate)
    first_frame_s: float     # includes warm-up
    loop_s: float            # all frames: decode + track + record
    fps_after_first: float | None


def weights_path(cfg: Config, override: str | None = None) -> Path:
    p = Path(override or cfg.detector.weights)
    if not p.is_absolute() and cfg.source:
        p = Path(cfg.source).parent / p          # relative to hima.toml, not to the CWD
    return p


def model_imgsz(cfg: Config, frame_width_px: int) -> int:
    """Model input size: [detector].imgsz, or with 0 the frame's own width rounded up to a multiple of 32
    (the frame is not shrunk)."""
    return cfg.detector.imgsz or -(-int(frame_width_px) // 32) * 32


def check_class_names(names: dict[int, str], class_map: dict[str, str]) -> None:
    missing = sorted(set(names.values()) - set(class_map))
    if missing:
        raise ValueError(f"model classes {missing} are not in [detector.class_map] {sorted(class_map)}")


def _detections(result: Any, names: dict[int, str]) -> list[RawDetection]:
    b = result.boxes
    if b is None or len(b) == 0:
        return []
    ids = b.id.int().tolist() if b.is_track else [None] * len(b)
    return [RawDetection(tid, names[int(c)], float(cf), tuple(float(v) for v in xywh))
            for tid, c, cf, xywh in zip(ids, b.cls.tolist(), b.conf.tolist(), b.xywh.tolist())]


class VideoSource:
    """A camera stand-in: next_frame() reads ONE frame, runs the tracker on it and records it.
    record_video() reads a whole file with it; the end-to-end run (hima/e2e.py) calls it frame
    by frame and feeds each frame to the onboard loop as it arrives."""

    def __init__(self, video: str | Path, cfg: Config, *, model: Any = None, weights: str | None = None):
        self.video = Path(video)
        self.cfg = cfg
        self.cap = cv2.VideoCapture(str(self.video))
        if not self.cap.isOpened():
            raise OSError(f"cannot open video {self.video}")
        try:
            self.fps = self.cap.get(cv2.CAP_PROP_FPS)
            w, h = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if not self.fps or self.fps <= 0:
                raise ValueError(f"{self.video}: frame rate unknown ({self.fps}); cannot give frames a time")

            t0 = time.perf_counter()
            wpath = weights_path(cfg, weights)
            if model is None:
                if not wpath.is_file():
                    raise OSError(f"weights not found: {wpath}")
                from ultralytics import YOLO          # heavy import only when really needed
                model = YOLO(str(wpath))
            self.load_s = time.perf_counter() - t0
            self.model = model
            self.names = dict(model.names)
            check_class_names(self.names, cfg.detector.class_map)
            self.imgsz = model_imgsz(cfg, w)

            self.rec = FlightMissionRecorder(
                mission_id=f"video-{self.video.stem}",
                source="video",
                camera={"width_px": w, "height_px": h, "hfov_deg": PLACEHOLDER_HFOV_DEG, "mount": "nadir",
                        "params_known": False},
                detector={"weights": wpath.name, "class_map": dict(cfg.detector.class_map),
                          "conf": cfg.detector.conf, "tracker": cfg.detector.tracker, "imgsz": self.imgsz},
                geometry_assumptions=(),
            )
        except BaseException:
            self.cap.release()
            raise
        self.k = 0

    def next_frame(self) -> dict[str, Any] | None:
        """Read, track and record the next frame; None at the end of the video."""
        ok, frame = self.cap.read()
        if not ok:
            return None
        result = self.model.track(frame, persist=True, conf=self.cfg.detector.conf,
                                  tracker=self.cfg.detector.tracker, imgsz=self.imgsz, verbose=False)[0]
        self.rec.add_frame(self.k / self.fps, PLACEHOLDER_POSE, _detections(result, self.names))
        self.k += 1
        return self.rec.frames[-1]

    def close(self) -> None:
        self.cap.release()


def record_video(video: str | Path, cfg: Config, *, model: Any = None, weights: str | None = None,
                 max_frames: int | None = None) -> tuple[dict[str, Any], VideoStats, FlightMissionRecorder]:
    src = VideoSource(video, cfg, model=model, weights=weights)
    try:
        k, first_s = 0, 0.0
        t_loop = time.perf_counter()
        while max_frames is None or k < max_frames:
            ts = time.perf_counter()
            if src.next_frame() is None:
                break
            if k == 0:
                first_s = time.perf_counter() - ts
            k += 1
        loop_s = time.perf_counter() - t_loop
    finally:
        src.close()
    if k == 0:
        raise ValueError(f"{src.video}: no frames could be read")
    rec = src.rec
    rest = loop_s - first_s
    stats = VideoStats(frames=k, detections=sum(len(f["detections"]) for f in rec.frames),
                       untracked=rec.skipped_untracked, video_fps=src.fps,
                       load_s=src.load_s, first_frame_s=first_s, loop_s=loop_s,
                       fps_after_first=(k - 1) / rest if k > 1 and rest > 0 else None)
    return rec.to_session(), stats, rec
