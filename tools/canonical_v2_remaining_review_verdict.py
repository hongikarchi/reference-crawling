#!/usr/bin/env python3
"""Read-only C4 verdict for remaining C3 completeness candidates.

This report verifies the 88 candidates left after C3. It does not mutate
canonical artifacts, Neon, R2, source DBs, or upload code.
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

DEFAULT_REVIEW = ROOT / "data/reports/canonical_v2_backfill_candidates_review_needed.completeness_c3.json"
DEFAULT_LOCATION = ROOT / "data/reports/canonical_v2_llm_location_adjudication.json"
DEFAULT_JSON = ROOT / "data/reports/canonical_v2_remaining_review_verdict.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_remaining_review_verdict.md"


YEAR_APPLY: dict[str, dict[str, Any]] = {
    "bld_031996": {"project_year": 2025, "reason": "Expo Osaka 2025 pavilion; event year is the project year."},
    "bld_032313": {"project_year": 2029, "reason": "Source says relocation to the new headquarters is aimed for 2029."},
    "bld_033220": {"project_year": 2025, "reason": "Source metadata text says `Status: Under construction Year: 2025`."},
    "bld_034560": {"project_year": 2025, "reason": "Source says completion expected in 2025."},
    "bld_034566": {"project_year": 2026, "reason": "Source says ongoing, completion expected in 2026."},
    "bld_035042": {"project_year": 2030, "reason": "Source says `Year of Completion: 2030`."},
    "bld_036234": {"project_year": 2026, "reason": "Source says construction started in 2023 and is expected to be completed by 2026."},
    "bld_036484": {"project_year": 2022, "reason": "Source says the villa underwent full reconstruction in 2022."},
    "bld_037524": {"project_year": 2025, "reason": "Source says `Year: 2023-2025`; use range end as completion year candidate."},
    "bld_037719": {"project_year": 2030, "reason": "Source says `Completion Year: 2030`."},
    "bld_037983": {"project_year": 2023, "reason": "Source says `Year: 2022-2023`; use range end as completion year candidate."},
    "bld_038956": {"project_year": 2027, "reason": "Source says opening for classes in 2027."},
    "bld_039003": {"project_year": 2027, "reason": "Source says completion scheduled for 2027."},
    "bld_039222": {"project_year": 2027, "reason": "Source says under construction, completion 2027."},
    "bld_039576": {"project_year": 2025, "reason": "Source metadata line identifies the Queenstown house site entry as Singapore 2025."},
}

YEAR_KEEP_REASONS: dict[str, str] = {
    "bld_004967": "Years refer to Georges Durand's lifespan, not project completion.",
    "bld_007853": "Multiple years describe architect biography and Eni Village construction phases; one project year is not safely extractable.",
    "bld_007856": "Multiple years describe architect biography, social centre period, and later cultural program; no single safe project year.",
    "bld_030397": "2050 is an urbanization projection, not project year.",
    "bld_030465": "2024 is an award year and 2030 is Saudi Vision context; neither is project year.",
    "bld_031120": "2027/2034 are host-event/FIFA compliance context, not completion year.",
    "bld_031278": "2030 refers to World Cup host-city application context, not project completion.",
    "bld_031854": "2023 is work start and 2024 is rendering unveiling; completion year is not provided.",
    "bld_032307": "2030/2040/2050 are masterplan horizon targets, not completion year.",
    "bld_033073": "2030 refers to regional GDP/bay-area plan context, not project completion.",
    "bld_033079": "2009/2030 are regional history/projection and 2018 is competition; no safe completion year.",
    "bld_034537": "2019/2100 are climate data/projection and 2021 is competition context, not building completion.",
    "bld_034559": "Years are climate projection periods, not project completion.",
    "bld_034563": "Years describe historical silo operation/heritage listing, not current project completion.",
    "bld_034643": "2019 is competition and 2022 is commissioning strategy; completion year is not provided.",
    "bld_035226": "2024/2025 describe engagement and public presentation milestones, not completion.",
    "bld_035554": "2030 is Saudi Vision context, not project completion.",
    "bld_036680": "Years describe camp history and feasibility context, not completion of a new project.",
    "bld_037904": "2021/2022 are concept/planning permission milestones, not completion.",
    "bld_037911": "1909-1911 is historical renovation range; current project year is not safely extractable.",
    "bld_038138": "2016/2030 refer to Saudi Vision context, not project completion.",
    "bld_038551": "Years describe historical barracks use, not current student house project completion.",
    "bld_038572": "1901/2013 describe factory history, not reconstruction completion.",
    "bld_038718": "2051 is speculative future scenario, not project completion.",
    "bld_038824": "1979/1989 describe acquisition/adaptive reuse history; current project year remains ambiguous.",
    "bld_039059": "Years describe historic building and renovation decision context; completion year not safely extractable.",
    "bld_039219": "2015-2023 describes architect firm period, not project completion.",
    "bld_039295": "Historical library dates; 2011 is relocation/current site context, not necessarily project completion.",
    "bld_039394": "2030 is Saudi Vision context, not project completion.",
    "bld_039756": "2006/2026 describe festival history/anniversary, not a building completion year.",
}


def load_json(path: Path) -> Any:
    return json.load(path.open(encoding="utf-8"))


def table(rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(value) for value in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(value) for value in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def location_verdicts(review_items: list[dict[str, Any]], location_report: dict[str, Any]) -> list[dict[str, Any]]:
    decision_by_cid = {item["canonical_bld_id"]: item for item in location_report.get("decisions") or []}
    out: list[dict[str, Any]] = []
    for item in review_items:
        cid = str(item.get("canonical_bld_id") or "")
        decision = decision_by_cid.get(cid)
        if not decision:
            out.append({**item, "verdict": "manual_review", "reason": "No C2.6 location decision found."})
            continue
        kind = decision.get("location_kind")
        if kind == "country_only":
            verdict = "verified_keep_city_null"
            reason = "Location string is a country, so it must not be written to location_city."
        elif kind in {"locality_country", "airport_locality_inferred", "region_country", "non_city_descriptor_country", "national_park"}:
            verdict = "manual_review"
            reason = "Location is not a clean city value or needs external/source-specific confirmation."
        else:
            verdict = "manual_review"
            reason = "Unexpected residual location candidate after C3; keep for manual review."
        out.append(
            {
                **item,
                "verdict": verdict,
                "location_kind": kind,
                "proposed_city": decision.get("proposed_city"),
                "proposed_country": decision.get("proposed_country"),
                "confidence": decision.get("confidence"),
                "reason": reason,
            }
        )
    return out


def build_payload(review_path: Path, location_path: Path) -> dict[str, Any]:
    review = load_json(review_path)
    location_report = load_json(location_path)
    items = review.get("items") or []
    year_items = [item for item in items if item.get("field") == "project_year"]
    location_items = [item for item in items if item.get("field") == "location_city"]

    year_apply: list[dict[str, Any]] = []
    year_keep: list[dict[str, Any]] = []
    for item in year_items:
        cid = str(item.get("canonical_bld_id") or "")
        if cid in YEAR_APPLY:
            year_apply.append(
                {
                    **item,
                    "verdict": "policy_apply_candidate",
                    "proposed_project_year": YEAR_APPLY[cid]["project_year"],
                    "reason": YEAR_APPLY[cid]["reason"],
                }
            )
        else:
            year_keep.append(
                {
                    **item,
                    "verdict": "manual_review_or_keep_null",
                    "reason": YEAR_KEEP_REASONS.get(cid, "No safe project-year interpretation from remaining evidence."),
                }
            )

    locations = location_verdicts(location_items, location_report)
    counts = Counter()
    counts["year_policy_apply_candidates"] = len(year_apply)
    counts["year_keep_review"] = len(year_keep)
    for item in locations:
        counts[f"location_{item['verdict']}"] += 1

    return {
        "status": "PASS",
        "mode": "read-only semantic verification",
        "review_input": str(review_path),
        "location_input": str(location_path),
        "total_remaining_items": len(items),
        "counts": dict(counts),
        "year_policy_apply_candidates": year_apply,
        "year_keep_review": year_keep,
        "location_verdicts": locations,
        "writes": "reports only",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_md(path: Path, payload: dict[str, Any]) -> None:
    count_rows = [["metric", "count"]]
    for key, value in payload["counts"].items():
        count_rows.append([key, value])

    year_apply_rows = [["cid", "name", "year", "reason"]]
    for item in payload["year_policy_apply_candidates"]:
        year_apply_rows.append([item["canonical_bld_id"], item["name"], item["proposed_project_year"], item["reason"]])

    location_rows = [["verdict", "count"]]
    location_counts = Counter(item["verdict"] for item in payload["location_verdicts"])
    for key, value in location_counts.items():
        location_rows.append([key, value])

    text = f"""# canonical_v2 remaining candidate verification

Mode: read-only semantic verification.

Input: `{payload["review_input"]}`

Total remaining items: {payload["total_remaining_items"]}

## Summary

{table(count_rows)}

## Project-year policy apply candidates

These are still not applied. They are candidates for a future C4 apply step
after explicit approval.

{table(year_apply_rows)}

## Location verdicts

{table(location_rows)}

Full row-level verdicts are in:
`data/reports/canonical_v2_remaining_review_verdict.json`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify remaining C3 completeness candidates.")
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--location", type=Path, default=DEFAULT_LOCATION)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    payload = build_payload(args.review, args.location)
    write_json(args.json, payload)
    write_md(args.md, payload)
    print(json.dumps({
        "status": payload["status"],
        "total_remaining_items": payload["total_remaining_items"],
        "counts": payload["counts"],
        "writes": payload["writes"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
