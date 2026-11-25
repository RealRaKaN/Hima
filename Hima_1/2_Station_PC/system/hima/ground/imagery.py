"""Satellite images under the station's route map: kept on the station, offline.

    python -m hima imagery add IMAGE --bounds SOUTH,WEST,NORTH,EAST [--crs 4326|3857] --source "..." --license "..."
    python -m hima imagery add IMAGE --world IMAGE.jgw --crs 3857 ...          (a world file instead of --bounds)
    python -m hima imagery list

Each image is stored in [route_review].imagery_dir as <name>.jpg plus <name>.json (bounds in degrees, source,
licence, SHA-256 of the original file). The map is drawn in Web Mercator, so an image in EPSG:4326 (equal steps of
latitude) is resampled row by row to Web Mercator when it is added (over the reserve's 1.4 degrees of latitude the two
differ by up to ~1 km: drawn without it, the route would sit beside its place on the image). An image that does not
sit right on the route can be switched off on the map ("images").

images_for(area) gives the dashboard each stored image cut to the area around a route and made smaller (max_px), as
data URLs: the map page needs no file server. The images (and their sources' terms) stay on the station: never in git.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

R = 6378137.0
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def merc_y(lat: float | np.ndarray):
    return R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def lat_of(y: float | np.ndarray):
    return np.degrees(2 * np.arctan(np.exp(np.asarray(y) / R)) - np.pi / 2)


def merc_x(lon):
    return R * np.radians(lon)


def lon_of(x):
    return np.degrees(np.asarray(x) / R)


def read_world(path: str | Path, w: int, h: int, crs: int) -> tuple[float, float, float, float]:
    """A world file (6 lines: x size, 0, 0, -y size, x of the first pixel's centre, y of it) -> S, W, N, E degrees."""
    v = [float(x) for x in Path(path).read_text(encoding="utf-8").split()[:6]]
    if abs(v[1]) > 1e-12 or abs(v[2]) > 1e-12:
        raise ValueError("rotated world files are not supported")
    x0, y0 = v[4] - v[0] / 2, v[5] - v[3] / 2            # outer corner of the first pixel
    x1, y1 = x0 + v[0] * w, y0 + v[3] * h
    if crs == 3857:
        return (float(lat_of(min(y0, y1))), float(lon_of(min(x0, x1))), float(lat_of(max(y0, y1))),
                float(lon_of(max(x0, x1))))
    return min(y0, y1), min(x0, x1), max(y0, y1), max(x0, x1)


def to_mercator_rows(img: np.ndarray, south: float, north: float) -> np.ndarray:
    """EPSG:4326 rows (equal latitude steps) -> Web Mercator rows (equal merc-y steps), same height."""
    import cv2
    h, w = img.shape[:2]
    ys = np.linspace(merc_y(north), merc_y(south), h)     # top to bottom, centres approximately
    lats = lat_of(ys)
    src_rows = (north - lats) / (north - south) * (h - 1)
    map_y = np.repeat(src_rows[:, None], w, axis=1).astype(np.float32)
    map_x = np.repeat(np.arange(w, dtype=np.float32)[None, :], h, axis=0)
    return cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR)


def add_image(src: str | Path, dest_dir: str | Path, bounds: tuple[float, float, float, float], crs: int,
              source: str, licence: str, name: str | None = None, quality: int = 85) -> dict[str, Any]:
    import cv2
    src = Path(src)
    s, w_, n, e = bounds
    if not (-85 < s < n < 85 and -180 <= w_ < e <= 180):
        raise ValueError(f"bounds must be south < north, west < east in degrees, got {bounds}")
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"{src}: not an image OpenCV can read")
    if crs == 4326:
        img = to_mercator_rows(img, s, n)
    elif crs != 3857:
        raise ValueError("crs must be 4326 or 3857")
    name = name or src.stem
    if not NAME.match(name):
        raise ValueError(f"name {name!r}: letters, digits, _ . - only")
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise OSError("cannot encode the image")
    (d / f"{name}.jpg").write_bytes(buf.tobytes())
    meta = {"schema_version": "hima.imagery/1", "name": name, "bounds_deg": [s, w_, n, e], "stored_crs": "EPSG:3857",
            "added_from_crs": f"EPSG:{crs}", "width_px": int(img.shape[1]), "height_px": int(img.shape[0]),
            "source": source, "licence": licence, "original": src.name,
            "original_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
            "m_per_px": round((merc_x(e) - merc_x(w_)) / img.shape[1] * math.cos(math.radians((s + n) / 2)), 2)}
    (d / f"{name}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta


def list_images(dest_dir: str | Path) -> list[dict[str, Any]]:
    d = Path(dest_dir)
    out = []
    for j in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            m = json.loads(j.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if m.get("schema_version") == "hima.imagery/1" and (d / f"{m['name']}.jpg").is_file():
            out.append(m)
    return out


def images_for(dest_dir: str | Path, area: tuple[float, float, float, float], max_px: int = 2400,
               quality: int = 80) -> list[dict[str, Any]]:
    """Each stored image cut to area (S, W, N, E degrees) and scaled to at most max_px: [{src, bounds, name}]."""
    import cv2
    s, w_, n, e = area
    out = []
    for m in list_images(dest_dir):
        bs, bw, bn, be = m["bounds_deg"]
        cs, cw, cn, ce = max(s, bs), max(w_, bw), min(n, bn), min(e, be)
        if cs >= cn or cw >= ce:
            continue
        img = cv2.imread(str(Path(dest_dir) / f"{m['name']}.jpg"), cv2.IMREAD_COLOR)
        if img is None:
            continue
        h, w = img.shape[:2]
        X = lambda lon: (merc_x(lon) - merc_x(bw)) / (merc_x(be) - merc_x(bw)) * w            # noqa: E731
        Y = lambda lat: (merc_y(bn) - merc_y(lat)) / (merc_y(bn) - merc_y(bs)) * h            # noqa: E731
        x0, x1 = int(max(0, math.floor(X(cw)))), int(min(w, math.ceil(X(ce))))
        y0, y1 = int(max(0, math.floor(Y(cn)))), int(min(h, math.ceil(Y(cs))))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        crop = img[y0:y1, x0:x1]
        k = min(1.0, max_px / max(crop.shape[:2]))
        if k < 1.0:
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * k)), max(1, int(crop.shape[0] * k))),
                              interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            continue
        # the crop's own bounds (whole pixels), back to degrees
        lon0 = float(lon_of(merc_x(bw) + x0 / w * (merc_x(be) - merc_x(bw))))
        lon1 = float(lon_of(merc_x(bw) + x1 / w * (merc_x(be) - merc_x(bw))))
        lat0 = float(lat_of(merc_y(bn) - y1 / h * (merc_y(bn) - merc_y(bs))))
        lat1 = float(lat_of(merc_y(bn) - y0 / h * (merc_y(bn) - merc_y(bs))))
        out.append({"name": m["name"], "bounds": [lat0, lon0, lat1, lon1], "source": m["source"],
                    "licence": m["licence"],
                    "src": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")})
    return out
