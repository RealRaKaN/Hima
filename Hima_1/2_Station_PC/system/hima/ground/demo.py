"""Virtual flights always in the station's archive ("the dashboard must not open empty"; then
"add more than one flight to compare").

[demo].package is a mission package of a simulated flight (ArduPilot SITL + the simulator's video: NOT a real
flight). [demo].more is a folder of more packages: synthetic flights (tools/make_demo_archive.py --packages-out), so
the Compare page has flights to compare. At the station's start each is checked and ingested like any package (ingest
is idempotent: already there = nothing to do). A virtual flight whose package changed (a new report) replaces
its old copy; only flights listed in demo.json are ever replaced, never a real one. [demo].route_offer is a route offer whose wait is over: the Route page
shows a real plan of the reserve without anything waiting.

    python -m hima demo --archive ground_archive        (the station's start does it by itself)

The archive keeps demo.json with the virtual flights' mission ids; the dashboard marks them as virtual and leaves them
out of the totals once real flights have arrived.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from hima.config import Config
from hima.contract.package import MANIFEST, sha256_file
from hima.ground.archive import ingest
from hima.contract.io import write_json_atomic
from hima.ground.plans import PlanStore, check_offer

DEMO_FILE = "demo.json"
NOTE = "virtual flights: ArduPilot SITL and the simulator's video, or the synthetic generator; not real flights"


def _doc(archive: str | Path) -> dict[str, Any]:
    try:
        d = json.loads((Path(archive) / DEMO_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def demo_id(archive: str | Path) -> str | None:
    """The main virtual flight's mission id (the simulated flight with video), if this archive has one."""
    return _doc(archive).get("mission_id")


def demo_ids(archive: str | Path) -> list[str]:
    """Every virtual flight in this archive (the main one first)."""
    d = _doc(archive)
    ids = [d["mission_id"]] if d.get("mission_id") else []
    return ids + [m for m in d.get("mission_ids") or [] if m not in ids]


def _ingest(pkg: Path, archive: str | Path) -> tuple[str, str]:
    mid = json.loads((pkg / MANIFEST).read_text(encoding="utf-8"))["mission_id"]
    dest = Path(archive) / mid
    if (mid in demo_ids(archive) and (dest / MANIFEST).is_file()
            and sha256_file(dest / MANIFEST) != sha256_file(pkg / MANIFEST)):
        old = Path(archive) / f".old-{mid}-{uuid.uuid4().hex[:8]}"   # a changed virtual flight: put the new one in
        os.replace(dest, old)
        try:
            r = ingest(pkg, archive)
        except BaseException:
            os.replace(old, dest)
            raise
        shutil.rmtree(old, ignore_errors=True)
        return "replaced", r["entry"]["mission_id"]
    r = ingest(pkg, archive)
    return r["status"], r["entry"]["mission_id"] if r.get("entry") else pkg.name


def ensure_demo(cfg: Config, archive: str | Path, plans_dir: str | Path,
                log: Callable[[str], None] = print) -> dict[str, Any]:
    """Ingest the virtual flights and write the demo route offer when they are missing. Never raises: a station
    without the demo files starts as before."""
    from hima.onboard.deliver import config_path
    out: dict[str, Any] = {"flight": None, "more": None, "route": None}
    if not cfg.demo.enabled:
        return {"flight": "off", "more": "off", "route": "off"}
    main, more = None, []
    pkg = config_path(cfg, cfg.demo.package)
    try:
        if not (pkg / "package.json").is_file():
            out["flight"] = f"missing: {pkg}"
        else:
            status, main = _ingest(pkg, archive)
            out["flight"] = f"{status}: {main}"
    except Exception as e:                                        # noqa: BLE001 - the station must start anyway
        out["flight"] = f"not added: {type(e).__name__}: {e}"
    if cfg.demo.more:
        folder = config_path(cfg, cfg.demo.more)
        added, bad = 0, []
        for p in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not (p / "package.json").is_file():
                continue
            try:
                status, mid = _ingest(p, archive)
                more.append(mid)
                added += status in ("ingested", "replaced")
            except Exception as e:                                # noqa: BLE001
                bad.append(f"{p.name}: {type(e).__name__}")
        out["more"] = (f"{len(more)} flights ({added} new)" if folder.is_dir() else f"missing: {folder}") \
            + (f"; not added: {', '.join(bad)}" if bad else "")
    else:
        out["more"] = "none"
    if main or more:
        try:
            write_json_atomic({"schema_version": "hima.demo/1", "mission_id": main, "mission_ids": more,
                               "note": NOTE}, Path(archive) / DEMO_FILE)
        except OSError as e:
            out["flight"] = f"{out['flight']} (demo.json not written: {e})"
    off = config_path(cfg, cfg.demo.route_offer)
    try:
        doc = json.loads(off.read_text(encoding="utf-8"))
        err = check_offer(doc)
        if err:
            out["route"] = f"not added: {err}"
        else:
            store = PlanStore(plans_dir)
            if store.offer(doc["plan_id"]) is None:
                store.save_offer({**doc, "wait_s": 0, "wait_left_s": 0, "demo": True}, "demo")
                out["route"] = f"added: {doc['plan_id']}"
            else:
                out["route"] = f"already there: {doc['plan_id']}"
    except OSError:
        out["route"] = f"missing: {off}"
    except Exception as e:                                        # noqa: BLE001
        out["route"] = f"not added: {type(e).__name__}: {e}"
    log(f"demo flight {out['flight']}; more virtual flights {out['more']}; demo route {out['route']}")
    return out
