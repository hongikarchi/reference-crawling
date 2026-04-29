#!/usr/bin/env python3
"""Quality checks for the canonical building artefact (data/canonical/canonical_buildings.json).

Run BEFORE Postgres migration. Each check returns one of:
  PASS  — invariant holds
  WARN  — soft-fail (e.g. orphan rate slightly outside expected band)
  FAIL  — hard fail (would corrupt downstream state if uploaded)

Console summary + JSON report at data/reports/canonical_qc.json.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from core import vocab
from canonical.schema import CanonicalBuilding

CANONICAL_PATH = "data/canonical/canonical_buildings.json"
DIVISARE_DB_PATH = "data/crawl/divisare.db"
REPORT_PATH = "data/reports/canonical_qc.json"

# Field-coverage thresholds — flag WARN if non-null share falls below.
COVERAGE_THRESHOLDS = {
    "name":                     0.99,
    "location_country":         0.90,
    "location_city":            0.85,
    "project_year":             0.70,   # divisare lite often misses year
    "architect_canonical_ids":  0.95,
    "architect_names":          0.95,
    "description":              0.50,   # only matched-with-metalocus rows have this
    "program":                  0.85,
    "atmosphere":               0.50,
    "image_paths":              0.50,
    "embedding":                0.99,
    "provenance":               0.99,
}

# Orphan ratio = metalocus-only rows (no Divisare match). Plan estimates 30-50%.
ORPHAN_BAND = (0.20, 0.60)


@dataclass
class CheckResult:
    name: str
    status: str          # PASS | WARN | FAIL
    metric: Any = None   # number, dict, or list
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VOCAB_FIELDS = (
    ("program",    vocab.PROGRAM),
    ("style",      vocab.STYLE),
    ("atmosphere", vocab.ATMOSPHERE),
    ("color_tone", vocab.COLOR_TONE),
)

_DATACLASS_FIELDS = {f.name: f for f in CanonicalBuilding.__dataclass_fields__.values()}


def _load_canonical(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected JSON list at root of {path}; got {type(data).__name__}")
    return data


def _is_orphan(row: dict) -> bool:
    return row.get("divisare_id") is None and row.get("metalocus_building_id") is not None


def _value_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)) and not value:
        return False
    return True


# ---------------------------------------------------------------------------
# Check 1: schema — every record round-trips through CanonicalBuilding
# ---------------------------------------------------------------------------

def check_schema(records: list[dict]) -> CheckResult:
    failures = []
    for i, row in enumerate(records):
        if not isinstance(row, dict):
            failures.append(f"row {i}: not a dict ({type(row).__name__})")
            continue
        unknown = set(row.keys()) - set(_DATACLASS_FIELDS)
        if unknown:
            failures.append(f"row {i} ({row.get('divisare_id')!r}): unknown keys {sorted(unknown)}")
        try:
            CanonicalBuilding.from_dict(row)
        except (TypeError, ValueError) as exc:
            failures.append(f"row {i} ({row.get('divisare_id')!r}): {type(exc).__name__}: {exc}")
    if failures:
        return CheckResult("schema", "FAIL",
                           metric={"total_rows": len(records), "failures": len(failures)},
                           details=failures[:25])
    return CheckResult("schema", "PASS", metric={"total_rows": len(records)})


# ---------------------------------------------------------------------------
# Check 2: pk_unique — divisare_id duplicates would corrupt upserts
# ---------------------------------------------------------------------------

def check_pk_unique(records: list[dict]) -> CheckResult:
    seen: dict[Any, list[int]] = {}
    for i, row in enumerate(records):
        did = row.get("divisare_id")
        if did is None:
            continue
        seen.setdefault(did, []).append(i)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        details = [f"divisare_id={k} at rows {v[:5]}" for k, v in list(dupes.items())[:10]]
        return CheckResult("pk_unique", "FAIL",
                           metric={"unique_ids": len(seen), "duplicate_ids": len(dupes)},
                           details=details)
    return CheckResult("pk_unique", "PASS",
                       metric={"unique_divisare_ids": len(seen)})


# ---------------------------------------------------------------------------
# Check 3: field_coverage — % non-null per field, vs threshold
# ---------------------------------------------------------------------------

def check_field_coverage(records: list[dict]) -> CheckResult:
    n = len(records)
    if n == 0:
        return CheckResult("field_coverage", "WARN", metric={}, details=["no records"])
    coverage: dict[str, dict] = {}
    failures = []
    for fname, threshold in COVERAGE_THRESHOLDS.items():
        present = sum(1 for r in records if _value_is_present(r.get(fname)))
        ratio = present / n
        coverage[fname] = {
            "present": present, "total": n,
            "ratio": round(ratio, 4), "threshold": threshold,
            "ok": ratio >= threshold,
        }
        if ratio < threshold:
            failures.append(f"{fname}: {ratio:.1%} < {threshold:.0%} threshold")
    status = "WARN" if failures else "PASS"
    return CheckResult("field_coverage", status, metric=coverage, details=failures)


# ---------------------------------------------------------------------------
# Check 4: provenance_audit — every populated field must be tracked
# ---------------------------------------------------------------------------

# Fields that don't carry a source (book-keeping / IDs / structural).
_NON_PROVENANCE_FIELDS = {
    "divisare_id", "divisare_slug", "metalocus_building_id",
    "embedding", "vocab_version", "prompt_version", "provenance",
    "confidence_tier",   # set by reality_filter, not a content field
}


def check_provenance_audit(records: list[dict]) -> CheckResult:
    bad = []
    for i, row in enumerate(records):
        prov = row.get("provenance") or {}
        if not isinstance(prov, dict):
            bad.append(f"row {i}: provenance is not a dict")
            continue
        for fname in _DATACLASS_FIELDS:
            if fname in _NON_PROVENANCE_FIELDS:
                continue
            value = row.get(fname)
            if _value_is_present(value) and fname not in prov:
                bad.append(f"row {i} ({row.get('divisare_id')!r}): "
                           f"{fname!r} present but missing from provenance")
                break  # one issue per row is enough
    if bad:
        return CheckResult("provenance_audit", "FAIL",
                           metric={"rows_missing_provenance": len(bad)},
                           details=bad[:25])
    return CheckResult("provenance_audit", "PASS",
                       metric={"rows_audited": len(records)})


# ---------------------------------------------------------------------------
# Check 5: orphan_ratio — % of metalocus-only rows
# ---------------------------------------------------------------------------

def check_orphan_ratio(records: list[dict]) -> CheckResult:
    if not records:
        return CheckResult("orphan_ratio", "WARN", metric={"orphans": 0, "total": 0})
    orphans = sum(1 for r in records if _is_orphan(r))
    matched = sum(1 for r in records if r.get("divisare_id") is not None)
    divisare_only = sum(1 for r in records if r.get("divisare_id") is not None
                        and r.get("metalocus_building_id") is None)
    n = len(records)
    ratio = orphans / n
    lo, hi = ORPHAN_BAND
    in_band = lo <= ratio <= hi
    return CheckResult(
        "orphan_ratio",
        "PASS" if in_band else "WARN",
        metric={
            "total": n, "orphans": orphans, "matched": matched,
            "divisare_only": divisare_only,
            "orphan_ratio": round(ratio, 4),
            "expected_band": [lo, hi],
        },
        details=[] if in_band else [
            f"orphan ratio {ratio:.1%} outside expected band {lo:.0%}-{hi:.0%}"
        ],
    )


# ---------------------------------------------------------------------------
# Check 6: architect_link — architect_canonical_ids must exist in divisare_architects
# ---------------------------------------------------------------------------

def check_architect_link(records: list[dict], divisare_db: str = DIVISARE_DB_PATH) -> CheckResult:
    if not os.path.exists(divisare_db):
        return CheckResult("architect_link", "WARN",
                           metric={"divisare_db": divisare_db},
                           details=[f"divisare DB not found at {divisare_db} — skipped"])
    referenced: set[int] = set()
    for r in records:
        for aid in r.get("architect_canonical_ids") or []:
            if isinstance(aid, int):
                referenced.add(aid)
    if not referenced:
        return CheckResult("architect_link", "WARN",
                           metric={"referenced_ids": 0},
                           details=["no architect_canonical_ids populated yet"])
    with sqlite3.connect(divisare_db) as conn:
        rows = conn.execute(
            "SELECT id FROM divisare_architects WHERE id IN ({})".format(
                ",".join("?" * len(referenced))
            ),
            tuple(referenced),
        ).fetchall()
    existing = {row[0] for row in rows}
    missing = referenced - existing
    if missing:
        return CheckResult(
            "architect_link", "FAIL",
            metric={"referenced": len(referenced), "missing": len(missing)},
            details=[f"missing architect IDs: {sorted(missing)[:25]}"],
        )
    return CheckResult("architect_link", "PASS",
                       metric={"referenced": len(referenced),
                               "all_resolved_in_divisare_db": True})


# ---------------------------------------------------------------------------
# Check 7: image_existence — local image_paths must point to real files
# ---------------------------------------------------------------------------

def check_image_existence(records: list[dict], project_root: str = ".") -> CheckResult:
    missing = []
    total_paths = 0
    for r in records:
        for path in r.get("image_paths") or []:
            total_paths += 1
            full = path if os.path.isabs(path) else os.path.join(project_root, path)
            if not os.path.exists(full):
                missing.append(path)
                if len(missing) >= 50:
                    break
        if len(missing) >= 50:
            break
    if missing:
        return CheckResult(
            "image_existence", "FAIL",
            metric={"total_paths": total_paths, "missing": len(missing)},
            details=[f"missing: {p}" for p in missing[:20]],
        )
    return CheckResult("image_existence", "PASS",
                       metric={"total_paths": total_paths, "missing": 0})


# ---------------------------------------------------------------------------
# Check 8: embedding_shape — must be 384-dim list of floats
# ---------------------------------------------------------------------------

EXPECTED_EMBEDDING_DIM = 384


def check_embedding_shape(records: list[dict]) -> CheckResult:
    bad: list[str] = []
    n_with_emb = 0
    for i, r in enumerate(records):
        emb = r.get("embedding")
        if emb is None:
            continue
        n_with_emb += 1
        if not isinstance(emb, list):
            bad.append(f"row {i}: embedding type {type(emb).__name__}, not list")
            continue
        if len(emb) != EXPECTED_EMBEDDING_DIM:
            bad.append(f"row {i}: embedding dim {len(emb)} != {EXPECTED_EMBEDDING_DIM}")
            continue
        if any(not isinstance(v, (int, float)) for v in emb[:8]):
            bad.append(f"row {i}: embedding has non-numeric elements")
    if bad:
        return CheckResult("embedding_shape", "FAIL",
                           metric={"rows_with_embedding": n_with_emb,
                                   "malformed": len(bad)},
                           details=bad[:20])
    return CheckResult("embedding_shape", "PASS",
                       metric={"rows_with_embedding": n_with_emb,
                               "dim": EXPECTED_EMBEDDING_DIM})


# ---------------------------------------------------------------------------
# Check 9: vocab_validity — program/style/atmosphere/color_tone must be in vocab
# ---------------------------------------------------------------------------

def check_vocab_validity(records: list[dict]) -> CheckResult:
    bad_per_field: dict[str, list[str]] = {fname: [] for fname, _ in _VOCAB_FIELDS}
    for i, r in enumerate(records):
        for fname, allowed in _VOCAB_FIELDS:
            val = r.get(fname)
            if val and not vocab.is_valid(fname, val):
                bad_per_field[fname].append(f"row {i} ({r.get('divisare_id')!r}): {val!r}")
    flat_bad = sum(len(v) for v in bad_per_field.values())
    if flat_bad:
        details = []
        for fname, items in bad_per_field.items():
            if items:
                details.append(f"{fname}: {len(items)} invalid (e.g. {items[0]})")
        return CheckResult("vocab_validity", "WARN",
                           metric={f: len(v) for f, v in bad_per_field.items()},
                           details=details)
    return CheckResult("vocab_validity", "PASS",
                       metric={"vocab_version": vocab.VOCAB_VERSION})


_VALID_TIERS = {"T1", "T2", "T3"}


def check_confidence_tier(records: list[dict]) -> CheckResult:
    """Phase 14a invariant: every kept row has confidence_tier ∈ {T1,T2,T3}.

    Pre-Phase-14 canonical files had no `confidence_tier` field — those
    pass with an INFO-level result (not a failure). Mixed populations
    (some rows have it, some don't) raise WARN.
    """
    with_tier = [r for r in records if r.get("confidence_tier")]
    without_tier = [r for r in records if not r.get("confidence_tier")]
    if not with_tier:
        return CheckResult("confidence_tier", "PASS",
                           metric={"phase_14_field_absent": True,
                                   "rows_without_tier": len(without_tier)},
                           details=["legacy file pre-Phase-14a; no confidence_tier"])
    invalid = [r for r in with_tier if r["confidence_tier"] not in _VALID_TIERS]
    tier_counts = {"T1": 0, "T2": 0, "T3": 0}
    for r in with_tier:
        tier_counts[r["confidence_tier"]] = tier_counts.get(r["confidence_tier"], 0) + 1
    if without_tier:
        return CheckResult("confidence_tier", "WARN",
                           metric={**tier_counts, "without_tier": len(without_tier)},
                           details=[f"{len(without_tier)} rows missing confidence_tier "
                                    f"(should be set on every strict-mode KEEP)"])
    if invalid:
        return CheckResult("confidence_tier", "FAIL",
                           metric={**tier_counts,
                                   "invalid_values": [r['confidence_tier'] for r in invalid[:5]]},
                           details=[f"{len(invalid)} rows have an out-of-vocab tier value"])
    # T1 fraction guidance: aim for ≥ 30% cross-source confirmed
    t1_frac = tier_counts["T1"] / len(records) if records else 0
    if t1_frac < 0.30:
        return CheckResult("confidence_tier", "WARN",
                           metric={**tier_counts, "t1_fraction": round(t1_frac, 3)},
                           details=[f"T1 (cross-source) fraction {t1_frac:.1%} below 30% target — "
                                    f"means most rows are arch-only / single-source"])
    return CheckResult("confidence_tier", "PASS",
                       metric={**tier_counts, "t1_fraction": round(t1_frac, 3)})


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

ALL_CHECKS = [
    ("schema",            check_schema),
    ("pk_unique",         check_pk_unique),
    ("field_coverage",    check_field_coverage),
    ("provenance_audit",  check_provenance_audit),
    ("orphan_ratio",      check_orphan_ratio),
    ("architect_link",    check_architect_link),
    ("image_existence",   check_image_existence),
    ("embedding_shape",   check_embedding_shape),
    ("vocab_validity",    check_vocab_validity),
    ("confidence_tier",   check_confidence_tier),
]


def run_all(canonical_path: str = CANONICAL_PATH,
            report_path: str = REPORT_PATH) -> dict:
    records = _load_canonical(canonical_path)
    print(f"loaded {len(records)} canonical records from {canonical_path}\n")

    results: list[CheckResult] = []
    for name, fn in ALL_CHECKS:
        try:
            result = fn(records)
        except Exception as exc:
            result = CheckResult(name, "FAIL",
                                 metric={"exception": type(exc).__name__},
                                 details=[str(exc)])
        results.append(result)
        _print_result(result)

    overall = _overall_status(results)
    report = {
        "canonical_file": canonical_path,
        "record_count": len(records),
        "overall_status": overall,
        "checks": [r.to_dict() for r in results],
    }

    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n→ report saved to {report_path}")
    print(f"OVERALL: {overall}")
    return report


def _print_result(r: CheckResult) -> None:
    sym = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(r.status, "?")
    print(f"  {sym} {r.status:5s}  {r.name}")
    if r.name == "field_coverage" and isinstance(r.metric, dict):
        for fname, m in r.metric.items():
            mark = "✓" if m["ok"] else "⚠"
            print(f"      {mark}  {fname:30s} {m['present']:6d}/{m['total']:6d} "
                  f"({m['ratio']:.1%}, threshold {m['threshold']:.0%})")
    elif r.metric and r.status != "PASS":
        print(f"      metric: {r.metric}")
    for line in r.details[:5]:
        print(f"      · {line}")
    if len(r.details) > 5:
        print(f"      … and {len(r.details) - 5} more")


def _overall_status(results: list[CheckResult]) -> str:
    statuses = [r.status for r in results]
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="QC checks for canonical_buildings.json")
    p.add_argument("file", nargs="?", default=CANONICAL_PATH,
                   help=f"canonical JSON path (default {CANONICAL_PATH})")
    p.add_argument("--report", default=REPORT_PATH,
                   help=f"report output path (default {REPORT_PATH})")
    args = p.parse_args(argv)
    if not os.path.exists(args.file):
        print(f"canonical file not found: {args.file}", file=sys.stderr)
        return 2
    report = run_all(args.file, args.report)
    return 0 if report["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
