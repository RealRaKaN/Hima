"""The drone camera for a live flight: one thread reads frames, the flight takes the newest one.

[camera].device:
  "0", "1", ...        a V4L2 / USB camera index (/dev/video0 ...); asked for width_px x height_px in MJPG
  "/dev/video0"        a device path
  "... ! appsink"      a GStreamer pipeline (has "!"), e.g. a CSI camera on the Jetson:
                       "nvarguscamerasrc ! video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1 ! nvvidconv !
                        video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink drop=1"
  "flight.mp4"         a video file (bench tests): read at its own frame rate, as a camera would deliver it

A camera delivers frames whether or not YOLO is ready; the reader thread keeps only the newest frame, so a slow
frame never builds a backlog (the next frame YOLO gets is the current view, not an old one). frames_read and
frames_taken show how many were dropped.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np


class CameraError(OSError):
    pass


class Camera:
    def __init__(self, device: str, width_px: int, height_px: int, *, open_timeout_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.device = str(device)
        self.clock = clock
        self.is_file = Path(self.device).is_file()
        if self.device.isdigit():
            api = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(int(self.device), api)
        elif "!" in self.device:
            self.cap = cv2.VideoCapture(self.device, cv2.CAP_GSTREAMER)
        else:
            self.cap = cv2.VideoCapture(self.device)
        if not self.cap.isOpened():
            raise CameraError(f"cannot open camera {self.device!r}")
        if not self.is_file and "!" not in self.device:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))   # USB 720p/1080p at full rate
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width_px)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height_px)
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.source_fps = fps if fps and fps > 0 else None
        if self.is_file and self.source_fps is None:
            self.cap.release()
            raise CameraError(f"{self.device}: frame rate unknown")
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._n = 0                         # frames read from the device
        self._wall = 0.0
        self.taken = 0
        self.ended = False                  # a video file reached its end, or the camera stopped
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hima-camera", daemon=True)
        self._thread.start()
        end = self.clock() + open_timeout_s
        while self._n == 0 and not self.ended and self.clock() < end:
            time.sleep(0.01)
        if self._n == 0:
            self.close()
            raise CameraError(f"camera {self.device!r} gave no frame in {open_timeout_s:g} s"
                              + (f" ({self.error})" if self.error else ""))
        h, w = self._frame.shape[:2]
        self.width_px, self.height_px = int(w), int(h)

    def _run(self) -> None:
        t0 = time.monotonic()
        k = 0
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if not ok:
                self.ended = True
                self.error = self.error or ("end of file" if self.is_file else "read failed")
                return
            with self._lock:
                self._frame, self._n, self._wall = frame, self._n + 1, self.clock()
            k += 1
            if self.is_file:                # deliver at the file's own rate, like a camera
                time.sleep(max(0.0, t0 + k / self.source_fps - time.monotonic()))

    def latest(self, after: int = 0) -> tuple[int, float, np.ndarray] | None:
        """(frame number, wall time read, image) of the newest frame if it is newer than `after`, else None."""
        with self._lock:
            if self._n <= after or self._frame is None:
                return None
            self.taken += 1
            return self._n, self._wall, self._frame

    @property
    def frames_read(self) -> int:
        return self._n

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2.0)
        self.cap.release()
