#!/usr/bin/env python3
"""C2.5 classify review-needed completeness gaps into rule-based candidates.

Read-only. This does not patch canonical JSON, source DBs, Neon, R2, or upload
code. It takes the C1/C2 manual review queue and separates:

- `safe_after_policy`: deterministic candidates that can be applied after the
  user approves the rule.
- `keep_review`: candidates that still require manual/semantic review.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_canonical_data_integrity import clean, load_source_meta, norm_country  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402


DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_REVIEW = ROOT / "data/reports/canonical_v2_backfill_candidates_review_needed.json"
DEFAULT_JSON = ROOT / "data/reports/canonical_v2_review_rule_candidates.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_review_rule_candidates.md"

CURRENT_YEAR = date.today().year


COUNTRY_ALIASES = {
    "uae": "united arab emirates",
    "u.a.e.": "united arab emirates",
    "usa": "united states",
    "us": "united states",
    "u.s.a.": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
}


def normalize_country(value: Any) -> str:
    text = norm_country(value)
    return COUNTRY_ALIASES.get(text, text)


def strip_location(value: str) -> str:
    text = value.replace("| View Map", "")
    text = re.sub(r"\s+", " ", text).strip(" ,;-")
    return text


def split_location_full(value: str) -> tuple[str | None, str | None, str]:
    """Parse only conservative City/Country patterns.

    Accepted:
    - `City - Country`
    - `City, Region, Country`
    - `City, Country`

    Rejected:
    - single-token country/city strings
    - hyphenated place names without ` - ` separator
    - strings with no plausible city component
    """
    text = strip_location(value)
    if not text:
        return None, None, "empty_location_full"

    if " - " in text:
        city, country = [part.strip() for part in text.split(" - ", 1)]
        return city or None, country or None, "city_dash_country"

    if "," in text:
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if len(parts) >= 2:
            city = parts[0]
            country = parts[-1]
            return city or None, country or None, "city_comma_country"

    return None, None, "single_location_string"


def row_index(input_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in iter_buildings(input_path):
        cid = clean(row.get("canonical_bld_id"))
        if cid:
            out[cid] = row
    return out


def known_countries() -> set[str]:
    source_meta, _stats = load_source_meta()
    countries = {
        normalize_country(meta.get("country"))
        for meta in source_meta.values()
        if normalize_country(meta.get("country"))
    }
    countries.update(
        {
            "albania",
            "china",
            "nigeria",
            "poland",
            "singapore",
            "spain",
            "ukraine",
            "united arab emirates",
        }
    )
    return countries


def classify_location_city(
    item: dict[str, Any],
    row: dict[str, Any],
    countries: set[str],
) -> tuple[str, dict[str, Any]]:
    locations = item.get("evidence", {}).get("location_full") or []
    if len(locations) != 1:
        return "keep_review", {**item, "decision_reason": "multiple_or_missing_location_full"}

    city, parsed_country, rule = split_location_full(str(locations[0]))
    canonical_country = normalize_country(row.get("location_country"))
    parsed_country_norm = normalize_country(parsed_country)
    city_norm_as_country = normalize_country(city)

    evidence = {
        "location_full": locations[0],
        "parse_rule": rule,
        "parsed_city": city,
        "parsed_country": parsed_country,
        "canonical_country": row.get("location_country"),
    }

    if not city or not parsed_country:
        return "keep_review", {**item, "decision_reason": rule, "evidence": evidence}
    if city_norm_as_country in countries:
        return "keep_review", {**item, "decision_reason": "parsed_city_is_known_country", "evidence": evidence}
    if canonical_country and parsed_country_norm and canonical_country != parsed_country_norm:
        return "keep_review", {**item, "decision_reason": "parsed_country_conflicts_with_canonical_country", "evidence": evidence}
    if parsed_country_norm not in countries and not canonical_country:
        return "keep_review", {**item, "decision_reason": "parsed_country_not_known", "evidence": evidence}

    return "safe_after_policy", {
        **item,
        "field": "location_city",
        "proposed": city,
        "rule": "location_full_city_country_parse_matches_country",
        "confidence": "policy_safe",
        "evidence": evidence,
    }


def classify_project_year(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    years = [int(year) for year in item.get("evidence", {}).get("year_candidates") or []]
    if len(years) != 1:
        return "keep_review", {**item, "decision_reason": "multiple_description_year_candidates"}

    year = years[0]
    if year > CURRENT_YEAR:
        return "keep_review", {**item, "decision_reason": "future_description_year_candidate"}
    if year < 1800:
        return "keep_review", {**item, "decision_reason": "year_before_supported_range"}

    return "safe_after_policy", {
        **item,
        "field": "project_year",
        "proposed": year,
        "rule": "single_past_or_current_description_year",
        "confidence": "policy_safe_not_structured_source",
    }


def classify(input_path: Path, review_path: Path) -> dict[str, Any]:
    review = json.load(review_path.open(encoding="utf-8"))
    rows = row_index(input_path)
    countries = known_countries()

    safe: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    safe_by_field: Counter[str] = Counter()
    keep_by_reason: Counter[str] = Counter()

    for item in review.get("items") or []:
        cid = clean(item.get("canonical_bld_id"))
        row = rows.get(cid or "")
        if not row:
            keep_item = {**item, "decision_reason": "canonical_row_missing_from_input"}
            keep.append(keep_item)
            keep_by_reason[keep_item["decision_reason"]] += 1
            continue

        field = item.get("field")
        if field == "location_city":
            decision, classified = classify_location_city(item, row, countries)
        elif field == "project_year":
            decision, classified = classify_project_year(item)
        else:
            decision, classified = "keep_review", {**item, "decision_reason": "unsupported_field"}

        if decision == "safe_after_policy":
            safe.append(classified)
            safe_by_field[str(classified["field"])] += 1
        else:
            keep.append(classified)
            keep_by_reason[str(classified.get("decision_reason"))] += 1

    return {
        "status": "PASS",
        "mode": "read-only",
        "input": str(input_path),
        "review_input": str(review_path),
        "total_review_items": len(review.get("items") or []),
        "safe_after_policy_count": len(safe),
        "keep_review_count": len(keep),
        "safe_by_field": dict(safe_by_field),
        "keep_review_by_reason": dict(keep_by_reason),
        "safe_after_policy": safe,
        "keep_review": keep,
        "writes": "reports only",
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def table(rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(v) for v in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def write_md(path: Path, payload: dict[str, Any]) -> None:
    safe_rows = [["field", "count"]]
    for field, count in sorted(payload["safe_by_field"].items(), key=lambda item: (-item[1], item[0])):
        safe_rows.append([field, count])

    keep_rows = [["reason", "count"]]
    for reason, count in sorted(payload["keep_review_by_reason"].items(), key=lambda item: (-item[1], item[0])):
        keep_rows.append([reason, count])

    sample_rows = [["cid", "field", "proposed", "rule"]]
    for item in payload["safe_after_policy"][:25]:
        sample_rows.append([
            item.get("canonical_bld_id"),
            item.get("field"),
            item.get("proposed"),
            item.get("rule"),
        ])

    text = f"""# canonical_v2 C2.5 review-rule candidates

Mode: read-only.

Input: `{payload["input"]}`

Review queue input: `{payload["review_input"]}`

Total review items: {payload["total_review_items"]}

Safe after explicit policy approval: {payload["safe_after_policy_count"]}

Keep manual review: {payload["keep_review_count"]}

## Safe-after-policy candidates

{table(safe_rows)}

## Keep-review reasons

{table(keep_rows)}

## Safe candidate samples

{table(sample_rows)}

## Proposed policy

`location_city` may be auto-filled only when `location_full` has a conservative
`City - Country` or `City, Country` parse and the parsed country agrees with
the existing canonical country or known source countries.

`project_year` may be auto-filled only when the description has exactly one
year candidate and that year is not in the future. This is less reliable than
structured source metadata, so it should be treated as policy-safe only after
user approval, not as C2 high-confidence.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify C2.5 review-rule candidates.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    payload = classify(args.input, args.review)
    write_json(args.json, payload)
    write_md(args.md, payload)

    print(json.dumps({
        "status": payload["status"],
        "total_review_items": payload["total_review_items"],
        "safe_after_policy_count": payload["safe_after_policy_count"],
        "keep_review_count": payload["keep_review_count"],
        "safe_by_field": payload["safe_by_field"],
        "top_keep_review_reasons": dict(sorted(payload["keep_review_by_reason"].items(), key=lambda item: (-item[1], item[0]))[:8]),
        "writes": payload["writes"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
