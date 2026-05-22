#!/usr/bin/env python3
"""C5.1 read-only verdict for narrow local crawler gap candidates.

Consumes the C5 crawler gap audit and records explicit semantic verdicts for
the high-value local candidates:

- raw location strings that may contain a city/locality;
- text snippets with completion/opening/building year signals.

This script does not mutate canonical artifacts, source DBs, Neon, R2, or
upload paths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("data/reports/canonical_v2_crawler_gap_audit.json")
DEFAULT_JSON = Path("data/reports/canonical_v2_c5_local_candidate_verdict.json")
DEFAULT_MD = Path("data/reports/canonical_v2_c5_local_candidate_verdict.md")


VERDICTS: dict[tuple[str, str], dict[str, Any]] = {
    (
        "city_raw_location_candidate",
        "bld_030149",
    ): {
        "verdict": "keep_null",
        "field": "location_city",
        "proposed_value": None,
        "confidence": "high",
        "reason": (
            "Raw value `NEOM Community-1 Saudi Arabia` identifies a project "
            "community/local development area, not a stable city value."
        ),
    },
    (
        "city_raw_location_candidate",
        "bld_035966",
    ): {
        "verdict": "keep_null",
        "field": "location_city",
        "proposed_value": None,
        "confidence": "high",
        "reason": (
            "Raw value `private - The Netherlands` contains country/privacy "
            "metadata only and no city/locality."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_007853",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "high",
        "reason": (
            "Source text contains multiple component ranges for the village "
            "complex, including 1955-1962, 1958-1961, 1956-1957, and "
            "1956-1961. No single canonical project year is safe."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_031854",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "high",
        "reason": (
            "2023 indicates construction start and 2024 indicates rendering "
            "unveiling, not project completion."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_032307",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "high",
        "reason": (
            "2030/2040/2050 are future planning horizon years for Almere 2040, "
            "not a completed project year."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_034563",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "medium",
        "reason": (
            "Text says the silos operated 1940-1991 and were built in the "
            "1940s, but does not provide one exact completion year."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_037911",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "medium",
        "reason": (
            "Text describes a renovation between 1909 and 1911. A range is not "
            "safe enough to collapse into one canonical project year."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_038824",
    ): {
        "verdict": "apply",
        "field": "project_year",
        "proposed_value": 1989,
        "confidence": "high",
        "reason": (
            "Text says the building was adapted for reuse and opened its doors "
            "in 1989 as Anthology Film Archives, which is a direct opening/"
            "completion signal for the named project."
        ),
    },
    (
        "year_text_completion_signal_candidate",
        "bld_039059",
    ): {
        "verdict": "keep_null",
        "field": "project_year",
        "proposed_value": None,
        "confidence": "medium",
        "reason": (
            "Text includes original construction, redesign, and renovation "
            "intent years. For a renovation project, 2004 is an owner-action "
            "date, not a confirmed completion year."
        ),
    },
}


def candidate_context(candidate: dict[str, Any]) -> str:
    if candidate.get("raw_locations"):
        return "; ".join(str(v) for v in candidate["raw_locations"])
    contexts = candidate.get("contexts") or []
    parts = []
    for item in contexts:
        year = item.get("year")
        text = " ".join(str(item.get("context", "")).split())
        parts.append(f"{year}: {text}")
    return " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()

    audit = json.loads(args.input.read_text())
    examples = audit.get("examples", {})

    rows: list[dict[str, Any]] = []
    for metric in (
        "city_raw_location_candidate",
        "year_text_completion_signal_candidate",
    ):
        for candidate in examples.get(metric, []):
            cid = candidate["canonical_bld_id"]
            verdict = VERDICTS.get((metric, cid))
            if verdict is None:
                raise SystemExit(f"Missing verdict for {metric} {cid}")
            rows.append(
                {
                    "metric": metric,
                    "canonical_bld_id": cid,
                    "name": candidate.get("name"),
                    "source_refs": candidate.get("source_refs", {}),
                    "missing_fields": candidate.get("missing_fields", []),
                    "context": candidate_context(candidate),
                    **verdict,
                }
            )

    counts: dict[str, int] = {
        "total_candidates": len(rows),
        "apply": 0,
        "keep_null": 0,
        "project_year_apply": 0,
        "project_year_keep_null": 0,
        "location_city_apply": 0,
        "location_city_keep_null": 0,
    }
    for row in rows:
        verdict = row["verdict"]
        field = row["field"]
        counts[verdict] = counts.get(verdict, 0) + 1
        if field == "project_year":
            counts[f"project_year_{verdict}"] += 1
        elif field == "location_city":
            counts[f"location_city_{verdict}"] += 1

    payload = {
        "status": "PASS",
        "mode": "read_only_semantic_verdict",
        "input": str(args.input),
        "counts": counts,
        "rows": rows,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    lines = [
        "# canonical_v2 C5.1 local candidate verdict",
        "",
        "Mode: read-only semantic verdict over C5 high-value local candidates.",
        "",
        "## Counts",
        "",
        "| metric | count |",
        "| --- | ---: |",
    ]
    for key in sorted(counts):
        lines.append(f"| {key} | {counts[key]} |")

    lines.extend(
        [
            "",
            "## Verdicts",
            "",
            "| cid | name | field | verdict | value | confidence | reason |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in rows:
        value = "" if row["proposed_value"] is None else str(row["proposed_value"])
        reason = row["reason"].replace("|", "/")
        lines.append(
            "| {cid} | {name} | {field} | {verdict} | {value} | "
            "{confidence} | {reason} |".format(
                cid=row["canonical_bld_id"],
                name=row["name"],
                field=row["field"],
                verdict=row["verdict"],
                value=value,
                confidence=row["confidence"],
                reason=reason,
            )
        )

    lines.extend(
        [
            "",
            "## Apply boundary",
            "",
            "This report does not mutate canonical artifacts or Neon. If approved, "
            "only `bld_038824.project_year = 1989` should be considered for "
            "a narrow C5.2 apply.",
            "",
        ]
    )
    args.output_md.write_text("\n".join(lines))

    print(
        json.dumps(
            {
                "status": "PASS",
                "counts": counts,
                "writes": [str(args.output_json), str(args.output_md)],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
