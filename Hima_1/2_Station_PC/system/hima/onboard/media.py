"""Cut event clips and still frames out of the mission video, in ONE sequential pass.

Frames are read in order (no seeking: seeking is not frame-exact for every codec) and
written with OpenCV VideoWriter. Which codecs exist depends on how OpenCV was built
(opencv2/videoio.hpp, VideoWriter docs, checked 2026-11-13), so every writer is checked
with isOpened() and every clip is read back and its frames counted.
Audio is not copied.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

EPS = 1e-9


@dataclass(frozen=True)
class VideoInfo:
    fps: float
    frames: int        # as reported by the container (may be approximate)
    width_px: int
    height_px: int


def video_info(video: str | Path) -> VideoInfo:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise OSError(f"cannot open video {video}")
    try:
        return VideoInfo(cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
                         int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    finally:
        cap.release()


def check_session_matches_video(doc: dict[str, Any], info: VideoInfo) -> None:
    """record-video gives frame k the time k / fps. Anything else = wrong video for this session."""
    cam = doc["meta"]["camera"]
    if (cam["width_px"], cam["height_px"]) != (info.width_px, info.height_px):
        raise ValueError(f"video is {info.width_px}x{info.height_px}, session camera is "
                         f"{cam['width_px']}x{cam['height_px']}")
    for k, fr in enumerate(doc["frames"]):
        if abs(fr["t"] - k / info.fps) > 1e-6:
            raise ValueError(f"session frame {k} has t = {fr['t']}, the video gives {k / info.fps} "
                             f"({info.fps} fps): this session was not recorded from this video")


def check_video_is_session_frames(doc: dict[str, Any], video: str | Path, info: VideoInfo) -> None:
    """Live flight (hima/fly.py): the video holds exactly one frame per session frame, in order, at uneven times
    (frames are taken when YOLO is ready). Video frame k = session frame k; counted, not trusted from the header."""
    cam = doc["meta"]["camera"]
    if (cam["width_px"], cam["height_px"]) != (info.width_px, info.height_px):
        raise ValueError(f"video is {info.width_px}x{info.height_px}, session camera is "
                         f"{cam['width_px']}x{cam['height_px']}")
    n = count_frames(video)
    if n != len(doc["frames"]):
        raise ValueError(f"video has {n} frames, the session {len(doc['frames'])}: not this flight's video")


def frame_range_by_times(times: list[float], t_start: float, t_end: float) -> tuple[int, int]:
    """Session frames k with t_start <= times[k] <= t_end (times ascending)."""
    a = bisect.bisect_left(times, t_start - EPS)
    b = bisect.bisect_right(times, t_end + EPS) - 1
    return a, b


def frame_range(t_start: float, t_end: float, fps: float) -> tuple[int, int]:
    """Frames k with t_start <= k / fps <= t_end."""
    return math.ceil(t_start * fps - EPS), math.floor(t_end * fps + EPS)


def count_frames(video: str | Path) -> int:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise OSError(f"cannot open {video}")
    n = 0
    try:
        while cap.grab():
            n += 1
    finally:
        cap.release()
    return n


def extract(video: str | Path, out_dir: Path, *, clips: dict[str, tuple[int, int]],
            stills: dict[str, int], fps: float, fourcc: str, jpeg_quality: int) -> dict[str, int]:
    """clips: relative path -> (first, last) frame (inclusive); stills: relative path -> frame.
    Returns frames written per clip (and 1 per still). Raises if a frame cannot be read or written."""
    if not clips and not stills:
        return {}
    last_needed = max([b for _, b in clips.values()] + list(stills.values()))
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise OSError(f"cannot open video {video}")
    writers: dict[str, cv2.VideoWriter] = {}
    written = {name: 0 for name in clips}
    try:
        for k in range(last_needed + 1):
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f"{video}: frame {k} could not be read (needed up to {last_needed})")
            for name, (a, b) in clips.items():
                if a <= k <= b:
                    if name not in writers:
                        p = out_dir / name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        h, w = frame.shape[:2]
                        wr = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
                        if not wr.isOpened():
                            raise OSError(f"OpenCV cannot write {p} with fourcc {fourcc!r} on this build")
                        writers[name] = wr
                    writers[name].write(frame)
                    written[name] += 1
                    if k == b:
                        writers.pop(name).release()
            for name, kk in stills.items():
                if kk == k:
                    p = out_dir / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]):
                        raise OSError(f"cannot write {p}")
    finally:
        for wr in writers.values():
            wr.release()
        cap.release()
    for name, (a, b) in clips.items():
        n = count_frames(out_dir / name)
        if n != b - a + 1 or written[name] != n:
            raise ValueError(f"clip {name}: expected {b - a + 1} frames, wrote {written[name]}, read back {n}")
    return {**written, **{name: 1 for name in stills}}
