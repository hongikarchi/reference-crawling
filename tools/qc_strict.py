#!/usr/bin/env python3
"""Strict-aware QC for canonical_buildings_strict_v2.json (Phase 14a+ schema).

Replaces the legacy `canonical/qc.py` checks that were written for the
Phase 4-9 CanonicalBuilding dataclass (name_en/divisare_id/etc).

Run:
    python3 tools/qc_strict.py
    python3 tools/qc_strict.py --input data/canonical/canonical_buildings_strict_v2.json

Schema invariants checked:
  - schema:                 every row has the canonical_bld_id + name fields
  - pk_unique:              canonical_bld_id is unique
  - field_coverage:         coverage thresholds per field
  - architect_link:         architect_canonical_ids resolve to architects_canonical.json
  - vocab_validity:         program/style/color_tone/atmosphere ∈ vocab v2
  - confidence_tier:        T1/T2/T3 distribution
  - covers_by_type:         5 keys present, exterior most common
  - image_derived:          d2 enrichment landed
  - source_refs_unique:     no duplicate (source, source_id) across canonicals
  - architect_consistency:  architects_text and architect_names approximately agree
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/canonical/canonical_buildings_strict_v2.json"
ARCH_PATH = ROOT / "data/canonical/architects_canonical.json"
REPORT_PATH = ROOT / "data/reports/canonical_strict_qc.json"

PROGRAM = {"Education","Healthcare","Hospitality","Housing","Infrastructure",
           "Landscape","Mixed Use","Museum","Office","Other","Public",
           "Religion","Sports","Transport"}
STYLE = {"Brutalist","Contemporary","Deconstructivist","High-Tech","Industrial",
         "Minimalist","Modernist","Neo-Classical","Organic","Parametric",
         "Postmodern","Vernacular"}
COLOR = {"Cool","Dark","Earth","Light","Monochrome","Neutral","Vibrant","Warm"}
ATMOS = {"Contemplative","Dynamic","Futuristic","Industrial","Intimate",
         "Monumental","Playful","Raw","Rustic","Serene","Urban","Warm"}
IMG_TYPES = {"exterior","interior","drawing","aerial","detail"}


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    metric: dict
    details: list

    def to_dict(self):
        return {"name": self.name, "status": self.status,
                "metric": self.metric, "details": self.details[:25]}


def check_schema(buildings: list[dict]) -> CheckResult:
    required = ["canonical_bld_id", "name"]
    bad = []
    for i, b in enumerate(buildings):
        for k in required:
            if not b.get(k):
                bad.append(f"row {i}: missing {k}")
                break
    return CheckResult("schema", "FAIL" if bad else "PASS",
                       {"total": len(buildings), "missing_required": len(bad)}, bad)


def check_pk_unique(buildings: list[dict]) -> CheckResult:
    seen = Counter(b.get("canonical_bld_id") for b in buildings if b.get("canonical_bld_id"))
    dups = [cid for cid, n in seen.items() if n > 1]
    return CheckResult("pk_unique", "FAIL" if dups else "PASS",
                       {"unique_ids": len(seen), "duplicates": len(dups)},
                       [f"{cid} ×{seen[cid]}" for cid in dups[:10]])


def check_field_coverage(buildings: list[dict]) -> CheckResult:
    fields = {
        "name": 0.99, "location_city": 0.85, "location_country": 0.90,
        "project_year": 0.85, "program": 0.95, "style": 0.95,
        "color_tone": 0.95, "atmosphere": 0.95,
        "material_visual": 0.95, "visual_description": 0.95,
        "architect_canonical_ids": 0.85,
        "covers_by_type": 0.99, "confidence_tier": 0.99,
        "all_images": 0.95,
    }
    metric = {}
    failed = []
    for f, threshold in fields.items():
        present = sum(1 for b in buildings if _is_present(b.get(f)))
        ratio = present / len(buildings) if buildings else 0
        ok = ratio >= threshold
        metric[f] = {"present": present, "ratio": round(ratio, 4),
                     "threshold": threshold, "ok": ok}
        if not ok:
            failed.append(f"{f}: {ratio*100:.1f}% < {threshold*100:.0f}%")
    status = "WARN" if failed else "PASS"
    return CheckResult("field_coverage", status, metric, failed)


def _is_present(v) -> bool:
    if v is None: return False
    if isinstance(v, (list, dict, str)) and not v: return False
    return True


def check_architect_link(buildings: list[dict]) -> CheckResult:
    if not ARCH_PATH.exists():
        return CheckResult("architect_link", "WARN",
                           {"reason": "architects_canonical.json not found"}, [])
    arch = json.load(ARCH_PATH.open())
    valid_ids = {c["canonical_arch_id"] for c in arch["clusters"]}
    bad = []
    referenced = 0
    for i, b in enumerate(buildings):
        for aid in b.get("architect_canonical_ids") or []:
            referenced += 1
            if aid not in valid_ids:
                bad.append(f"row {i} ({b.get('canonical_bld_id')}): "
                           f"unknown arch_id {aid}")
                if len(bad) >= 25: break
    status = "FAIL" if bad else "PASS"
    return CheckResult("architect_link", status,
                       {"referenced_ids": referenced, "valid_pool": len(valid_ids),
                        "unknown_ids": len(bad)}, bad)


def check_vocab_validity(buildings: list[dict]) -> CheckResult:
    bad = []
    vmap = {"program": PROGRAM, "style": STYLE,
            "color_tone": COLOR, "atmosphere": ATMOS}
    for i, b in enumerate(buildings):
        for f, vocab in vmap.items():
            v = b.get(f)
            if v is not None and v not in vocab:
                bad.append(f"row {i} ({b.get('canonical_bld_id')}): "
                           f"{f}={v!r} not in vocab")
                if len(bad) >= 25: break
        if len(bad) >= 25: break
    status = "FAIL" if bad else "PASS"
    return CheckResult("vocab_validity", status,
                       {"vocab_version": "v2", "violations": len(bad)}, bad)


def check_confidence_tier(buildings: list[dict]) -> CheckResult:
    counts = Counter(b.get("confidence_tier") for b in buildings)
    total = len(buildings)
    t1_frac = counts.get("T1", 0) / total if total else 0
    return CheckResult("confidence_tier", "PASS",
                       {"T1": counts.get("T1", 0), "T2": counts.get("T2", 0),
                        "T3": counts.get("T3", 0), "t1_fraction": round(t1_frac, 4)},
                       [])


def check_covers_by_type(buildings: list[dict]) -> CheckResult:
    bad = []
    type_counts = Counter()
    for i, b in enumerate(buildings):
        cv = b.get("covers_by_type") or {}
        if not isinstance(cv, dict):
            bad.append(f"row {i}: covers_by_type not dict")
            continue
        if set(cv.keys()) != IMG_TYPES:
            bad.append(f"row {i} ({b.get('canonical_bld_id')}): "
                       f"keys={set(cv.keys())} != {IMG_TYPES}")
        for t, url in cv.items():
            if url:
                type_counts[t] += 1
        if len(bad) >= 25: break
    status = "FAIL" if bad else "PASS"
    return CheckResult("covers_by_type", status,
                       {"type_coverage": dict(type_counts),
                        "total": len(buildings),
                        "with_exterior": type_counts.get("exterior", 0)},
                       bad)


def check_image_derived(buildings: list[dict]) -> CheckResult:
    has = sum(1 for b in buildings
              if (b.get("image_derived") or {}).get("style"))
    ratio = has / len(buildings) if buildings else 0
    status = "PASS" if ratio >= 0.95 else "WARN"
    return CheckResult("image_derived", status,
                       {"with_image_style": has, "ratio": round(ratio, 4)}, [])


def check_source_refs_unique(buildings: list[dict]) -> CheckResult:
    seen = defaultdict(list)  # (source, source_id) -> [cid, ...]
    for b in buildings:
        cid = b.get("canonical_bld_id")
        for src, ids in (b.get("source_refs") or {}).items():
            for sid in ids:
                seen[(src, str(sid))].append(cid)
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    status = "FAIL" if dups else "PASS"
    samples = [f"{k} → {v[:3]}" for k, v in list(dups.items())[:10]]
    return CheckResult("source_refs_unique", status,
                       {"total_refs": len(seen),
                        "duplicate_refs": len(dups)}, samples)


def check_architect_consistency(buildings: list[dict]) -> CheckResult:
    """architect_names should be subset of what architects_text mentions."""
    inconsistent = 0
    for b in buildings:
        names = b.get("architect_names") or []
        text = (b.get("architects_text") or "").lower()
        if not names or not text:
            continue
        # Soft check: at least one name's first word appears in text
        any_match = any(n and n.split()[0].lower() in text for n in names if n)
        if not any_match:
            inconsistent += 1
    total = sum(1 for b in buildings
                if b.get("architect_names") and b.get("architects_text"))
    ratio = inconsistent / total if total else 0
    status = "WARN" if ratio > 0.10 else "PASS"
    return CheckResult("architect_consistency", status,
                       {"checked": total, "inconsistent": inconsistent,
                        "inconsistent_ratio": round(ratio, 4)}, [])


CHECKS = [
    check_schema, check_pk_unique, check_field_coverage,
    check_architect_link, check_vocab_validity, check_confidence_tier,
    check_covers_by_type, check_image_derived, check_source_refs_unique,
    check_architect_consistency,
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(DEFAULT_INPUT))
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: {in_path} not found", file=sys.stderr)
        sys.exit(2)

    print(f"loading {in_path}")
    data = json.load(in_path.open())
    buildings = data.get("buildings") or data.get("clusters") or []
    print(f"  {len(buildings):,} buildings\n")

    results = []
    for fn in CHECKS:
        r = fn(buildings)
        sym = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[r.status]
        print(f"  {sym} {r.status:5s} {r.name}")
        if r.details:
            for d in r.details[:5]:
                print(f"      · {d}")
        results.append(r)

    overall = "PASS"
    if any(r.status == "FAIL" for r in results): overall = "FAIL"
    elif any(r.status == "WARN" for r in results): overall = "WARN"

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w") as f:
        json.dump({
            "input": str(in_path),
            "record_count": len(buildings),
            "overall_status": overall,
            "checks": [r.to_dict() for r in results],
        }, f, indent=2, ensure_ascii=False)

    print(f"\n→ report: {REPORT_PATH}")
    print(f"OVERALL: {overall}")
    sys.exit(0 if overall != "FAIL" else 1)


if __name__ == "__main__":
    main()
