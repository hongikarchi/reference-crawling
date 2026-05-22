#!/usr/bin/env python3
"""Apply C8 web/LLM reviewed safe updates to canonical_v2 local artifacts.

Writes new artifact files only. Does not touch Neon/R2/upload.
"""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

ALLOWED_FIELDS = {"location_country", "location_city", "project_year"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def row_list(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("buildings", "items", "rows"):
            value = doc.get(key)
            if isinstance(value, list):
                return value
    raise SystemExit("unsupported canonical JSON shape")


def apply_updates(doc: Any, updates_by_cid: dict[str, dict[str, Any]]) -> tuple[Any, dict[str, int], list[str]]:
    out = deepcopy(doc)
    rows = row_list(out)
    seen: set[str] = set()
    counts: dict[str, int] = {field: 0 for field in sorted(ALLOWED_FIELDS)}
    counts.update({"skipped_same_value": 0, "skipped_not_empty_project_year": 0})

    for row in rows:
        cid = row.get("canonical_bld_id")
        if not cid or cid not in updates_by_cid:
            continue
        seen.add(cid)
        for field, value in updates_by_cid[cid].items():
            if field not in ALLOWED_FIELDS:
                raise SystemExit(f"unsupported field for {cid}: {field}")
            current = row.get(field)
            if current == value:
                counts["skipped_same_value"] += 1
                continue
            if field == "project_year" and current not in (None, ""):
                counts["skipped_not_empty_project_year"] += 1
                continue
            row[field] = value
            counts[field] += 1

    missing = sorted(set(updates_by_cid) - seen)
    return out, counts, missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict-input", default="data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c7.json")
    parser.add_argument("--embedded-input", default="data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c7.json")
    parser.add_argument("--review", default="data/reports/canonical_v2_c8_web_llm_review.json")
    parser.add_argument("--strict-output", default="data/canonical/country_conflict_refresh/canonical_buildings_strict.completeness_c8.json")
    parser.add_argument("--embedded-output", default="data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8.json")
    parser.add_argument("--affected-output", default="data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c8_affected.json")
    parser.add_argument("--affected-cids-output", default="data/canonical/country_conflict_refresh/completeness_c8_affected_cids.json")
    parser.add_argument("--report", default="data/reports/canonical_v2_completeness_c8_apply_report.json")
    args = parser.parse_args()

    review = load_json(Path(args.review))
    update_items = [item for item in review["items"] if item.get("verdict") == "apply" and item.get("field_updates")]
    updates_by_cid = {item["canonical_bld_id"]: item["field_updates"] for item in update_items}

    strict_doc = load_json(Path(args.strict_input))
    embedded_doc = load_json(Path(args.embedded_input))

    strict_out, strict_counts, strict_missing = apply_updates(strict_doc, updates_by_cid)
    embedded_out, embedded_counts, embedded_missing = apply_updates(embedded_doc, updates_by_cid)

    if strict_missing or embedded_missing:
        raise SystemExit(json.dumps({"status": "FAIL", "strict_missing": strict_missing, "embedded_missing": embedded_missing}, ensure_ascii=False, indent=2))

    affected_cids = sorted(updates_by_cid)
    embedded_rows = row_list(embedded_out)
    affected_rows = [row for row in embedded_rows if row.get("canonical_bld_id") in updates_by_cid]

    dump_json(Path(args.strict_output), strict_out)
    dump_json(Path(args.embedded_output), embedded_out)
    dump_json(Path(args.affected_output), affected_rows)
    dump_json(Path(args.affected_cids_output), affected_cids)

    report = {
        "status": "PASS",
        "review": args.review,
        "planned_update_cids": len(updates_by_cid),
        "affected_cid_count": len(affected_rows),
        "strict_field_updates": strict_counts,
        "embedded_field_updates": embedded_counts,
        "outputs": {
            "strict": args.strict_output,
            "embedded": args.embedded_output,
            "affected": args.affected_output,
            "affected_cids": args.affected_cids_output,
        },
        "writes": "new artifact files only; no Neon/R2/upload mutation",
    }
    dump_json(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
