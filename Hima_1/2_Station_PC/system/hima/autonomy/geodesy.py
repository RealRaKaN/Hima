"""Latitude / longitude <-> local east / north metres over a whole reserve (about 150 km across).

telemetry/geo.py is a flat-earth projection for the few kilometres of one camera view; over 150 km its
east scale drifts by about 1 %. Here: azimuthal equidistant projection around the reserve's centre, on
the sphere that best fits the WGS84 ellipsoid at that latitude (radius sqrt(M * N)). Distances from the
centre are exact on that sphere; over 100 km the error against the ellipsoid stays well under 0.1 %.
"""
from __future__ import annotations

import math

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


def gauss_radius_m(lat_deg: float) -> float:
    """sqrt(M * N): the radius of the sphere that touches the ellipsoid best at this latitude."""
    s2 = math.sin(math.radians(lat_deg)) ** 2
    w = 1 - WGS84_E2 * s2
    m = WGS84_A * (1 - WGS84_E2) / w ** 1.5
    n = WGS84_A / math.sqrt(w)
    return math.sqrt(m * n)


class LocalFrame:
    """East / north metres around (lat0, lon0)."""

    def __init__(self, lat0_deg: float, lon0_deg: float) -> None:
        self.lat0 = float(lat0_deg)
        self.lon0 = float(lon0_deg)
        self.r = gauss_radius_m(self.lat0)
        self._p0 = math.radians(self.lat0)
        self._l0 = math.radians(self.lon0)

    def to_xy(self, lat_deg: float, lon_deg: float) -> tuple[float, float]:
        p, dl = math.radians(lat_deg), math.radians(lon_deg) - self._l0
        sp0, cp0, sp, cp = math.sin(self._p0), math.cos(self._p0), math.sin(p), math.cos(p)
        cos_c = max(-1.0, min(1.0, sp0 * sp + cp0 * cp * math.cos(dl)))
        c = math.acos(cos_c)
        k = 1.0 if c < 1e-12 else c / math.sin(c)
        return (self.r * k * cp * math.sin(dl),
                self.r * k * (cp0 * sp - sp0 * cp * math.cos(dl)))

    def to_latlon(self, x_m: float, y_m: float) -> tuple[float, float]:
        rho = math.hypot(x_m, y_m)
        if rho < 1e-9:
            return self.lat0, self.lon0
        c = rho / self.r
        sc, cc = math.sin(c), math.cos(c)
        sp0, cp0 = math.sin(self._p0), math.cos(self._p0)
        p = math.asin(max(-1.0, min(1.0, cc * sp0 + y_m * sc * cp0 / rho)))
        lam = self._l0 + math.atan2(x_m * sc, rho * cp0 * cc - y_m * sp0 * sc)
        return math.degrees(p), (math.degrees(lam) + 540.0) % 360.0 - 180.0
