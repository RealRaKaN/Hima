"""Station side of the Wi-Fi delivery: receive mission packages from the drone and add them to the archive.

    python -m hima receive          (or launcher\\start_station.bat, which also opens the dashboard)

A package arrives file by file into <inbox>/<mission_id>.partial/ (the offer is kept next to it as
<mission_id>.offer.json), so a cut connection, or a restart of this program, goes on from the bytes already held.
Each file is checked against its SHA-256 as soon as it is complete; a file that does not match is deleted and sent
again. At commit the package goes through hima.ground.archive.ingest, the same checks as python -m hima ingest
(manifest, every SHA-256, session, report); accepted, it is in the archive and the partial folder is removed;
refused, it is moved to <inbox>/rejected/ with its reasons and nothing enters the archive. A mission already in the
archive with the same manifest is "already_ingested"; a DIFFERENT package with that mission id is refused, never
overwritten (as ingest).

Protocol and signatures: hima.contract.delivery. A request without a valid signature changes nothing.

Route offers before take-off, same key and signatures, files in hima.ground.plans:
  POST /v1/plan            the drone's route (hima.route_offer/1); the dashboard shows it
  GET  /v1/plan/answer     ?plan_id=...: the station's answer, or {"decision": null} while there is none
  POST /v1/plan/result     what the drone flies (its own route, or the edited one, and why)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from hima.contract import delivery as dl
from hima.contract.io import write_json_atomic
from hima.contract.package import MANIFEST, sha256_file, verify_package_dir
from hima.ground.archive import IngestRejected, ingest
from hima.ground.plans import PlanStore, check_offer, valid_id

FREE_MARGIN_BYTES = 500_000_000          # keep this much disk free beyond the package (twice: inbox + archive copy)
MAX_FILES = 20_000


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Receiver:
    """The station's logic, without HTTP: handle(method, target, headers, body) -> (status, answer dict)."""

    def __init__(self, key: bytes, inbox: str | Path, archive: str | Path, *,
                 log: Callable[[str], None] = lambda s: print(s, flush=True), name: str | None = None,
                 plans: str | Path | None = None):
        self.key = key
        self.inbox, self.archive = Path(inbox), Path(archive)
        self.plans = PlanStore(plans if plans is not None else self.inbox / "plans")
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.name = name or socket.gethostname()
        self.lock = threading.Lock()
        self._log = log

    def log(self, text: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}"
        self._log(line)
        try:
            with open(self.inbox / "receiver.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # -- authentication ------------------------------------------------------------------------------------------
    def check(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict] | None:
        """None if the request is signed with our key; else the error answer."""
        claimed = headers.get(dl.H_BODY.lower(), "")
        if not dl.same(claimed, dl.body_sha256(body)):
            return 400, {"error": "the body does not match its SHA-256 (changed on the way): send it again"}
        sig = headers.get(dl.H_SIG.lower(), "")
        if not sig or not dl.same(sig, dl.request_signature(self.key, method, target, claimed)):
            return 401, {"error": "bad signature: the drone's key is not this station's key (compare the "
                                  "fingerprints both print)"}
        return None

    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
        bad = self.check(method, target, headers, body)
        if bad is not None:
            self.log(f"refused {method} {urlsplit(target).path}: {bad[1]['error']}")
            return bad
        url = urlsplit(target)
        route = (method, url.path)
        try:
            if route == ("GET", "/v1/ping"):
                return 200, {"station": self.name, "protocol": dl.PROTOCOL}
            if route == ("PUT", "/v1/file"):
                return self.put_file(parse_qs(url.query), body)
            if url.path.startswith("/v1/plan"):
                return self.plan_call(method, url.path, parse_qs(url.query), body)
            if method == "POST" and url.path in ("/v1/offer", "/v1/commit"):
                try:
                    doc = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    return 400, {"error": f"invalid JSON: {e}"}
                if not isinstance(doc, dict) or not dl.valid_mission_id(doc.get("mission_id")):
                    return 400, {"error": "mission_id missing or not a valid name"}
                with self.lock:
                    return (self.offer if url.path == "/v1/offer" else self.commit)(doc)
            return 404, {"error": f"no such call: {method} {url.path}"}
        except OSError as e:                              # a full disk, a file in use: the drone tries again later
            self.log(f"ERROR {method} {url.path}: {type(e).__name__}: {e}")
            return 503, {"error": f"station storage error: {type(e).__name__}: {e}"}

    # -- route offers -------------------------------------------------------------------------------------
    def plan_call(self, method: str, path: str, q: dict[str, list[str]], body: bytes) -> tuple[int, dict]:
        if (method, path) == ("GET", "/v1/plan/answer"):
            pid = (q.get("plan_id") or [""])[0]
            if not valid_id(pid) or self.plans.offer(pid) is None:
                return 404, {"error": f"no offer {pid!r} at this station"}
            return 200, {"plan_id": pid, **(self.plans.answer(pid) or {"decision": None})}
        if method != "POST" or path not in ("/v1/plan", "/v1/plan/result"):
            return 404, {"error": f"no such call: {method} {path}"}
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return 400, {"error": f"invalid JSON: {e}"}
        if path == "/v1/plan":
            err = check_offer(doc)
            if err:
                return 400, {"error": err}
            with self.lock:
                saved = self.plans.save_offer(doc, "wifi")
            self.log(f"route offer {doc['plan_id']}: {len(doc['targets'])} points, {doc.get('route_km')} km, the "
                     f"drone waits {doc.get('wait_s')} s (dashboard: Route)")
            return 200, {"plan_id": doc["plan_id"], "state": "shown", "source": saved["source"]}
        pid = doc.get("plan_id") if isinstance(doc, dict) else None
        if not valid_id(pid) or self.plans.offer(pid) is None:
            return 404, {"error": f"no offer {pid!r} at this station"}
        self.plans.write_result(pid, {k: v for k, v in doc.items() if k != "schema_version"})
        self.log(f"route {pid}: the drone flies the {doc.get('used')} route ({doc.get('reason')})")
        return 200, {"plan_id": pid, "state": "recorded"}

    # -- the three steps -----------------------------------------------------------------------------------------
    def _paths(self, mid: str) -> tuple[Path, Path]:
        return self.inbox / f"{mid}.partial", self.inbox / f"{mid}.offer.json"

    def offer(self, doc: dict) -> tuple[int, dict]:
        mid, files = doc["mission_id"], doc.get("files")
        err = _check_files(files)
        if err:
            return 400, {"error": err}
        manifest = next(f for f in files if f["path"] == MANIFEST)
        dest = self.archive / mid
        if dest.exists():
            if (dest / MANIFEST).is_file() and sha256_file(dest / MANIFEST) == manifest["sha256"] \
                    and verify_package_dir(dest) == []:
                self.log(f"offer {mid}: already in the archive (nothing to send)")
                return 200, {"state": "already_ingested"}
            self.log(f"offer {mid}: REFUSED, a different package of this mission is already archived")
            return 409, {"state": "conflict", "error": f"a different package of mission {mid!r} is already in the "
                                                       "archive; it is never overwritten"}
        partial, offer_file = self._paths(mid)
        offered = {"mission_id": mid, "files": files}
        if offer_file.exists():
            try:
                old = json.loads(offer_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old = None
            if old != offered:                            # another version of this mission: start it again
                shutil.rmtree(partial, ignore_errors=True)
                self.log(f"offer {mid}: differs from the one in the inbox; starting again")
        total = sum(f["bytes"] for f in files)
        held = sum(_held(partial, f["path"]) for f in files) if partial.exists() else 0
        free = shutil.disk_usage(self.inbox).free
        if free < 2 * (total - held) + FREE_MARGIN_BYTES:
            self.log(f"offer {mid}: NOT ENOUGH DISK ({free / 1e9:.1f} GB free, package {total / 1e9:.2f} GB)")
            return 507, {"error": f"station disk too full: {free / 1e9:.1f} GB free, the package needs "
                                  f"{total / 1e9:.2f} GB twice plus a margin"}
        partial.mkdir(parents=True, exist_ok=True)
        write_json_atomic(offered, offer_file)
        have = {}
        for f in files:
            p = partial / f["path"]
            if f["bytes"] == 0 and not p.exists():       # an empty file has no piece to send
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            have[f["path"]] = _held(partial, f["path"])
        self.log(f"offer {mid}: {len(files)} files, {total / 1e6:.1f} MB"
                 + (f", {held / 1e6:.1f} MB already here (going on)" if held else ""))
        return 200, {"state": "receiving", "have": have}

    def put_file(self, q: dict[str, list[str]], body: bytes) -> tuple[int, dict]:
        mid, rel, off = (q.get(k, [""])[0] for k in ("mission", "path", "offset"))
        if not dl.valid_mission_id(mid) or not dl.valid_file_path(rel) or not off.isdigit():
            return 400, {"error": "mission, path or offset missing or invalid"}
        offset = int(off)
        with self.lock:
            partial, offer_file = self._paths(mid)
            if not offer_file.exists():
                return 409, {"error": f"no offer for {mid}: offer first"}
            files = {f["path"]: f for f in json.loads(offer_file.read_text(encoding="utf-8"))["files"]}
            if rel not in files:
                return 400, {"error": f"{rel} is not in the offer of {mid}"}
            want = files[rel]
            p = partial / rel
            have = p.stat().st_size if p.exists() else 0
            if offset != have:
                return 409, {"have": have}
            if have + len(body) > want["bytes"]:
                return 400, {"error": f"{rel}: {have + len(body)} bytes would be more than the {want['bytes']} offered"}
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "ab") as f:
                f.write(body)
                if have + len(body) == want["bytes"]:
                    f.flush()
                    os.fsync(f.fileno())
            have += len(body)
            if have == want["bytes"]:
                if sha256_file(p) != want["sha256"]:
                    p.unlink()
                    self.log(f"{mid}: {rel} complete but its SHA-256 does not match: deleted, the drone sends it again")
                    return 422, {"error": f"{rel}: SHA-256 does not match; send the file again", "have": 0}
            return 200, {"have": have}

    def commit(self, doc: dict) -> tuple[int, dict]:
        mid = doc["mission_id"]
        partial, offer_file = self._paths(mid)
        dest = self.archive / mid
        if not offer_file.exists():
            if dest.is_dir() and verify_package_dir(dest) == []:   # committed before, the answer was lost
                return 200, {"status": "already_ingested", "entry": None}
            return 409, {"error": f"no offer for {mid}"}
        files = json.loads(offer_file.read_text(encoding="utf-8"))["files"]
        short = {f["path"]: _held(partial, f["path"]) for f in files if _held(partial, f["path"]) != f["bytes"]}
        if short:
            return 409, {"error": f"{len(short)} file(s) not complete", "have": short}
        t0 = time.perf_counter()
        try:
            r = ingest(partial, self.archive)
        except IngestRejected as e:
            where = self.inbox / "rejected" / f"{mid}-{_stamp()}"
            where.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, where)
            (where.parent / f"{where.name}.reasons.txt").write_text("\n".join(e.errors) + "\n", encoding="utf-8")
            offer_file.unlink()
            self.log(f"commit {mid}: REJECTED ({len(e.errors)} reasons, first: {e.errors[0]}); kept in {where}")
            return 422, {"status": "rejected", "errors": e.errors[:50]}
        shutil.rmtree(partial)
        offer_file.unlink()
        e = r["entry"]
        self.log(f"commit {mid}: {r['status'].upper()} into {self.archive} in {time.perf_counter() - t0:.1f} s "
                 f"({e['tracks_seen']} tracks, {len(e['alert_track_ids'])} in alert, report {e['report_source']})")
        return 200, {"status": r["status"], "entry": {k: e[k] for k in (
            "mission_id", "tracks_seen", "tracks_by_species", "alert_track_ids", "report_source", "report_headline")}}


def _held(partial: Path, rel: str) -> int:
    p = partial / rel
    return p.stat().st_size if p.is_file() else 0


def _check_files(files: Any) -> str | None:
    if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
        return "files must be a non-empty list"
    seen: set[str] = set()
    folded: set[str] = set()
    for f in files:
        if not isinstance(f, dict) or set(f) != {"path", "bytes", "sha256"}:
            return "each file is {path, bytes, sha256}"
        if not dl.valid_file_path(f["path"]):
            return f"not an allowed file path: {f['path']!r}"
        if f["path"] in seen or f["path"].lower() in folded:       # Windows: names differing only in case collide
            return f"file listed twice: {f['path']!r}"
        seen.add(f["path"])
        folded.add(f["path"].lower())
        if isinstance(f["bytes"], bool) or not isinstance(f["bytes"], int) or not 0 <= f["bytes"] <= 10**12:
            return f"{f['path']}: bytes must be a whole number"
        if not isinstance(f["sha256"], str) or len(f["sha256"]) != 64 or \
                any(c not in "0123456789abcdef" for c in f["sha256"]):
            return f"{f['path']}: sha256 must be 64 lowercase hex characters"
    if MANIFEST not in seen:
        return f"the offer has no {MANIFEST}"
    return None


# -- HTTP ----------------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"                 # keep-alive: one connection carries the whole package
    server_version = "HimaReceiver/1"
    timeout = 120                                 # a dead connection is dropped
    receiver: Receiver

    def _serve(self) -> None:
        n = self.headers.get("Content-Length")
        limit = dl.MAX_PIECE_BYTES if self.command == "PUT" else dl.MAX_JSON_BYTES
        if n is None or not n.isdigit() or int(n) > limit:
            self.close_connection = True
            self._answer(413 if n and n.isdigit() else 411, {"error": f"Content-Length missing or above {limit}"}, "")
            return
        body = self.rfile.read(int(n))
        if len(body) != int(n):
            self.close_connection = True
            return
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, doc = self.receiver.handle(self.command, self.path, headers, body)
        self._answer(status, doc, headers.get(dl.H_SIG.lower(), ""))

    def _answer(self, status: int, doc: dict, request_sig: str) -> None:
        out = json.dumps(doc).encode("utf-8")
        sha = dl.body_sha256(out)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.send_header(dl.H_BODY, sha)
        self.send_header(dl.H_SIG, dl.response_signature(self.receiver.key, request_sig, status, sha))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _serve

    def log_message(self, format: str, *args: Any) -> None:     # noqa: A002 - the receiver logs each step itself
        pass


def make_server(receiver: Receiver, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"receiver": receiver})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def local_ipv4() -> list[str]:
    """This PC's IPv4 addresses (for [delivery].station_url on the drone). Nothing is sent to find them."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                  # a UDP "connect" picks the route; no packet leaves
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "0.")))


def serve(key: bytes, host: str, port: int, inbox: str | Path, archive: str | Path,
          log: Callable[[str], None] = lambda s: print(s, flush=True), plans: str | Path | None = None,
          radio: Any = None) -> None:
    """Run the receiver until Ctrl+C. radio: a started ground.route_radio.RadioBridge (stopped here at the end)."""
    rec = Receiver(key, inbox, archive, log=log, plans=plans)
    srv = make_server(rec, host, port)
    port = srv.server_address[1]
    log(f"HIMA station receiver ({dl.PROTOCOL}) on {host}:{port}")
    log(f"  archive {Path(archive).resolve()}  (the dashboard reads it)")
    log(f"  inbox   {Path(inbox).resolve()}")
    log(f"  key fingerprint {dl.fingerprint(key)}  (the drone must print the same)")
    ips = local_ipv4()
    if host in ("0.0.0.0", "", "::"):
        log("  this PC's addresses: " + (", ".join(ips) if ips else "none found")
            + f"  -> on the drone: [delivery].station_url = \"http://<one of them>:{port}\"")
    else:
        log(f"  listening on {host} only" + (" (this PC only: the drone cannot reach it)"
                                           if host.startswith("127.") or host == "localhost" else ""))
    for d in sorted(Path(inbox).glob("*.offer.json")):
        log(f"  unfinished in the inbox (goes on when the drone comes back): {d.name[:-len('.offer.json')]}")
    log("waiting for the drone (Ctrl+C to stop)")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("stopped (Ctrl+C)")
    finally:
        srv.server_close()
        if radio is not None:
            radio.stop()
