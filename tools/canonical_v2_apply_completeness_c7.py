#!/usr/bin/env python3
"""Apply C6.5 exact-source reviewed updates to local canonical artifacts.

This creates a `.completeness_c7` artifact family from the C6 baseline and an
affected-row subset. It intentionally does not write Neon/R2/upload paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STRICT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c6.json"
DEFAULT_EMBEDDED_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c6.json"
DEFAULT_STRICT_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c7.json"
DEFAULT_EMBEDDED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7.json"
DEFAULT_AFFECTED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7_affected.json"
DEFAULT_AFFECTED_CIDS = ROOT / "data/canonical/country_conflict_refresh/completeness_c7_affected_cids.json"
DEFAULT_REVIEW = ROOT / "data/reports/canonical_v2_c6_exact_source_review.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_completeness_c7_apply_report.json"

ALLOWED_FIELDS = {"location_country", "location_city", "project_year"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def build_updates(review_path: Path) -> dict[str, dict[str, Any]]:
    review = load_json(review_path)
    updates: dict[str, dict[str, Any]] = {}
    for item in review.get("rows") or []:
        if item.get("verdict") != "apply":
            continue
        cid = str(item.get("canonical_bld_id") or "")
        field_updates = item.get("field_updates") or {}
        if not cid:
            raise SystemExit("C7 review row missing canonical_bld_id")
        unknown = sorted(set(field_updates) - ALLOWED_FIELDS)
        if unknown:
            raise SystemExit(f"C7 review row {cid} has unsupported fields: {unknown}")
        if field_updates:
            updates[cid] = dict(field_updates)
    if not updates:
        raise SystemExit("no C7 updates found")
    return updates


def apply_updates(data: dict[str, Any], updates: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: Counter[str] = Counter()
    changed_rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in data.get("buildings") or []:
        cid = str(row.get("canonical_bld_id") or "")
        row_updates = updates.get(cid)
        if not row_updates:
            continue
        seen.add(cid)

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        skipped: dict[str, Any] = {}
        for field, proposed in row_updates.items():
            current = row.get(field)
            if current in (None, "", []):
                row[field] = proposed
                before[field] = current
                after[field] = proposed
                counts[field] += 1
            elif current == proposed:
                skipped[field] = current
                counts[f"{field}_skipped_same_value"] += 1
            else:
                skipped[field] = current
                counts[f"{field}_skipped_not_empty"] += 1

        if after or skipped:
            changed_rows.append(
                {
                    "canonical_bld_id": cid,
                    "name": row.get("name"),
                    "source_refs": row.get("source_refs") or {},
                    "before": before,
                    "after": after,
                    "skipped": skipped,
                }
            )

    return data, {
        "field_updates": dict(counts),
        "changed_row_count": len([row for row in changed_rows if row["after"]]),
        "changed_rows": changed_rows,
        "missing_update_cids": sorted(set(updates) - seen),
    }


def subset_embedded(embedded: dict[str, Any], affected_cids: set[str]) -> dict[str, Any]:
    return {
        "buildings": [
            row
            for row in embedded.get("buildings") or []
            if str(row.get("canonical_bld_id") or "") in affected_cids
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply C6.5 exact-source updates locally.")
    parser.add_argument("--strict-input", type=Path, default=DEFAULT_STRICT_INPUT)
    parser.add_argument("--embedded-input", type=Path, default=DEFAULT_EMBEDDED_INPUT)
    parser.add_argument("--strict-output", type=Path, default=DEFAULT_STRICT_OUTPUT)
    parser.add_argument("--embedded-output", type=Path, default=DEFAULT_EMBEDDED_OUTPUT)
    parser.add_argument("--affected-output", type=Path, default=DEFAULT_AFFECTED_OUTPUT)
    parser.add_argument("--affected-cids", type=Path, default=DEFAULT_AFFECTED_CIDS)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    updates = build_updates(args.review)
    strict, strict_report = apply_updates(load_json(args.strict_input), updates)
    embedded, embedded_report = apply_updates(load_json(args.embedded_input), updates)
    affected_cids = sorted(
        {
            row["canonical_bld_id"]
            for row in embedded_report["changed_rows"]
            if row["after"]
        }
    )

    write_json(args.strict_output, strict)
    write_json(args.embedded_output, embedded)
    write_json(args.affected_output, subset_embedded(embedded, set(affected_cids)))
    write_json(args.affected_cids, {"affected_cids": affected_cids})

    report = {
        "status": "PASS",
        "mode": "local_artifact_patch_only",
        "strict_input": str(args.strict_input),
        "embedded_input": str(args.embedded_input),
        "strict_output": str(args.strict_output),
        "embedded_output": str(args.embedded_output),
        "affected_output": str(args.affected_output),
        "affected_cids": str(args.affected_cids),
        "planned_update_cids": len(updates),
        "affected_cid_count": len(affected_cids),
        "strict": strict_report,
        "embedded": embedded_report,
        "writes": "new artifact files only; no Neon/R2/upload mutation",
    }
    write_json(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "planned_update_cids": report["planned_update_cids"],
                "affected_cid_count": report["affected_cid_count"],
                "strict_field_updates": strict_report["field_updates"],
                "embedded_field_updates": embedded_report["field_updates"],
                "writes": report["writes"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
