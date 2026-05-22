#!/usr/bin/env python3
"""Apply approved C4 project-year backfills to C3 canonical artifacts.

Inputs are read-only remaining-review verdicts. Outputs are new
`.completeness_c4` artifacts; C3 and resume10 artifacts are never overwritten.
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

DEFAULT_STRICT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c3.json"
DEFAULT_EMBEDDED_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c3.json"
DEFAULT_STRICT_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c4.json"
DEFAULT_EMBEDDED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json"
DEFAULT_AFFECTED_OUTPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4_affected.json"
DEFAULT_AFFECTED_CIDS = ROOT / "data/canonical/country_conflict_refresh/completeness_c4_affected_cids.json"
DEFAULT_VERDICT = ROOT / "data/reports/canonical_v2_remaining_review_verdict.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_completeness_c4_apply_report.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def build_updates(verdict_path: Path) -> dict[str, int]:
    data = load_json(verdict_path)
    updates: dict[str, int] = {}
    for item in data.get("year_policy_apply_candidates") or []:
        cid = str(item.get("canonical_bld_id") or "")
        proposed = item.get("proposed_project_year")
        if cid and isinstance(proposed, int):
            updates[cid] = proposed
    return updates


def apply_updates(data: dict[str, Any], updates: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    counts: Counter[str] = Counter()
    changed_rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in data.get("buildings") or []:
        cid = str(row.get("canonical_bld_id") or "")
        if cid not in updates:
            continue
        seen.add(cid)
        current = row.get("project_year")
        proposed = updates[cid]
        if current in (None, "", []):
            row["project_year"] = proposed
            counts["project_year"] += 1
            changed_rows.append(
                {
                    "canonical_bld_id": cid,
                    "name": row.get("name"),
                    "source_refs": row.get("source_refs") or {},
                    "before": {"project_year": current},
                    "after": {"project_year": proposed},
                }
            )
        else:
            counts["project_year_skipped_not_empty"] += 1

    return data, {
        "field_updates": dict(counts),
        "changed_row_count": len(changed_rows),
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
    parser = argparse.ArgumentParser(description="Apply approved C4 project-year updates.")
    parser.add_argument("--strict-input", type=Path, default=DEFAULT_STRICT_INPUT)
    parser.add_argument("--embedded-input", type=Path, default=DEFAULT_EMBEDDED_INPUT)
    parser.add_argument("--strict-output", type=Path, default=DEFAULT_STRICT_OUTPUT)
    parser.add_argument("--embedded-output", type=Path, default=DEFAULT_EMBEDDED_OUTPUT)
    parser.add_argument("--affected-output", type=Path, default=DEFAULT_AFFECTED_OUTPUT)
    parser.add_argument("--affected-cids", type=Path, default=DEFAULT_AFFECTED_CIDS)
    parser.add_argument("--verdict", type=Path, default=DEFAULT_VERDICT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    updates = build_updates(args.verdict)
    strict, strict_report = apply_updates(load_json(args.strict_input), updates)
    embedded, embedded_report = apply_updates(load_json(args.embedded_input), updates)
    affected_cids = sorted(
        {
            row["canonical_bld_id"]
            for row in embedded_report["changed_rows"]
        }
    )

    write_json(args.strict_output, strict)
    write_json(args.embedded_output, embedded)
    write_json(args.affected_output, subset_embedded(embedded, set(affected_cids)))
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
