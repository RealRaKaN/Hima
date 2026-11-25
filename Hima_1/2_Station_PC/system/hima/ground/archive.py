"""Ground archive: ingest mission packages after checking them, and keep an index.

ingest(package_dir, archive) accepts a package only when ALL of these hold:
  - verify_package_dir: manifest schema + rules, every stored file present with the recorded
    size and SHA-256, no unlisted file, session_sha256 matches the session item;
  - the session file is a valid hima.session/1 with analysis, of the same mission_id;
  - report.json is a hima.report/1 record of the same mission, its report part matches
    report.schema.json, and its summary is exactly mission_summary() of that session (the
    report is about THIS session, not another one).
Then the folder is copied to <archive>/.tmp-*, checked again (the copy itself), and renamed to
<archive>/<mission_id>. Nothing is copied when a check fails. The same package twice is a no-op
("already_ingested"); a DIFFERENT package with an archived mission_id is refused, never
overwritten. <archive>/index.json is derived data: rebuild_index() recreates it from the folders.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hima.contract.io import ContractError, SessionInvalid, load_session, write_json_atomic
from hima.contract.package import MANIFEST, sha256_file, verify_package_dir
from hima.onboard.report import mission_summary, parse_report

FLIGHT_LOG_FILE = "flight_log.json"            # packager.FLIGHT_LOG_FILE (not imported: the packager pulls in OpenCV)

INDEX = "index.json"
INDEX_VERSION = "hima.ground_index/1"
SESSION_FILE, REPORT_FILE = "session.json", "report.json"


class IngestRejected(Exception):
    def __init__(self, package: Path, errors: list[ContractError | str]):
        self.package, self.errors = package, [str(e) for e in errors]
        super().__init__(f"{package}: rejected: " + "; ".join(self.errors[:5])
                         + (f" (+{len(self.errors) - 5} more)" if len(self.errors) > 5 else ""))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_package(pkg: str | Path) -> list[str]:
    """Every reason this folder cannot enter the archive ([] = it can)."""
    pkg = Path(pkg)
    errs = [str(e) for e in verify_package_dir(pkg)]
    if errs:
        return errs
    manifest = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))
    mid = manifest["mission_id"]
    try:
        session = load_session(pkg / SESSION_FILE)
    except SessionInvalid as e:
        return [f"{SESSION_FILE}: {x}" for x in e.errors]
    if session["meta"]["mission_id"] != mid:
        errs.append(f"{SESSION_FILE}: mission_id {session['meta']['mission_id']!r}, manifest says {mid!r}")
    if "analysis" not in session:
        return errs + [f"{SESSION_FILE}: has no analysis"]
    try:
        rep = json.loads((pkg / REPORT_FILE).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return errs + [f"{REPORT_FILE}: invalid JSON: {e}"]
    if not isinstance(rep, dict) or rep.get("schema_version") != "hima.report/1":
        return errs + [f"{REPORT_FILE}: not a hima.report/1 record"]
    if rep.get("mission_id") != mid:
        errs.append(f"{REPORT_FILE}: mission_id {rep.get('mission_id')!r}, manifest says {mid!r}")
    _, rerr = parse_report(json.dumps(rep.get("report")))
    errs += [f"{REPORT_FILE}: report: {x}" for x in rerr]
    want = None
    stored = rep.get("summary") if isinstance(rep.get("summary"), dict) else {}
    if "detail" in stored:                        # Recomputed with the package's own flight log
        fl = None
        if (pkg / FLIGHT_LOG_FILE).is_file():
            try:
                fl = json.loads((pkg / FLIGHT_LOG_FILE).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                fl = None
        try:
            dt = stored["detail"]
            want = mission_summary(session, session["analysis"], fl, dt.get("link_m"), dt.get("vehicle_near_herd_m"),
                                   dt.get("vehicle_close_m"))
        except Exception as e:                        # noqa: BLE001 - a broken summary is a finding, not a crash
            errs.append(f"{REPORT_FILE}: summary detail cannot be recomputed: {type(e).__name__}: {e}")
    else:
        want = mission_summary(session, session["analysis"])
    if want is not None and stored != want:
        errs.append(f"{REPORT_FILE}: summary does not match the session's analysis")
    return errs


def index_entry(folder: str | Path, ingested_at: str) -> dict[str, Any]:
    """One index row, computed from the archived files only."""
    folder = Path(folder)
    m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    rep = json.loads((folder / REPORT_FILE).read_text(encoding="utf-8"))
    meta = json.loads((folder / SESSION_FILE).read_text(encoding="utf-8"))["meta"]
    sm = rep["summary"]
    events: Counter[str] = Counter()
    for a in sm["animals"]:
        events.update(a["events"])
    items = m["items"]
    unc = [i for i in items if i["kind"] == "uncertain_detection"]
    clips = [i for i in items if i["kind"] == "event_clip"]
    return {
        "mission_id": m["mission_id"],
        "ingested_at": ingested_at,
        "manifest_sha256": sha256_file(folder / MANIFEST),
        "package_created_at": m["created_at"],
        "flight_date": meta["created_at"][:10],
        "source": meta["source"],
        "duration_s": sm["duration_s"],
        "metrics_known": sm["metrics_known"],
        "tracks_seen": sm["tracks_seen"],
        "tracks_by_species": dict(sorted(sm["tracks_by_species"].items())),
        "statuses": dict(sorted(Counter(a["status"] for a in sm["animals"]).items())),
        "events": dict(sorted(events.items())),
        "alert_track_ids": sm["alert_track_ids"],
        "report_source": rep["source"],
        "report_headline": rep["report"]["headline"],
        "uncertain": dict(sorted(Counter(i["criterion"] for i in unc).items())),
        "uncertain_with_image": sum(i["state"] == "stored" for i in unc),
        "clips": dict(sorted(Counter(i["state"] for i in clips).items())),
        "used_bytes": m["budget"]["used_bytes"],
        "dropped_count": m["budget"]["dropped_count"],
    }


def load_index(archive: str | Path) -> dict[str, Any]:
    p = Path(archive) / INDEX
    if not p.is_file():
        return {"schema_version": INDEX_VERSION, "missions": []}
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("schema_version") != INDEX_VERSION:
        raise ValueError(f"{p}: not a {INDEX_VERSION} index")
    return doc


def _write_index(archive: Path, missions: list[dict[str, Any]]) -> None:
    write_json_atomic({"schema_version": INDEX_VERSION,
                       "missions": sorted(missions, key=lambda e: (e["flight_date"], e["mission_id"]))},
                      archive / INDEX)


def ingest(package_dir: str | Path, archive: str | Path, *, now: str | None = None) -> dict[str, Any]:
    """Check, copy and index one package. Returns {"status": "ingested" | "already_ingested", "entry"}."""
    pkg, archive = Path(package_dir), Path(archive)
    errs = check_package(pkg)
    if errs:
        raise IngestRejected(pkg, errs)
    mid = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))["mission_id"]
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / mid
    index = load_index(archive)
    if dest.exists():
        same = (dest / MANIFEST).is_file() and sha256_file(dest / MANIFEST) == sha256_file(pkg / MANIFEST)
        if same and verify_package_dir(dest) == []:
            entry = next((e for e in index["missions"] if e["mission_id"] == mid), None)
            if entry is None:                                  # folder there, index lost: re-index it
                entry = index_entry(dest, now or _now())
                _write_index(archive, index["missions"] + [entry])
            return {"status": "already_ingested", "entry": entry}
        raise IngestRejected(pkg, [f"a different package of mission {mid!r} is already archived in {dest}; "
                                   "it is never overwritten"])
    tmp = archive / f".tmp-{mid}-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(pkg, tmp)
        copy_errs = check_package(tmp)
        if copy_errs:
            raise IngestRejected(pkg, ["the copy in the archive does not verify: " + e for e in copy_errs])
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    entry = index_entry(dest, now or _now())
    _write_index(archive, [e for e in index["missions"] if e["mission_id"] != mid] + [entry])
    return {"status": "ingested", "entry": entry}


def rebuild_index(archive: str | Path) -> dict[str, Any]:
    """Recreate index.json from the archived folders (keeps known ingest times). Returns
    {"missions": n, "problems": {folder: [reasons]}}; a folder that fails its checks is left out."""
    archive = Path(archive)
    known = {e["mission_id"]: e["ingested_at"] for e in load_index(archive)["missions"]}
    missions, problems = [], {}
    for d in sorted(p for p in archive.iterdir() if p.is_dir() and not p.name.startswith(".")):
        errs = check_package(d)
        if errs:
            problems[d.name] = errs
            continue
        e = index_entry(d, known.get(d.name, _now()))
        missions.append(e)
    _write_index(archive, missions)
    return {"missions": len(missions), "problems": problems}


def verify_archive(archive: str | Path) -> dict[str, list[str]]:
    """{mission_id: reasons} for every indexed mission whose folder no longer verifies, plus
    folders that are not in the index."""
    archive = Path(archive)
    idx = {e["mission_id"]: e for e in load_index(archive)["missions"]}
    out: dict[str, list[str]] = {}
    for mid, e in idx.items():
        d = archive / mid
        errs = check_package(d) if d.is_dir() else ["folder is missing"]
        if not errs and sha256_file(d / MANIFEST) != e["manifest_sha256"]:
            errs = ["package.json differs from the indexed one"]
        if errs:
            out[mid] = errs
    for d in archive.iterdir() if archive.is_dir() else []:
        if d.is_dir() and not d.name.startswith(".") and d.name not in idx:
            out[d.name] = ["folder is not in the index (run rebuild_index)"]
    return out
