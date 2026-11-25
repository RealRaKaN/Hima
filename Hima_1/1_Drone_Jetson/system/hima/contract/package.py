"""Mission package contract (hima.package/1): manifest validation and folder verification.

validate_package(manifest): JSON Schema (package.schema.json), then the rules the schema
cannot express:
  - unique item ids; exactly one session item and one report item, both stored;
  - state <-> file: stored = file with a path, dropped = file without a path + drop_reason,
    no_media = no file;
  - stored paths are relative, inside the package, unique;
  - t_start <= t_end; event clip frames consistent;
  - budget numbers match the items (used = sum of stored bytes, dropped_count, over_budget);
  - an id_switch item names two tracks and has 'prev';
  - at most one flight_log (always stored); a flight_image has sample_t inside its time range,
    names its window exactly when purpose = window, and is never no_media (taken onboard, not cut from
    a video file, so the "no video -> no_media" rule does not apply to it).
verify_package_dir(folder): the manifest is valid AND every stored file exists with the
recorded size and SHA-256, dropped files are gone, and no unlisted file is present.
"""
from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator

from hima.contract.io import ContractError, _non_finite, load_schema, schema_errors

PACKAGE_SCHEMA = "package.schema.json"
MANIFEST = "package.json"


@cache
def package_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(PACKAGE_SCHEMA))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_package(doc: Any) -> list[ContractError]:
    errors = schema_errors(package_validator(), doc)
    if errors:
        return errors
    errs = list(_non_finite(doc))
    if errs:
        return errs
    return _semantic_errors(doc)


def _bad_path(p: str) -> bool:
    pp = PurePosixPath(p)
    return "\\" in p or pp.is_absolute() or ".." in pp.parts or ":" in p or p in ("", ".") or p == MANIFEST


def _semantic_errors(doc: dict[str, Any]) -> list[ContractError]:
    errs: list[ContractError] = []

    def err(path: str, msg: str) -> None:
        errs.append(ContractError(path, msg, "semantic"))

    ids: set[str] = set()
    paths: set[str] = set()
    sessions = reports = logs = used = dropped = 0
    for i, it in enumerate(doc["items"]):
        p = f"items/{i}"
        if it["id"] in ids:
            err(f"{p}/id", f"duplicate item id {it['id']!r}")
        ids.add(it["id"])
        if it["t_start"] > it["t_end"]:
            err(f"{p}/t_start", f"t_start {it['t_start']} is after t_end {it['t_end']}")

        f, state = it["file"], it["state"]
        if state == "stored":
            if f is None or f["path"] is None:
                err(f"{p}/file", "a stored item needs a file with a path")
            else:
                if _bad_path(f["path"]):
                    err(f"{p}/file/path", f"{f['path']!r} is not a relative path inside the package")
                if f["path"] in paths:
                    err(f"{p}/file/path", f"path {f['path']!r} used twice")
                paths.add(f["path"])
                used += f["bytes"]
        elif state == "dropped":
            dropped += 1
            if f is None or f["path"] is not None:
                err(f"{p}/file", "a dropped item keeps size and hash but no path")
            if "drop_reason" not in it:
                err(f"{p}/drop_reason", "a dropped item must say why")
        elif f is not None:
            err(f"{p}/file", "a no_media item has no file")
        if state != "dropped" and "drop_reason" in it:
            err(f"{p}/drop_reason", "only a dropped item has a drop_reason")

        if it["kind"] in ("session", "report"):
            if it["kind"] == "session":
                sessions += 1
            else:
                reports += 1
            if state != "stored":
                err(f"{p}/state", f"the {it['kind']} item is always stored")
        elif it["kind"] == "event_clip":
            a, b = it["frame_first"], it["frame_last"]
            if (a is None) != (b is None):
                err(f"{p}/frame_first", "frame_first and frame_last are both set or both null")
            elif a is not None and a > b:
                err(f"{p}/frame_first", f"frame_first {a} is after frame_last {b}")
            if (a is None) != (state == "no_media"):
                err(f"{p}/frame_first", "frames are set exactly when the clip was cut from a video")
            if len(it["track_ids"]) != 1:
                err(f"{p}/track_ids", "an event clip belongs to one track")
        elif it["kind"] == "flight_log":
            logs += 1
            if state != "stored":
                err(f"{p}/state", "the flight_log item is always stored")
        elif it["kind"] in ("raw_flight", "no_position_analysis"):
            if state != "stored":
                err(f"{p}/state", f"the {it['kind']} item is always stored")
        elif it["kind"] == "flight_image":
            if not it["t_start"] <= it["sample_t"] <= it["t_end"]:
                err(f"{p}/sample_t", f"sample_t {it['sample_t']} is outside [{it['t_start']}, {it['t_end']}]")
            if (it["window"] is None) != (it["purpose"] != "window"):
                err(f"{p}/window", "a window image names its window; other images have window null")
            if state == "no_media":
                err(f"{p}/state", "a flight image is taken onboard: stored or dropped, never no_media")
            if (it["priority"] == 0) != (it["purpose"] == "vehicle_follow"):
                err(f"{p}/priority", "vehicle_follow images are priority 0 (never dropped); other images 1-3")
        else:                                   # uncertain_detection
            if it["criterion"] == "id_switch":
                if len(it["track_ids"]) != 2 or "prev" not in it:
                    err(f"{p}/criterion", "an id_switch item names two tracks and has 'prev'")
            elif "prev" in it:
                err(f"{p}/prev", "only an id_switch item has 'prev'")
            if not it["t_start"] <= it["sample_t"] <= it["t_end"]:
                err(f"{p}/sample_t", f"sample_t {it['sample_t']} is outside [{it['t_start']}, {it['t_end']}]")

    if sessions != 1:
        err("items", f"exactly one session item is required, found {sessions}")
    if reports != 1:
        err("items", f"exactly one report item is required, found {reports}")
    if logs > 1:
        err("items", f"at most one flight_log item, found {logs}")
    b = doc["budget"]
    if b["used_bytes"] != used:
        err("budget/used_bytes", f"used_bytes {b['used_bytes']} but stored files sum to {used}")
    if b["dropped_count"] != dropped:
        err("budget/dropped_count", f"dropped_count {b['dropped_count']} but {dropped} items are dropped")
    if b["over_budget"] != (used > b["limit_bytes"]):
        err("budget/over_budget", f"over_budget must be {used > b['limit_bytes']}")
    if doc["media"] is None and any(it["state"] != "no_media" for it in doc["items"]
                                   if it["kind"] in ("event_clip", "uncertain_detection")):
        err("media", "without a video every clip and still frame is no_media")
    return errs


def verify_package_dir(folder: str | Path) -> list[ContractError]:
    folder = Path(folder)
    mpath = folder / MANIFEST
    try:
        doc = json.loads(mpath.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [ContractError(MANIFEST, "missing", "file")]
    except json.JSONDecodeError as e:
        return [ContractError(MANIFEST, f"invalid JSON: {e}", "json")]
    errs = validate_package(doc)
    if errs:
        return errs
    listed = {MANIFEST}
    for i, it in enumerate(doc["items"]):
        f = it["file"]
        if it["state"] != "stored":
            continue
        listed.add(f["path"])
        fp = folder / f["path"]
        if not fp.is_file():
            errs.append(ContractError(f"items/{i}/file", f"{f['path']} is missing", "file"))
            continue
        size = fp.stat().st_size
        if size != f["bytes"]:
            errs.append(ContractError(f"items/{i}/file", f"{f['path']} is {size} bytes, manifest says {f['bytes']}", "file"))
        elif sha256_file(fp) != f["sha256"]:
            errs.append(ContractError(f"items/{i}/file", f"{f['path']} SHA-256 does not match the manifest", "file"))
    present = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    for extra in sorted(present - listed):
        errs.append(ContractError(extra, "file is not listed in the manifest (or was dropped)", "file"))
    session = next(it for it in doc["items"] if it["kind"] == "session")
    if not errs and session["file"]["sha256"] != doc["session_sha256"]:
        errs.append(ContractError("session_sha256", "does not match the session item", "semantic"))
    return errs
