#!/usr/bin/env python3
"""Apply approved C3 completeness backfills to derived canonical artifacts.

Inputs are read-only C2.5/C2.6 reports. Outputs are new `.completeness_c3`
artifacts; the resume10 complete artifacts are never overwritten.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STRICT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json"
DEFAULT_EMBEDDED_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_STRICT_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c3.json"
DEFAULT_EMBEDDED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3.json"
DEFAULT_AFFECTED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3_affected.json"
DEFAULT_AFFECTED_CIDS = ROOT / "data/canonical/country_conflict_refresh/completeness_c3_affected_cids.json"
DEFAULT_YEAR_REPORT = ROOT / "data/reports/canonical_v2_review_rule_candidates.json"
DEFAULT_LOCATION_REPORT = ROOT / "data/reports/canonical_v2_llm_location_adjudication.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_completeness_c3_apply_report.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def build_updates(year_report: Path, location_report: Path) -> dict[str, dict[str, Any]]:
    updates: dict[str, dict[str, Any]] = defaultdict(dict)

    year_data = load_json(year_report)
    for item in year_data.get("safe_after_policy") or []:
        if item.get("field") != "project_year":
            continue
        cid = str(item.get("canonical_bld_id") or "")
        proposed = item.get("proposed")
        if cid and isinstance(proposed, int):
            updates[cid]["project_year"] = proposed

    location_data = load_json(location_report)
    for item in location_data.get("decisions") or []:
        if not item.get("should_apply_city"):
            continue
        cid = str(item.get("canonical_bld_id") or "")
        proposed = item.get("proposed_city")
        if cid and isinstance(proposed, str) and proposed.strip():
            updates[cid]["location_city"] = proposed.strip()

    return dict(updates)


def apply_updates(data: dict[str, Any], updates: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: Counter[str] = Counter()
    changed_rows: list[dict[str, Any]] = []
    seen_updates: set[str] = set()

    for row in data.get("buildings") or []:
        cid = str(row.get("canonical_bld_id") or "")
        row_updates = updates.get(cid)
        if not row_updates:
            continue
        seen_updates.add(cid)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for field, value in row_updates.items():
            current = row.get(field)
            if current in (None, "", []) and value not in (None, "", []):
                before[field] = current
                row[field] = value
                after[field] = value
                counts[field] += 1
            else:
                counts[f"{field}_skipped_not_empty"] += 1
        if after:
            changed_rows.append(
                {
                    "canonical_bld_id": cid,
                    "name": row.get("name"),
                    "source_refs": row.get("source_refs") or {},
                    "before": before,
                    "after": after,
                }
            )

    missing_update_cids = sorted(set(updates) - seen_updates)
    report = {
        "field_updates": dict(counts),
        "changed_row_count": len(changed_rows),
        "changed_rows": changed_rows,
        "missing_update_cids": missing_update_cids,
    }
    return data, report


def subset_embedded(embedded: dict[str, Any], changed_cids: set[str]) -> dict[str, Any]:
    return {
        "buildings": [
            row
            for row in embedded.get("buildings") or []
            if str(row.get("canonical_bld_id") or "") in changed_cids
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply approved C3 completeness updates to new artifacts.")
    parser.add_argument("--strict-input", type=Path, default=DEFAULT_STRICT_INPUT)
    parser.add_argument("--embedded-input", type=Path, default=DEFAULT_EMBEDDED_INPUT)
    parser.add_argument("--strict-output", type=Path, default=DEFAULT_STRICT_OUTPUT)
    parser.add_argument("--embedded-output", type=Path, default=DEFAULT_EMBEDDED_OUTPUT)
    parser.add_argument("--affected-output", type=Path, default=DEFAULT_AFFECTED_OUTPUT)
    parser.add_argument("--affected-cids", type=Path, default=DEFAULT_AFFECTED_CIDS)
    parser.add_argument("--year-report", type=Path, default=DEFAULT_YEAR_REPORT)
    parser.add_argument("--location-report", type=Path, default=DEFAULT_LOCATION_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    updates = build_updates(args.year_report, args.location_report)
    strict, strict_report = apply_updates(load_json(args.strict_input), updates)
    embedded, embedded_report = apply_updates(load_json(args.embedded_input), updates)

    strict_changed = {
        str(row["canonical_bld_id"])
        for row in strict_report["changed_rows"]
    }
    embedded_changed = {
        str(row["canonical_bld_id"])
        for row in embedded_report["changed_rows"]
    }
    affected_cids = sorted(strict_changed | embedded_changed)

    affected = subset_embedded(embedded, set(affected_cids))

    write_json(args.strict_output, strict)
    write_json(args.embedded_output, embedded)
    write_json(args.affected_output, affected)
    write_json(args.affected_cids, {"affected_cids": affected_cids})

    report = {
        "status": "PASS",
        "mode": "artifact_patch_only",
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
