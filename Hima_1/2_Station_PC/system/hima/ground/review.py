"""Review export of uncertain detections, in YOLO format for labelling.

For every archived uncertain_detection with a stored still frame:
  images/<mission_id>__<item_id>.jpg   the frame (copied byte for byte)
  labels/<mission_id>__<item_id>.txt   one line per box: "<class> <cx> <cy> <w> <h>", normalised
                                       (bbox_xywh_px is already centre x, centre y, width, height)
  review.csv                           one row per image: why it is uncertain, confidences, tracks
  classes.txt / data.yaml              class names in the WEIGHT's order (hima.toml class_map order,
                                       read from best_4_1.pt: 0 'arabain oryx', 1 'reem_gazelle')
The labels are the DETECTOR'S GUESSES on detections it was unsure about: pre-labels for a human
to correct, never ground truth. Items without an image (synthetic missions, budget drops) are
counted, not exported. The output folder must be new or empty: nothing is overwritten.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

import cv2

from hima.contract.package import MANIFEST
from hima.ground.archive import load_index

LABEL_NOTE = "Pre-labels from the detector on detections it was unsure about. Correct them before training."


def review_items(archive: str | Path) -> list[dict[str, Any]]:
    """Every uncertain detection in the archive, with its mission, image path (or None) and size."""
    archive = Path(archive)
    out = []
    for e in load_index(archive)["missions"]:
        m = json.loads((archive / e["mission_id"] / MANIFEST).read_text(encoding="utf-8"))
        size = (m["media"]["width_px"], m["media"]["height_px"]) if m["media"] else None
        for it in m["items"]:
            if it["kind"] != "uncertain_detection":
                continue
            img = archive / e["mission_id"] / it["file"]["path"] if it["state"] == "stored" else None
            out.append({"mission_id": e["mission_id"], "item": it, "image": img, "size": size})
    return out


def export_review(archive: str | Path, out: str | Path, class_names: list[str]) -> dict[str, Any]:
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; the review export never overwrites")
    rows, skipped = [], {}
    items = review_items(archive)
    unknown = sorted({b["class_raw"] for r in items for b in r["item"]["boxes"]} - set(class_names))
    if unknown:
        raise ValueError(f"class names not in the weight's list {class_names}: {unknown}")
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "labels").mkdir(parents=True, exist_ok=True)
    for r in items:
        it = r["item"]
        if r["image"] is None:
            skipped[it["state"]] = skipped.get(it["state"], 0) + 1
            continue
        w, h = r["size"]
        img = cv2.imread(str(r["image"]))
        if img is None or img.shape[:2] != (h, w):
            raise ValueError(f"{r['image']}: unreadable or not {w}x{h} as the manifest says")
        stem = f"{r['mission_id']}__{it['id']}"
        shutil.copyfile(r["image"], out / "images" / f"{stem}.jpg")
        lines = []
        for b in it["boxes"]:
            cx, cy, bw, bh = b["bbox_xywh_px"]
            lines.append(f"{class_names.index(b['class_raw'])} {cx / w:.6f} {cy / h:.6f} {bw / w:.6f} {bh / h:.6f}")
        (out / "labels" / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        rows.append({"image": f"images/{stem}.jpg", "mission_id": r["mission_id"], "item_id": it["id"],
                     "criterion": it["criterion"], "count": it["count"], "sample_t": it["sample_t"],
                     "sample_frame": it["sample_frame"],
                     "track_ids": " ".join(map(str, it["track_ids"])),
                     "classes": " ".join(b["class_raw"] for b in it["boxes"]),
                     "confs": " ".join(f"{b['conf']:.3f}" for b in it["boxes"]),
                     "reason": it["reason"]})
    with open(out / "review.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["image", "mission_id", "item_id", "criterion", "count", "sample_t",
                                           "sample_frame", "track_ids", "classes", "confs", "reason"])
        wr.writeheader()
        wr.writerows(rows)
    (out / "classes.txt").write_text("\n".join(class_names) + "\n", encoding="utf-8")
    names = "\n".join(f"  {i}: {json.dumps(n)}" for i, n in enumerate(class_names))
    (out / "data.yaml").write_text(f"# {LABEL_NOTE}\npath: .\ntrain: images\nval: images\nnames:\n{names}\n",
                                   encoding="utf-8")
    return {"exported": len(rows), "skipped_without_image": skipped, "out": str(out)}
