"""Pixel <-> ground geometry for a nadir (straight-down) camera over flat ground.

Conventions
-----------
Image:   u to the right, v downward, origin at the top-left pixel corner.
Camera:  offset from the point under the drone, in metres: dx to the image right,
         dy toward the image top ("forward").
World:   local ENU plane: x east, y north (metres).
heading_deg: compass direction the image top points to (0 = north, 90 = east, clockwise).

Assumptions (written to meta.geometry_assumptions): flat_ground, nadir_camera,
square pixels (same metres-per-pixel on both axes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Camera:
    width_px: int
    height_px: int
    hfov_deg: float

    @classmethod
    def from_meta(cls, cam: dict[str, Any]) -> "Camera":
        return cls(cam["width_px"], cam["height_px"], cam["hfov_deg"])


@dataclass(frozen=True)
class DronePose:
    x_m: float
    y_m: float
    agl_m: float
    heading_deg: float

    @classmethod
    def from_frame(cls, drone: dict[str, Any]) -> "DronePose":
        return cls(drone["x_m"], drone["y_m"], drone["agl_m"], drone["heading_deg"])


@dataclass(frozen=True)
class GroundPoint:
    x_m: float        # world east
    y_m: float        # world north
    dx_m: float       # camera right offset from the nadir point
    dy_m: float       # camera forward offset from the nadir point
    slant_m: float    # straight-line distance drone -> animal


def footprint_width_m(agl_m: float, hfov_deg: float) -> float:
    """Ground width covered by the image: 2 * agl * tan(hfov / 2)."""
    if not agl_m > 0:
        raise ValueError(f"agl_m must be > 0, got {agl_m}")
    if not 0 < hfov_deg < 180:
        raise ValueError(f"hfov_deg must be in (0, 180), got {hfov_deg}")
    return 2.0 * agl_m * math.tan(math.radians(hfov_deg) / 2.0)


def gsd_m_per_px(agl_m: float, hfov_deg: float, width_px: int) -> float:
    """Ground sampling distance (metres per pixel)."""
    return footprint_width_m(agl_m, hfov_deg) / width_px


def camera_to_world_offset(dx_m: float, dy_m: float, heading_deg: float) -> tuple[float, float]:
    """Rotate a camera offset (right, forward) into world (east, north)."""
    h = math.radians(heading_deg)
    c, s = math.cos(h), math.sin(h)
    return dx_m * c + dy_m * s, -dx_m * s + dy_m * c


def world_to_camera_offset(east_m: float, north_m: float, heading_deg: float) -> tuple[float, float]:
    """Inverse of camera_to_world_offset."""
    h = math.radians(heading_deg)
    c, s = math.cos(h), math.sin(h)
    return east_m * c - north_m * s, east_m * s + north_m * c


def pixel_to_ground(u: float, v: float, cam: Camera, pose: DronePose) -> GroundPoint:
    g = gsd_m_per_px(pose.agl_m, cam.hfov_deg, cam.width_px)
    dx = (u - cam.width_px / 2.0) * g
    dy = (cam.height_px / 2.0 - v) * g
    east, north = camera_to_world_offset(dx, dy, pose.heading_deg)
    return GroundPoint(pose.x_m + east, pose.y_m + north, dx, dy,
                       math.sqrt(dx * dx + dy * dy + pose.agl_m * pose.agl_m))


def ground_to_pixel(x_m: float, y_m: float, cam: Camera, pose: DronePose) -> tuple[float, float]:
    """World point -> pixel (u, v). Inverse of pixel_to_ground (used by the synthetic source)."""
    g = gsd_m_per_px(pose.agl_m, cam.hfov_deg, cam.width_px)
    dx, dy = world_to_camera_offset(x_m - pose.x_m, y_m - pose.y_m, pose.heading_deg)
    return cam.width_px / 2.0 + dx / g, cam.height_px / 2.0 - dy / g
