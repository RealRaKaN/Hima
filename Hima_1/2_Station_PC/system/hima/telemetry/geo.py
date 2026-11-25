"""Latitude / longitude <-> local east / north metres around a home point.

Equirectangular (flat-earth) projection on a sphere of the WGS84 equatorial radius: fine for the few
kilometres of one mission (the error grows with the square of the distance; at 2 km it is on the order of
centimetres, below GPS noise). Not for long distances or near the poles.
"""
from __future__ import annotations

import math

EARTH_R_M = 6378137.0


def to_local(lat_deg: float, lon_deg: float, lat0_deg: float, lon0_deg: float) -> tuple[float, float]:
    """(east_m, north_m) of a point relative to home."""
    north = math.radians(lat_deg - lat0_deg) * EARTH_R_M
    east = math.radians(lon_deg - lon0_deg) * EARTH_R_M * math.cos(math.radians(lat0_deg))
    return east, north


def to_latlon(east_m: float, north_m: float, lat0_deg: float, lon0_deg: float) -> tuple[float, float]:
    lat = lat0_deg + math.degrees(north_m / EARTH_R_M)
    lon = lon0_deg + math.degrees(east_m / (EARTH_R_M * math.cos(math.radians(lat0_deg))))
    return lat, lon
