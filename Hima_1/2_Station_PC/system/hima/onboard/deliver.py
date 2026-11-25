"""Drone side of the Wi-Fi delivery: send finished mission packages to the ground station.

    python -m hima deliver --watch     the hima-deliver service on the Jetson: always running, sends each new package
                                       as soon as the station can be reached (after landing, on the base Wi-Fi)
    python -m hima deliver             one pass over the waiting packages
    python -m hima deliver PACKAGE     this package, even if it was refused or marked broken before

Packages are found in [flight].out_dir/*/packages/<mission_id>/ (the packager renames a package into place only
when it is complete, so a half package is never seen). For each package not yet delivered:
  1. it is checked on the drone first (verify_package_dir); a package damaged on the drone is not sent: "broken";
  2. offer: the station answers which bytes it already holds (a cut transfer goes on, it does not start again);
  3. the missing bytes go in pieces of [delivery].chunk_mb, each signed with its SHA-256; every complete file is
     checked by the station against its SHA-256 (a mismatch: the file is sent again);
  4. commit: the station runs its ingest checks (the same as python -m hima ingest) and answers accepted
     ("delivered") or refused ("rejected", with the reasons; it is not sent again by itself).
The result goes next to the package as <mission_id>.delivery.json. The package folder itself is never changed and
never deleted: it stays on the drone. While the station cannot be reached (in flight there is no Wi-Fi, the
station PC is off), nothing is written and --watch asks again every [delivery].retry_s.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from hima.config import Config
from hima.contract import delivery as dl
from hima.contract.io import write_json_atomic
from hima.contract.package import MANIFEST, sha256_file, verify_package_dir

STATE_SUFFIX = ".delivery.json"
STATE_VERSION = "hima.delivery/1"
FIRST = ("session.json", "report.json", "flight_log.json", "no_position_analysis.json", "raw_flight.jsonl.gz")      # the most important files go first
DONE = ("delivered", "rejected", "broken")


class StationUnreachable(Exception):
    """No answer, or an answer that is not from this system's station: the package waits, the drone tries again."""


class StationRefused(Exception):
    """The station refused the package for good (a conflict in its archive, or its ingest checks)."""

    def __init__(self, errors: list[str]):
        self.errors = [str(e) for e in errors if e]
        super().__init__("; ".join(self.errors[:3]))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def config_path(cfg: Config, p: str) -> Path:
    """A path from hima.toml: relative paths are relative to the hima.toml file."""
    q = Path(p)
    return q if q.is_absolute() or not cfg.source else Path(cfg.source).parent / q


class StationClient:
    """Signed calls to the station's receiver over one kept-alive HTTP connection."""

    def __init__(self, url: str, key: bytes, timeout_s: float):
        u = urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError(f"station url must be http://HOST:PORT, got {url!r}")
        self.url, self.key, self.timeout = url, key, timeout_s
        self.host, self.port, self.base = u.hostname, u.port or 80, u.path.rstrip("/")
        self._conn: http.client.HTTPConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def call(self, method: str, path: str, body: bytes = b"", query: dict[str, Any] | None = None,
             ctype: str = "application/json") -> tuple[int, dict[str, Any]]:
        target = self.base + path + ("?" + urlencode(query) if query else "")
        sha = dl.body_sha256(body)
        sig = dl.request_signature(self.key, method, target, sha)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), dl.H_BODY: sha, dl.H_SIG: sig}
        for attempt in (1, 2):
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self._conn.request(method, target, body=body, headers=headers)
                r = self._conn.getresponse()
                data = r.read()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as e:
                self.close()                      # a kept-alive connection the station closed: one fresh try (the
                if attempt == 2:                  # steps are idempotent, a repeated piece is answered 409 + have)
                    raise StationUnreachable(f"{self.url}: {type(e).__name__}: {e}") from e
            except (OSError, http.client.HTTPException) as e:
                self.close()
                raise StationUnreachable(f"{self.url}: {type(e).__name__}: {e}") from e
        if (r.getheader("Connection") or "").lower() == "close":
            self.close()
        rsha, rsig = r.getheader(dl.H_BODY) or "", r.getheader(dl.H_SIG) or ""
        signed = dl.same(rsha, dl.body_sha256(data)) and dl.same(rsig, dl.response_signature(self.key, sig, r.status,
                                                                                              rsha))
        if r.status == 401:
            raise StationUnreachable(f"{self.url} does not accept this drone's key: the two key files differ "
                                     f"(this drone's key fingerprint is {dl.fingerprint(self.key)}; the station "
                                     "prints its own when it starts)")
        if not signed:
            raise StationUnreachable(f"{self.url} answered {r.status} without a valid signature: not a HIMA station, "
                                     "or another key")
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise StationUnreachable(f"{self.url}: the answer is not JSON: {e}") from e
        return r.status, doc


def _json(doc: Any) -> bytes:
    return json.dumps(doc).encode("utf-8")


def package_files(pkg: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Every file of the package with its size and SHA-256 (from the manifest), package.json last."""
    files = [{"path": it["file"]["path"], "bytes": it["file"]["bytes"], "sha256": it["file"]["sha256"]}
             for it in manifest["items"] if it["state"] == "stored"]
    files.sort(key=lambda f: FIRST.index(f["path"]) if f["path"] in FIRST else len(FIRST))
    return files + [{"path": MANIFEST, "bytes": (pkg / MANIFEST).stat().st_size, "sha256": sha256_file(pkg / MANIFEST)}]


def deliver_package(pkg: str | Path, client: StationClient, chunk_bytes: int,
                    log: Callable[[str], None] = lambda s: None,
                    on_piece: Callable[[str, int, int, int], None] | None = None) -> dict[str, Any]:
    """Offer, send what the station lacks, commit. Returns the result; raises StationRefused / StationUnreachable.
    on_piece(path, file_bytes_held, file_bytes, bytes_sent_so_far) after each accepted piece."""
    pkg = Path(pkg)
    manifest = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))
    mid = manifest["mission_id"]
    files = package_files(pkg, manifest)
    total = sum(f["bytes"] for f in files)
    t0 = time.perf_counter()
    st, doc = client.call("POST", "/v1/offer", _json({"mission_id": mid, "files": files}))
    if st == 409 and doc.get("state") == "conflict":
        raise StationRefused([doc.get("error", "conflict")])
    if st != 200 or doc.get("state") not in ("receiving", "already_ingested"):
        raise StationUnreachable(f"offer: {st} {doc.get('error', doc)}")
    if doc["state"] == "already_ingested":
        return {"station_status": "already_ingested", "bytes": total, "sent_bytes": 0, "resumed_bytes": 0,
                "seconds": round(time.perf_counter() - t0, 2), "entry": None}
    have = {k: int(v) for k, v in doc["have"].items()}
    resumed = sum(min(have.get(f["path"], 0), f["bytes"]) for f in files)
    log(f"  {mid}: {len(files)} files, {total / 1e6:.1f} MB" +
        (f"; the station already holds {resumed / 1e6:.1f} MB (going on from there)" if resumed else ""))
    sent = 0
    quarter = int(4 * resumed / total) if total else 4          # progress lines at 25 %, 50 %, 75 %
    for f in files:
        size, n, again = f["bytes"], have.get(f["path"], 0), 0
        with open(pkg / f["path"], "rb") as fh:
            while n < size:
                fh.seek(n)
                piece = fh.read(min(chunk_bytes, size - n))
                if not piece:
                    raise StationUnreachable(f"{f['path']} is shorter on the drone than in its manifest")
                st, doc = client.call("PUT", "/v1/file", piece, {"mission": mid, "path": f["path"], "offset": n},
                                      "application/octet-stream")
                if st == 200:
                    n = int(doc["have"])
                    sent += len(piece)
                    if on_piece is not None:
                        on_piece(f["path"], n, size, sent)
                    q = int(4 * (resumed + sent) / total)
                    if quarter < q < 4:
                        quarter = q
                        log(f"  {25 * q} % ({(resumed + sent) / 1e6:.1f} of {total / 1e6:.1f} MB, "
                            f"{time.perf_counter() - t0:.0f} s)")
                elif st == 409 and isinstance(doc.get("have"), int) and doc["have"] != n:
                    n = doc["have"]                          # the station holds another amount: go on from there
                elif st == 422 and again < 2:
                    again += 1                               # the whole file did not match its SHA-256: again
                    n = 0
                    log(f"  {f['path']}: the station found a SHA-256 mismatch; sending the file again")
                else:
                    raise StationUnreachable(f"{f['path']} at {n}: {st} {doc.get('error', doc)}")
    st, doc = client.call("POST", "/v1/commit", _json({"mission_id": mid}))
    if st == 422:
        raise StationRefused(doc.get("errors") or [doc.get("error", "rejected")])
    if st != 200:
        raise StationUnreachable(f"commit: {st} {doc.get('error', doc)}")
    secs = time.perf_counter() - t0
    return {"station_status": doc["status"], "bytes": total, "sent_bytes": sent, "resumed_bytes": resumed,
            "seconds": round(secs, 2), "mb_per_s": round(sent / 1e6 / secs, 2) if secs > 0 else None,
            "entry": doc.get("entry")}


# -- packages and their delivery state ---------------------------------------------------------------------------
def state_path(pkg: Path) -> Path:
    return pkg.parent / f"{pkg.name}{STATE_SUFFIX}"


def read_state(pkg: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(state_path(pkg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema_version") == STATE_VERSION else None


def find_packages(root: Path) -> list[Path]:
    """Complete packages under <root>/*/packages/ (a folder starting with '.' is one being built)."""
    return [m.parent for m in sorted(Path(root).glob(f"*/packages/*/{MANIFEST}")) if not m.parent.name.startswith(".")]


def ping(cfg: Config, key: bytes | None = None, timeout_s: float | None = None) -> tuple[bool, str]:
    key = key if key is not None else dl.load_key(config_path(cfg, cfg.delivery.key_file))
    c = StationClient(cfg.delivery.station_url, key, timeout_s or min(5.0, cfg.delivery.timeout_s))
    try:
        st, doc = c.call("GET", "/v1/ping")
    except StationUnreachable as e:
        return False, str(e)
    finally:
        c.close()
    return st == 200, f"station {doc.get('station')!r} at {cfg.delivery.station_url} ({doc.get('protocol')})"


def deliver_pending(cfg: Config, packages: list[str | Path] | None = None, *, key: bytes | None = None,
                    client: StationClient | None = None,
                    log: Callable[[str], None] = lambda s: print(s, flush=True)) -> dict[str, Any]:
    """One pass. packages=None: every waiting package under [flight].out_dir; given: these, whatever their state.
    Returns {"delivered", "rejected", "broken", "pending", "done_before": [mission ids], "error": str | None}."""
    explicit = packages is not None
    found = [Path(p) for p in packages] if explicit else find_packages(config_path(cfg, cfg.flight.out_dir))
    out: dict[str, Any] = {"delivered": [], "rejected": [], "broken": [], "pending": [], "done_before": [],
                           "error": None}
    todo = []
    for pkg in found:
        if not (pkg / MANIFEST).is_file():
            raise FileNotFoundError(f"{pkg} is not a package folder (no {MANIFEST})")
        prev = read_state(pkg)
        if not explicit and prev is not None and prev.get("status") in DONE:
            out["done_before"].append(pkg.name)
        else:
            todo.append(pkg)
    if not todo:
        return out
    key = key if key is not None else dl.load_key(config_path(cfg, cfg.delivery.key_file))
    own = client is None
    client = client or StationClient(cfg.delivery.station_url, key, cfg.delivery.timeout_s)
    try:
        try:
            client.call("GET", "/v1/ping")             # not there (no Wi-Fi yet): nothing is written, try later
        except StationUnreachable as e:
            out["pending"] = [p.name for p in todo]
            out["error"] = str(e)
            return out
        for i, pkg in enumerate(todo):
            st = read_state(pkg) or {"schema_version": STATE_VERSION, "mission_id": pkg.name, "attempts": 0,
                                     "first_try_at": _now()}
            st.update(attempts=st["attempts"] + 1, last_try_at=_now(), station=cfg.delivery.station_url)
            errs = verify_package_dir(pkg)
            if errs:
                st.update(status="broken", errors=[str(e) for e in errs[:20]])
                write_json_atomic(st, state_path(pkg))
                out["broken"].append(pkg.name)
                log(f"NOT SENT {pkg.name}: the package is damaged on the drone ({errs[0]}); see {state_path(pkg)}")
                continue
            log(f"sending {pkg.name} to {cfg.delivery.station_url}")
            try:
                res = deliver_package(pkg, client, int(cfg.delivery.chunk_mb * 1_000_000), log)
            except StationRefused as e:
                st.update(status="rejected", errors=e.errors[:50])
                write_json_atomic(st, state_path(pkg))
                out["rejected"].append(pkg.name)
                log(f"REJECTED by the station: {pkg.name}: {e}")
                continue
            except StationUnreachable as e:
                st.update(status="pending", last_error=str(e))
                write_json_atomic(st, state_path(pkg))
                out["pending"] = [p.name for p in todo[i:]]
                out["error"] = str(e)
                log(f"  interrupted: {e} (it goes on from here next time)")
                break
            st.update(status="delivered", delivered_at=_now(), result=res)
            st.pop("last_error", None)
            write_json_atomic(st, state_path(pkg))
            out["delivered"].append(pkg.name)
            e = res["entry"] or {}
            speed = f" ({res['mb_per_s']} MB/s)" if res.get("mb_per_s") else ""
            log(f"DELIVERED {pkg.name}: station {res['station_status']}; sent {res['sent_bytes'] / 1e6:.1f} MB in "
                f"{res['seconds']} s{speed}" + (f"; {e['tracks_seen']} tracks, {len(e['alert_track_ids'])} in alert"
                                               if e else ""))
    finally:
        if own:
            client.close()
    return out


def watch(cfg: Config, *, log: Callable[[str], None] = lambda s: print(s, flush=True),
          stop: threading.Event | None = None) -> None:
    """Forever (the hima-deliver service): a pass every [delivery].retry_s; a waiting state is logged once."""
    stop = stop or threading.Event()
    key_path = config_path(cfg, cfg.delivery.key_file)
    log(f"HIMA delivery: packages in {config_path(cfg, cfg.flight.out_dir)} -> {cfg.delivery.station_url}, "
        f"every {cfg.delivery.retry_s:g} s")
    last = None
    while not stop.is_set():
        try:
            key = dl.load_key(key_path)
            if last is None or last.startswith("no key"):
                log(f"key fingerprint {dl.fingerprint(key)} (the station must print the same)")
            r = deliver_pending(cfg, key=key, log=log)
            n = len(r["pending"])
            note = (f"waiting: {n} package(s); station not reachable ({r['error']})" if n and r["error"] else
                    "nothing waiting" if not n else f"{n} package(s) waiting")
        except dl.DeliveryKeyError as e:
            note = f"no key yet: {e}"
        except Exception as e:                         # noqa: BLE001 - the service must keep running; logged once
            note = f"ERROR: {type(e).__name__}: {e}"
        if note != last:
            log(note)
            last = note
        stop.wait(cfg.delivery.retry_s)
