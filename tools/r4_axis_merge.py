#!/usr/bin/env python3
"""Shared R4 axis logic: era derivation, Unknown→NULL, text+vision merge.

Single home for everything that must stay consistent across the text runner,
the vision runner, the upload validator/loader overlay, and the Neon deploy —
so an artifact reload reproduces the deployed Neon state (idempotency
precedent: canonical_v2_upload_validator.reclassify_material).

Merge policy (per axis, applied in merge_records):
  roof_type          vision wins when it answered; text fills otherwise
                     (purely visual attribute — photos beat prose)
  structural_system  text wins when it answered; vision fills the gaps
                     (a description saying "CLT structure" beats a photo guess)
  facade_pattern     text wins; vision fills the gaps
  scale              text only (vision sees one photo, text sees the program)
  era                NOT merged here — derived from project_year at apply time

CLI: merge the two sidecars into r4_results.merged.jsonl + coverage report.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import vocab  # noqa: E402

TEXT_SIDECAR = ROOT / "data/canonical/r4_results.text.jsonl"
VISION_SIDECAR = ROOT / "data/canonical/r4_results.vision.jsonl"
MERGED_SIDECAR = ROOT / "data/canonical/r4_results.merged.jsonl"

LLM_AXES = ("scale", "structural_system", "roof_type", "facade_pattern")
R4_AXES = ("era",) + LLM_AXES  # Neon column order for the backfill

VOCAB_BY_AXIS = {
    "scale": vocab.SCALE,
    "structural_system": vocab.STRUCTURAL_SYSTEM,
    "roof_type": vocab.ROOF_TYPE,
    "facade_pattern": vocab.FACADE_PATTERN,
}

# vision answers only these; scale stays text-only
VISION_AXES = ("roof_type", "structural_system", "facade_pattern")
VISION_WINS = frozenset({"roof_type"})

ERA_BUCKETS = (
    (1900, "Pre-1900"), (1945, "1900-1945"), (1980, "1945-1980"),
    (2000, "1980-2000"), (2015, "2000-2015"), (9999, "2015+"),
)
ERA_VALUES = tuple(label for _, label in ERA_BUCKETS)


def era_from_year(year) -> str | None:
    if not isinstance(year, int):
        return None
    for upper, label in ERA_BUCKETS:
        if year < upper:
            return label
    return None


def normalize_axis_value(axis: str, value) -> str | None:
    """Unknown / out-of-vocab / empty → None; valid vocab value passes."""
    if not value or value == "Unknown":
        return None
    return value if value in VOCAB_BY_AXIS[axis] else None


def load_sidecar(path: Path) -> dict[str, dict]:
    """JSONL of runner results → {cid: {axis: normalized value}} (ok rows only)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") != "ok":
                continue
            cid = row.get("canonical_bld_id")
            tags = row.get("tags") or {}
            if not cid:
                continue
            out[cid] = {
                axis: normalize_axis_value(axis, tags.get(axis))
                for axis in LLM_AXES
                if axis in tags
            }
    return out


def merge_records(text: dict | None, vision: dict | None) -> tuple[dict, dict]:
    """Apply the per-axis merge policy. Returns (values, sources)."""
    text = text or {}
    vision = vision or {}
    values: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for axis in LLM_AXES:
        t, v = text.get(axis), vision.get(axis)
        if axis in VISION_WINS:
            value, source = (v, "vision") if v is not None else (t, "text")
        else:
            value, source = (t, "text") if t is not None else (v, "vision")
        if value is not None:
            values[axis] = value
            sources[axis] = source
    return values, sources


def load_merged(path: Path = MERGED_SIDECAR) -> dict[str, dict]:
    """Merged sidecar → {cid: {axis: value, 'sources': {...}}}."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row["canonical_bld_id"]] = row
    return out


def run_merge(text_path: Path, vision_path: Path, out_path: Path) -> dict:
    text = load_sidecar(text_path)
    vision = load_sidecar(vision_path)
    cids = sorted(set(text) | set(vision))
    coverage: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    with out_path.open("w", encoding="utf-8") as fout:
        for cid in cids:
            values, sources = merge_records(text.get(cid), vision.get(cid))
            for axis in values:
                coverage[axis] += 1
                source_counts[f"{axis}:{sources[axis]}"] += 1
            fout.write(json.dumps(
                {"canonical_bld_id": cid, **values, "sources": sources},
                ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "cids_total": len(cids),
        "cids_text": len(text),
        "cids_vision": len(vision),
        "coverage": {axis: coverage.get(axis, 0) for axis in LLM_AXES},
        "coverage_pct": {
            axis: round(coverage.get(axis, 0) / len(cids), 3) if cids else None
            for axis in LLM_AXES
        },
        "sources": dict(sorted(source_counts.items())),
        "output": str(out_path.relative_to(ROOT)),
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", type=Path, default=TEXT_SIDECAR)
    ap.add_argument("--vision", type=Path, default=VISION_SIDECAR)
    ap.add_argument("--output", type=Path, default=MERGED_SIDECAR)
    args = ap.parse_args()
    report = run_merge(args.text, args.vision, args.output)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
