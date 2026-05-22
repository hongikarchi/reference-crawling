#!/usr/bin/env python3
"""C5 read-only audit for crawler/parser gaps behind canonical missing fields.

This asks: for rows still missing country/city/year after C4, do local source
DB rows contain raw fields that suggest the crawler/parser missed extractable
metadata?

No network, no source DB writes, no canonical/Neon/R2/upload mutation.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_canonical_data_integrity import clean, parse_year, years_in_text  # noqa: E402
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.completeness_c4.json"
DEFAULT_JSON = ROOT / "data/reports/canonical_v2_crawler_gap_audit.json"
DEFAULT_MD = ROOT / "data/reports/canonical_v2_crawler_gap_audit.md"

DBS = {
    "divisare": ROOT / "data/crawl/divisare.db",
    "architizer": ROOT / "data/crawl/architizer.db",
    "archello": ROOT / "data/crawl/archello.db",
    "metalocus": ROOT / "data/crawl/metalocus.db",
}


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def load_source_rows() -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}

    conn = sqlite3.connect(DBS["divisare"])
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM divisare_projects"):
            item = dict(row)
            item["_source"] = "divisare"
            item["_source_id"] = str(row["id"])
            item["_raw_location"] = None
            item["_structured_country"] = clean(row["location_country"])
            item["_structured_city"] = clean(row["location_city"])
            item["_structured_year"] = parse_year(row["project_year"])
            item["_text"] = " ".join(str(v or "") for v in [row["name"], row["abstract"], row["description"], row["credits"]])
            out[("divisare", str(row["id"]))] = item
    finally:
        conn.close()

    conn = sqlite3.connect(DBS["architizer"])
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM architizer_projects"):
            item = dict(row)
            item["_source"] = "architizer"
            item["_source_id"] = str(row["id"])
            item["_raw_location"] = clean(row["location_full"])
            item["_structured_country"] = clean(row["location_country"])
            item["_structured_city"] = clean(row["location_city"])
            item["_structured_year"] = parse_year(row["completion_year"])
            item["_text"] = " ".join(str(v or "") for v in [row["name"], row["description"], row["description_short"], row["location_full"]])
            out[("architizer", str(row["id"]))] = item
    finally:
        conn.close()

    conn = sqlite3.connect(DBS["archello"])
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM archello_projects"):
            item = dict(row)
            item["_source"] = "archello"
            item["_source_id"] = str(row["id"])
            item["_raw_location"] = clean(row["location_full"])
            item["_structured_country"] = clean(row["location_country"])
            item["_structured_city"] = clean(row["location_city"])
            item["_structured_year"] = parse_year(row["project_year"])
            item["_text"] = " ".join(str(v or "") for v in [row["name"], row["description"], row["location_full"]])
            out[("archello", str(row["id"]))] = item
    finally:
        conn.close()

    conn = sqlite3.connect(DBS["metalocus"])
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM buildings"):
            item = dict(row)
            item["_source"] = "metalocus"
            item["_source_id"] = str(row["id"])
            item["_raw_location"] = clean(row["location"])
            item["_structured_country"] = clean(row["country"])
            item["_structured_city"] = clean(row["city"])
            item["_structured_year"] = parse_year(row["year"])
            item["_text"] = " ".join(str(v or "") for v in [row["title"], row["description"], row["location"], row["raw_metadata"]])
            out[("metalocus", str(row["id"]))] = item
    finally:
        conn.close()

    return out


def source_refs(row: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for source, ids in (row.get("source_refs") or {}).items():
        for sid in ids or []:
            out.append((str(source), str(sid)))
    return out


def add_example(examples: dict[str, list[dict[str, Any]]], key: str, item: dict[str, Any]) -> None:
    bucket = examples.setdefault(key, [])
    if len(bucket) < 25:
        bucket.append(item)


def likely_raw_location_has_country(raw: str) -> bool:
    text = raw.replace("| View Map", "").strip()
    if not text:
        return False
    return bool("," in text or " - " in text or re.search(r"\b(United|Republic|Kingdom|Emirates|Arabia|Netherlands|China|Spain|Japan|Singapore|Kuwait|Qatar|Vietnam|Latvia|Israel|Italy|Poland|Nigeria|Ukraine|Albania|Turkey|Switzerland|Maldives|Iceland)\b", text, re.I))


def likely_raw_location_has_city(raw: str) -> bool:
    text = raw.replace("| View Map", "").strip()
    if not text:
        return False
    if "," in text or " - " in text:
        return True
    return bool(re.search(r"\b(Riyadh|Dubai|Abu Dhabi|Tokyo|Hong Kong|Singapore|Tabriz|Shunde|AlUla|Prishtin[eë]|NEOM)\b", text, re.I))


def year_contexts(text: str, years: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for year in years:
        match = re.search(r".{0,90}\b" + str(year) + r"\b.{0,90}", text, flags=re.S)
        if match:
            ctx = " ".join(match.group(0).split())
            signal = bool(
                re.search(
                    r"\b(completion|completed|opening|opened|year|status|under construction|construction|built|renovation|reconstruction)\b",
                    ctx,
                    flags=re.I,
                )
            )
            out.append({"year": year, "context": ctx, "completion_signal": signal})
    return out


def audit(input_path: Path) -> dict[str, Any]:
    sources = load_source_rows()
    counts: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = {}

    for row in iter_buildings(input_path):
        cid = str(row.get("canonical_bld_id") or "")
        missing_fields = [
            field
            for field in ("location_country", "location_city", "project_year")
            if row.get(field) in (None, "", [])
        ]
        if not missing_fields:
            continue

        members = [(source, sid, sources.get((source, sid))) for source, sid in source_refs(row)]
        members = [(s, sid, meta) for s, sid, meta in members if meta]
        if not members:
            counts["missing_rows_without_local_source_meta"] += 1
            continue

        base = {
            "canonical_bld_id": cid,
            "name": row.get("name"),
            "missing_fields": missing_fields,
            "source_refs": row.get("source_refs") or {},
        }

        if "location_country" in missing_fields:
            raw_locations = [meta["_raw_location"] for _s, _sid, meta in members if meta.get("_raw_location")]
            structured = [meta["_structured_country"] for _s, _sid, meta in members if meta.get("_structured_country")]
            if structured:
                counts["country_structured_source_available"] += 1
            elif any(likely_raw_location_has_country(str(raw)) for raw in raw_locations):
                counts["country_raw_location_candidate"] += 1
                for s, _sid, meta in members:
                    if meta.get("_raw_location"):
                        by_source[s]["country_raw_location_candidate"] += 1
                add_example(examples, "country_raw_location_candidate", {**base, "raw_locations": raw_locations[:6]})
            else:
                counts["country_no_local_candidate"] += 1

        if "location_city" in missing_fields:
            raw_locations = [meta["_raw_location"] for _s, _sid, meta in members if meta.get("_raw_location")]
            structured = [meta["_structured_city"] for _s, _sid, meta in members if meta.get("_structured_city")]
            if structured:
                counts["city_structured_source_available"] += 1
            elif any(likely_raw_location_has_city(str(raw)) for raw in raw_locations):
                counts["city_raw_location_candidate"] += 1
                for s, _sid, meta in members:
                    if meta.get("_raw_location"):
                        by_source[s]["city_raw_location_candidate"] += 1
                add_example(examples, "city_raw_location_candidate", {**base, "raw_locations": raw_locations[:6]})
            else:
                counts["city_no_local_candidate"] += 1

        if "project_year" in missing_fields:
            structured_years = [meta["_structured_year"] for _s, _sid, meta in members if meta.get("_structured_year")]
            text_years = sorted({year for _s, _sid, meta in members for year in years_in_text(meta.get("_text"))})
            contexts = [ctx for _s, _sid, meta in members for ctx in year_contexts(meta.get("_text") or "", text_years)]
            if structured_years:
                counts["year_structured_source_available"] += 1
            elif any(ctx["completion_signal"] for ctx in contexts):
                counts["year_text_completion_signal_candidate"] += 1
                for s, _sid, meta in members:
                    if years_in_text(meta.get("_text")):
                        by_source[s]["year_text_completion_signal_candidate"] += 1
                add_example(examples, "year_text_completion_signal_candidate", {**base, "contexts": contexts[:8]})
            elif text_years:
                counts["year_text_noncompletion_candidate"] += 1
                add_example(examples, "year_text_noncompletion_candidate", {**base, "years": text_years[:12]})
            else:
                counts["year_no_local_candidate"] += 1

    return {
        "status": "PASS",
        "mode": "read-only local crawler/parser gap audit",
        "input": str(input_path),
        "counts": dict(counts),
        "by_source": {source: dict(counter) for source, counter in sorted(by_source.items())},
        "examples": examples,
        "interpretation": {
            "structured_source_available": "Would indicate canonical assembly missed a structured source DB value.",
            "raw_location_candidate": "Suggests crawler stored raw location but parser did not split it into structured fields.",
            "year_text_completion_signal_candidate": "Suggests source text may contain completion/opening/year evidence, but semantic review is required before applying.",
            "no_local_candidate": "Likely needs source re-crawl, external web search, or should remain null.",
        },
        "writes": "reports only",
    }


def table(rows: list[list[Any]]) -> str:
    header = "| " + " | ".join(str(v) for v in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_md(path: Path, payload: dict[str, Any]) -> None:
    count_rows = [["metric", "count"]]
    for key, value in sorted(payload["counts"].items()):
        count_rows.append([key, value])
    source_rows = [["source", "metric", "count"]]
    for source, metrics in payload["by_source"].items():
        for key, value in sorted(metrics.items()):
            source_rows.append([source, key, value])
    text = f"""# canonical_v2 C5 crawler gap audit

Mode: read-only local source DB audit.

Input: `{payload["input"]}`

## Counts

{table(count_rows)}

## Source breakdown

{table(source_rows)}

## Interpretation

- `*_structured_source_available` would indicate canonical assembly missed a
  structured source DB field.
- `*_raw_location_candidate` suggests crawler stored raw location text but the
  parser did not split it into structured fields.
- `year_text_completion_signal_candidate` suggests source text may contain
  completion/opening/year evidence; semantic review is required before applying.
- `*_no_local_candidate` likely needs source re-crawl, external web search, or
  should remain null.

Full examples are in:
`data/reports/canonical_v2_crawler_gap_audit.json`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit crawler/parser gaps behind canonical missing fields.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    payload = audit(args.input)
    write_json(args.json, payload)
    write_md(args.md, payload)
    print(json.dumps({"status": payload["status"], "counts": payload["counts"], "writes": payload["writes"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
