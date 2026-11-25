"""The station dashboard: a small web server on this PC only (localhost), no outside files (the station in the field
has no internet). Python's own http.server: JSON from hima.ground.web.api, the archived files (path-checked, with
HTTP ranges), the event clips as JPEG frames (the drone writes mp4v, which browsers do not play), and the Route page's
answers.

    python -m hima dashboard --archive ground_archive [--port 8501] [--open]
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
import traceback
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from hima.config import Config
from hima.ground.plans import PlanStore, valid_id
from hima.ground.web import api

STATIC = Path(__file__).with_name("static")
ROUTE_MAP = Path(__file__).resolve().parents[1] / "route_map" / "index.html"
MISSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
ITEM_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
CLIP_W = 960
CLIP_CACHE = 6
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


class Clips:
    """Decoded event clips, as JPEG frames (a few clips kept in memory)."""

    def __init__(self):
        self._c: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path) -> dict[str, Any] | None:
        key = f"{path}:{path.stat().st_mtime_ns}"
        with self._lock:
            if key in self._c:
                self._c.move_to_end(key)
                return self._c[key]
        import cv2
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
        w0, h0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames = []
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if img.shape[1] > CLIP_W:
                img = cv2.resize(img, (CLIP_W, int(img.shape[0] * CLIP_W / img.shape[1])), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                frames.append(buf.tobytes())
        cap.release()
        if not frames:
            return None
        doc = {"frames": frames, "fps": fps, "w": w0, "h": h0}
        with self._lock:
            self._c[key] = doc
            while len(self._c) > CLIP_CACHE:
                self._c.popitem(last=False)
        return doc


class App:
    def __init__(self, archive: Path, cfg: Config):
        from hima.onboard.deliver import config_path
        self.archive = Path(archive)
        self.cfg = cfg
        self.plans = PlanStore(config_path(cfg, cfg.route_review.plans_dir))
        self.imagery = config_path(cfg, cfg.route_review.imagery_dir)
        self.clips = Clips()


def _bool(q: dict[str, list[str]], name: str) -> bool | None:
    v = (q.get(name) or [""])[0]
    return True if v == "1" else False if v == "0" else None


def _inside(root: Path, rel: str) -> Path | None:
    """root/rel when it stays inside root and is a file; None otherwise."""
    try:
        p = (root / rel).resolve()
        p.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return p if p.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "HimaStation/1"
    app: App

    def log_message(self, fmt, *args):                       # quiet: the station's console is for the receiver
        pass

    # ---- plumbing ------------------------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in LOCAL_HOSTS                            # a page from another site cannot use this server

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", (extra or {}).pop("Cache-Control", "no-store"))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, doc: Any, code: int = 200) -> None:
        self._send(code, json.dumps(doc, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _file(self, p: Path, cache: bool = False) -> None:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix in (".js", ".css", ".html", ".json"):
            ctype += "; charset=utf-8"
        size = p.stat().st_size
        rng = self.headers.get("Range")
        extra = {"Accept-Ranges": "bytes", "Cache-Control": "max-age=3600" if cache else "no-cache"}
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            a = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
            b = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
            b = min(b, size - 1)
            if a > b:
                self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            with open(p, "rb") as fh:
                fh.seek(a)
                body = fh.read(b - a + 1)
            self._send(206, body, ctype, {**extra, "Content-Range": f"bytes {a}-{b}/{size}"})
            return
        self._send(200, p.read_bytes(), ctype, extra)

    # ---- GET -----------------------------------------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            self._err(403, "this dashboard answers on localhost only")
            return
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        parts = [unquote(x) for x in u.path.split("/") if x]
        a = self.app
        try:
            if not parts:
                self._file(STATIC / "index.html")
            elif parts[0] == "static" and len(parts) == 2:
                p = _inside(STATIC, parts[1])
                self._file(p) if p else self._err(404, "not found")
            elif parts == ["route_map", "index.html"]:
                self._file(ROUTE_MAP)
            elif parts[0] == "media" and len(parts) >= 3 and MISSION_ID.match(parts[1]):
                p = _inside(a.archive / parts[1], "/".join(parts[2:]))
                self._file(p, cache=True) if p else self._err(404, "not found")
            elif parts[0] == "api":
                self._api(parts[1:], q)
            else:
                self._err(404, "not found")
        except api.NotFound as e:
            self._err(404, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                                # noqa: BLE001 - the page shows the error
            traceback.print_exc()
            self._err(500, f"{type(e).__name__}: {e}")

    def _api(self, p: list[str], q: dict[str, list[str]]) -> None:
        from hima.ground.routes import offers, pending, route_view
        a = self.app
        inc = _bool(q, "virtual")
        if p == ["state"]:
            self._json(api.state(a.archive, pending(a.plans)))
        elif not a.archive.is_dir() and p and p[0] not in ("routes", "route"):
            self._err(404, f"archive folder not found: {a.archive}")
        elif p == ["missions"]:
            self._json(api.mission_list(a.archive))
        elif p == ["overview"]:
            self._json(api.overview(a.archive, inc, (q.get("start") or [None])[0], (q.get("end") or [None])[0]))
        elif len(p) == 2 and p[0] == "mission" and MISSION_ID.match(p[1]):
            self._json(api.mission(a.archive, p[1]))
        elif len(p) == 4 and p[0] == "clip" and MISSION_ID.match(p[1]) and ITEM_ID.match(p[2]) and p[3] == "info":
            info = api.clip_boxes(a.archive, p[1], p[2])
            c = self._clip(p[1], p[2])
            if c is None:
                self._err(404, "the clip cannot be read")
                return
            self._json({**info, "n": len(c["frames"]), "fps": c["fps"], "w": c["w"], "h": c["h"]})
        elif len(p) == 4 and p[0] == "clip" and MISSION_ID.match(p[1]) and ITEM_ID.match(p[2]) and (
                p[3].isdigit() or p[3] == "poster"):
            c = self._clip(p[1], p[2])
            k = len(c["frames"]) // 2 if c is not None and p[3] == "poster" else int(p[3]) if p[3].isdigit() else 0
            if c is None or k >= len(c["frames"]):
                self._err(404, "no such frame")
                return
            self._send(200, c["frames"][k], "image/jpeg", {"Cache-Control": "max-age=3600"})
        elif p == ["compare"]:
            by = (q.get("by") or ["flight"])[0]
            if by not in ("flight", "day"):
                self._err(400, "by must be flight or day")
                return
            keys = [k for k in (q.get("keys") or [""])[0].split(",") if k]
            self._json(api.compare_view(a.archive, by, keys, inc))
        elif p == ["review"]:
            self._json(api.review(a.archive, inc))
        elif p == ["routes"]:
            self._json({"offers": offers(a.plans), "pending": pending(a.plans), "dir": str(a.plans.root),
                        "wait_s": a.cfg.route_review.wait_s})
        elif len(p) == 2 and p[0] == "route" and valid_id(p[1]):
            v = route_view(a.cfg, a.plans, p[1], a.imagery)
            self._json(v) if v else self._err(404, "no such route offer")
        else:
            self._err(404, "not found")

    def _clip(self, mid: str, item_id: str) -> dict[str, Any] | None:
        f = api._files(self.app.archive, mid)
        it = next((i for i in f["manifest"]["items"] if i["id"] == item_id and i["kind"] == "event_clip"), None)
        if it is None or it["state"] != "stored" or not it.get("file"):
            raise api.NotFound(f"no stored clip {item_id!r} in {mid}")
        p = _inside(f["folder"], it["file"]["path"])
        return self.app.clips.get(p) if p else None

    # ---- POST: the Route page's buttons ----------------------------------------------------------------------------
    def do_POST(self):
        from hima.ground.routes import act
        if not self._host_ok() or self.headers.get("X-Hima") != "1":
            self._err(403, "refused")                         # the header makes another site's form impossible
            return
        parts = [x for x in urlsplit(self.path).path.split("/") if x]
        if len(parts) != 3 or parts[:2] != ["api", "route"] or not valid_id(parts[2]):
            self._err(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                self._err(413, "too large")
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            pts = body.get("targets") or []
            if not isinstance(pts, list) or not all(isinstance(x, list) and len(x) == 2 for x in pts):
                self._err(400, "targets must be [[lat, lon], ...]")
                return
            ok, msg = act(self.app.cfg, self.app.plans, parts[2], str(body.get("action")), pts)
            self._json({"ok": ok, "message": msg})
        except (ValueError, TypeError) as e:
            self._err(400, f"{type(e).__name__}: {e}")


class Server(ThreadingHTTPServer):
    request_queue_size = 128          # the default 5 refuses connections when the page loads many files at once
    daemon_threads = True


def make_server(archive: Path, cfg: Config, port: int = 8501, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    app = App(archive, cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = Server((host, port), handler)
    srv.daemon_threads = True
    if Path(archive).is_dir():                                # the first Overview checks every file: start it now
        threading.Thread(target=lambda: _quiet(api.verify, Path(archive)), daemon=True).start()
    return srv


def _quiet(fn, *a) -> None:
    try:
        fn(*a)
    except Exception:                                         # noqa: BLE001 - the Overview shows any error itself
        pass


def serve(archive: Path, cfg: Config, port: int = 8501, open_browser: bool = False) -> None:
    srv = make_server(archive, cfg, port)
    url = f"http://localhost:{srv.server_address[1]}"
    print(f"Hima station dashboard: {url}  (archive {archive}; Ctrl+C stops it)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

