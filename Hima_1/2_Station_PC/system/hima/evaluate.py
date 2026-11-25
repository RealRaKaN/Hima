"""Compare computed statuses with the generator's ground_truth (synthetic sessions only).

This is the ONLY place that reads ground_truth, and it runs after analysis.
"""
from __future__ import annotations

from typing import Any

from hima.sources.synthetic import EXPECTED_STATUS


def evaluate(doc: dict[str, Any]) -> dict[str, Any]:
    if "analysis" not in doc:
        raise ValueError("session has no 'analysis' section; run analyze first")
    gt = doc.get("ground_truth") or {}
    if not gt:
        raise ValueError("session has no ground_truth (only synthetic sessions can be evaluated)")
    got = {a["track_id"]: a for a in doc["analysis"]["animals"]}

    rows = []
    for key in sorted(gt, key=int):
        tid, scenario = int(key), gt[key]
        expected = EXPECTED_STATUS[scenario]
        status = got[tid]["status"] if tid in got else None
        rows.append({"track_id": tid, "scenario": scenario, "expected": expected,
                     "got": status, "ok": status == expected})

    by_scenario: dict[str, bool] = {}
    for r in rows:
        by_scenario[r["scenario"]] = by_scenario.get(r["scenario"], True) and r["ok"]
    return {
        "rows": rows,
        "animals_ok": sum(r["ok"] for r in rows),
        "animals_total": len(rows),
        "scenarios_ok": sum(by_scenario.values()),
        "scenarios_total": len(by_scenario),
    }
