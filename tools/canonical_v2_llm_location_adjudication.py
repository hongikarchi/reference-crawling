#!/usr/bin/env python3
"""C2.6 semantic location adjudication report for 81 location_full gaps.

This is a read-only codex semantic classification artifact. It does not call an
external service at runtime and does not mutate canonical JSON, source DBs,
Neon, R2, or upload code.
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


DEFAULT_REVIEW = ROOT / "data/reports/canonical_v2_backfill_candidates_review_needed.json"
DEFAULT_JSON = ROOT / "data/reports/canonical_v2_llm_location_adjudication.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_llm_location_adjudication.md"


DECISIONS: dict[str, dict[str, Any]] = {
    "bld_030000": {"city": "Dubai", "country": "United Arab Emirates", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_030116": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_030149": {"city": "NEOM Community-1", "country": "Saudi Arabia", "kind": "locality_country", "confidence": 0.86, "apply": False},
    "bld_030323": {"city": None, "country": "Ukraine", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_030343": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_030520": {"city": None, "country": "Nigeria", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_031144": {"city": None, "country": "Albania", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_031180": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_031386": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_031431": {"city": "Prishtinë", "country": "Kosovo", "kind": "city_only_inferred_country", "confidence": 0.94, "apply": True},
    "bld_031631": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_031991": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_032168": {"city": None, "country": "Poland", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032197": {"city": None, "country": "China", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032810": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032811": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032812": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032813": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032814": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032815": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032816": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032818": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032819": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032820": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032821": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032822": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032823": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_032857": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_033009": {"city": None, "country": "China", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033033": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_033035": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_033092": {"city": "Riyadh", "country": "Saudi Arabia", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_033113": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_033213": {"city": None, "country": "China", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033216": {"city": None, "country": "China", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033218": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_033284": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_033427": {"city": None, "country": "Italy", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033576": {"city": None, "country": "Ukraine", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033745": {"city": "Abu Dhabi", "country": "United Arab Emirates", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_033883": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_033884": {"city": None, "country": "Spain", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_034273": {"city": None, "country": "Antigua and Barbuda", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_034283": {"city": None, "country": "Japan", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_034384": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_034561": {"city": "Tabriz", "country": "Iran", "kind": "city_only_inferred_country", "confidence": 0.96, "apply": True},
    "bld_034975": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_034983": {"city": "Tehran", "country": "Iran", "kind": "airport_locality_inferred", "confidence": 0.82, "apply": False},
    "bld_034990": {"city": "Riyadh", "country": "Saudi Arabia", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_035110": {"city": None, "country": "Iceland", "kind": "region_country", "confidence": 0.92, "apply": False},
    "bld_035203": {"city": "Shunde", "country": "China", "kind": "city_only_inferred_country", "confidence": 0.94, "apply": True},
    "bld_035529": {"city": None, "country": "Japan", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_035580": {"city": None, "country": "Netherlands", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_035915": {"city": "Riyadh", "country": "Saudi Arabia", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_035957": {"city": None, "country": "Switzerland", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_035966": {"city": None, "country": "Netherlands", "kind": "non_city_descriptor_country", "confidence": 0.92, "apply": False},
    "bld_035971": {"city": "Dubai", "country": "United Arab Emirates", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_036564": {"city": None, "country": "Turkey", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_036989": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_036992": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_037323": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_037408": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_037409": {"city": "Singapore", "country": "Singapore", "kind": "city_state", "confidence": 0.99, "apply": True},
    "bld_037418": {"city": None, "country": "Qatar", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_037851": {"city": None, "country": "Maldives", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_037987": {"city": None, "country": "Kuwait", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_037988": {"city": None, "country": "Kuwait", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_038101": {"city": "Tokyo", "country": "Japan", "kind": "city_only_inferred_country", "confidence": 0.98, "apply": True},
    "bld_038291": {"city": None, "country": "Israel", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_038316": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038317": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038321": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038323": {"city": None, "country": "Vietnam", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_038327": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038328": {"city": "Riyadh", "country": "Saudi Arabia", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_038329": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038332": {"city": "Hong Kong", "country": "Hong Kong", "kind": "city_region", "confidence": 0.98, "apply": True},
    "bld_038643": {"city": None, "country": "Latvia", "kind": "country_only", "confidence": 0.99, "apply": False},
    "bld_038869": {"city": "Dubai", "country": "United Arab Emirates", "kind": "city_country", "confidence": 0.99, "apply": True},
    "bld_039152": {"city": None, "country": "Iceland", "kind": "national_park", "confidence": 0.88, "apply": False},
    "bld_039289": {"city": "AlUla", "country": "Saudi Arabia", "kind": "city_country", "confidence": 0.99, "apply": True},
}


def classify(review_path: Path) -> dict[str, Any]:
    review = json.load(review_path.open(encoding="utf-8"))
    location_items = [item for item in review.get("items") or [] if item.get("field") == "location_city"]

    decisions: list[dict[str, Any]] = []
    missing_decisions: list[str] = []
    by_kind: Counter[str] = Counter()
    apply_count = 0
    country_only_count = 0

    for item in location_items:
        cid = str(item.get("canonical_bld_id") or "")
        decision = DECISIONS.get(cid)
        if not decision:
            missing_decisions.append(cid)
            continue
        should_apply = bool(decision["apply"] and decision.get("city") and decision.get("confidence", 0) >= 0.9)
        if should_apply:
            apply_count += 1
        if decision["kind"] == "country_only":
            country_only_count += 1
        by_kind[str(decision["kind"])] += 1
        decisions.append(
            {
                **item,
                "location_full": (item.get("evidence") or {}).get("location_full", [None])[0],
                "location_kind": decision["kind"],
                "proposed_city": decision.get("city"),
                "proposed_country": decision.get("country"),
                "confidence": decision["confidence"],
                "should_apply_city": should_apply,
                "should_apply_country": bool(decision.get("country") and decision.get("confidence", 0) >= 0.9),
                "reason": reason_for(decision),
            }
        )

    status = "PASS" if not missing_decisions and len(decisions) == len(location_items) else "FAIL"
    return {
        "status": status,
        "mode": "read-only codex semantic classification",
        "review_input": str(review_path),
        "total_location_items": len(location_items),
        "classified_items": len(decisions),
        "missing_decisions": missing_decisions,
        "should_apply_city_count": apply_count,
        "country_only_count": country_only_count,
        "by_kind": dict(by_kind),
        "n10_smoke": decisions[:10],
        "decisions": decisions,
        "writes": "reports only",
    }


def reason_for(decision: dict[str, Any]) -> str:
    kind = decision["kind"]
    if kind == "country_only":
        return "The location string is a country name, so it must not be written into location_city."
    if kind == "city_state":
        return "The location string names a city-state; using it as both city and country is appropriate."
    if kind in {"city_country", "city_region"}:
        return "The location string identifies a city/place and country or region with high confidence."
    if kind == "city_only_inferred_country":
        return "The string names a well-known city; country is inferred from geographic knowledge."
    return "The string is not a clean city value, or confidence is below the apply threshold."


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def md_table(rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(v) for v in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def write_md(path: Path, payload: dict[str, Any]) -> None:
    kind_rows = [["kind", "count"]]
    for kind, count in sorted(payload["by_kind"].items(), key=lambda item: (-item[1], item[0])):
        kind_rows.append([kind, count])

    apply_rows = [["cid", "name", "location_full", "city", "country", "confidence"]]
    for item in payload["decisions"]:
        if item["should_apply_city"]:
            apply_rows.append(
                [
                    item["canonical_bld_id"],
                    item["name"],
                    item["location_full"],
                    item["proposed_city"],
                    item["proposed_country"],
                    item["confidence"],
                ]
            )

    country_rows = [["cid", "name", "location_full", "country"]]
    for item in payload["decisions"]:
        if item["location_kind"] == "country_only":
            country_rows.append([item["canonical_bld_id"], item["name"], item["location_full"], item["proposed_country"]])

    text = f"""# canonical_v2 C2.6 LLM location adjudication

Mode: read-only Codex semantic classification.

Input: `{payload["review_input"]}`

Total location items: {payload["total_location_items"]}

Classified items: {payload["classified_items"]}

Apply-city candidates: {payload["should_apply_city_count"]}

Country-only items not to write into city: {payload["country_only_count"]}

## Classification kinds

{md_table(kind_rows)}

## Apply-city candidates

{md_table(apply_rows)}

## Country-only examples

{md_table(country_rows[:26])}

Full decisions are in:
`data/reports/canonical_v2_llm_location_adjudication.json`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write C2.6 semantic location adjudication reports.")
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    payload = classify(args.review)
    write_json(args.json, payload)
    write_md(args.md, payload)
    print(json.dumps({
        "status": payload["status"],
        "total_location_items": payload["total_location_items"],
        "classified_items": payload["classified_items"],
        "should_apply_city_count": payload["should_apply_city_count"],
        "country_only_count": payload["country_only_count"],
        "by_kind": payload["by_kind"],
        "writes": payload["writes"],
    }, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
