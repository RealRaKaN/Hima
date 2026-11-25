"""Session contract I/O: load, validate (JSON Schema + semantic rules), atomic save.

Validation runs in two stages:
  1. JSON Schema (session.schema.json, Draft 2020-12): structure, types, enums, ranges.
  2. Semantic rules the schema cannot express (only if stage 1 passed):
     finite numbers, strictly increasing t, unique track_id per frame,
     species == class_map[class_raw], bbox centre inside the image,
     and every track_id in ground_truth / analysis / advice exists in frames.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent
SESSION_SCHEMA = "session.schema.json"
ADVICE_SCHEMA = "advice.schema.json"


@dataclass(frozen=True)
class ContractError:
    path: str      # slash-separated location, e.g. "frames/3/t" ("" = document root)
    message: str
    kind: str      # "json" | "schema" | "semantic"

    def __str__(self) -> str:
        return f"[{self.kind}] {self.path or '<root>'}: {self.message}"


class SessionInvalid(ValueError):
    def __init__(self, errors: list[ContractError]):
        self.errors = errors
        shown = "\n  ".join(str(e) for e in errors[:5])
        more = f"\n  ... and {len(errors) - 5} more" if len(errors) > 5 else ""
        super().__init__(f"{len(errors)} contract error(s):\n  {shown}{more}")


@cache
def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


@cache
def _registry() -> Registry:
    schemas = (load_schema(SESSION_SCHEMA), load_schema(ADVICE_SCHEMA))
    return Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas)


@cache
def session_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(SESSION_SCHEMA), registry=_registry())


@cache
def advice_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema(ADVICE_SCHEMA))


def _fmt(path) -> str:
    return "/".join(str(p) for p in path)


def schema_errors(validator: Draft202012Validator, doc: Any) -> list[ContractError]:
    errs = validator.iter_errors(doc)
    out = [ContractError(_fmt(e.absolute_path), e.message, "schema") for e in errs]
    return sorted(out, key=lambda e: (e.path, e.message))


def validate_session(doc: Any) -> list[ContractError]:
    """Return every contract error (empty list = valid)."""
    errors = schema_errors(session_validator(), doc)
    if errors:
        return errors
    return _semantic_errors(doc)


def validate_advice(obj: Any) -> list[ContractError]:
    """Validate one LLM advice object against advice.schema.json."""
    return schema_errors(advice_validator(), obj)


def _non_finite(node: Any, path: str = "") -> Iterator[ContractError]:
    if isinstance(node, float) and not math.isfinite(node):
        yield ContractError(path, f"{node!r} is not a finite number", "semantic")
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _non_finite(v, f"{path}/{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _non_finite(v, f"{path}/{i}" if path else str(i))


def _semantic_errors(doc: dict[str, Any]) -> list[ContractError]:
    errs = list(_non_finite(doc))
    if errs:
        return errs

    meta = doc["meta"]
    class_map = meta["detector"]["class_map"]
    width, height = meta["camera"]["width_px"], meta["camera"]["height_px"]
    known_ids: set[int] = set()
    prev_t = None

    for i, fr in enumerate(doc["frames"]):
        if prev_t is not None and fr["t"] <= prev_t:
            errs.append(ContractError(f"frames/{i}/t",
                                      f"t={fr['t']} is not greater than previous t={prev_t}", "semantic"))
        prev_t = fr["t"]
        seen: set[int] = set()
        for j, d in enumerate(fr["detections"]):
            p = f"frames/{i}/detections/{j}"
            tid = d["track_id"]
            if tid in seen:
                errs.append(ContractError(f"{p}/track_id", f"duplicate track_id {tid} in one frame", "semantic"))
            seen.add(tid)
            known_ids.add(tid)
            expected = class_map.get(d["class_raw"])
            if expected is None:
                errs.append(ContractError(f"{p}/class_raw",
                                          f"{d['class_raw']!r} is not a key of meta.detector.class_map", "semantic"))
            elif expected != d["species"]:
                errs.append(ContractError(f"{p}/species",
                                          f"species {d['species']!r} but class_map[{d['class_raw']!r}] = {expected!r}",
                                          "semantic"))
            cx, cy = d["bbox_xywh_px"][0], d["bbox_xywh_px"][1]
            if not (0 <= cx <= width and 0 <= cy <= height):
                errs.append(ContractError(f"{p}/bbox_xywh_px",
                                          f"centre ({cx}, {cy}) is outside the {width}x{height} image", "semantic"))

    for key in doc.get("ground_truth", {}):
        if int(key) not in known_ids:
            errs.append(ContractError(f"ground_truth/{key}", f"track_id {key} never appears in frames", "semantic"))
    for section in ("analysis", "advice"):
        for k, a in enumerate(doc.get(section, {}).get("animals", [])):
            if a["track_id"] not in known_ids:
                errs.append(ContractError(f"{section}/animals/{k}/track_id",
                                          f"track_id {a['track_id']} never appears in frames", "semantic"))
    return errs


def load_session(path: str | Path) -> dict[str, Any]:
    """Read and validate a session file. Raises SessionInvalid on any error."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise SessionInvalid([ContractError("", f"invalid JSON: {e}", "json")]) from e
    errors = validate_session(doc)
    if errors:
        raise SessionInvalid(errors)
    return doc


def save_session(doc: dict[str, Any], path: str | Path) -> None:
    """Validate, then write atomically: temp file in the same folder + fsync + os.replace.

    If validation or writing fails, the existing file (if any) is left untouched and
    no temp file is left behind.
    """
    errors = validate_session(doc)
    if errors:
        raise SessionInvalid(errors)
    write_json_atomic(doc, path)


def write_json_atomic(doc: Any, path: str | Path) -> None:
    """Temp file in the same folder + fsync + os.replace: readers see the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)     # sessions/ is git-ignored: a fresh clone lacks it
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
