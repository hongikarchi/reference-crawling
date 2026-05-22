#!/usr/bin/env python3
"""C1/C2 read-only completeness inventory and backfill candidate builder.

The script does not mutate canonical artifacts, source DBs, Neon, R2, or
upload code. It compares the resume10 embedded canonical artifact against the
source crawl DBs and the canonical architect registry, then writes:

- gap inventory JSON/Markdown
- high-confidence deterministic backfill candidates
- review-needed candidates for ambiguous gaps
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_canonical_data_integrity import (
    clean,
    dedupe,
    load_arch_index,
    load_source_meta,
    norm,
    norm_country,
    parse_year,
    years_in_text,
)
from tools.canonical_v2_upload_validator import iter_buildings


DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_ARCHITECTS = ROOT / "data/canonical/architects_canonical.json"
DEFAULT_INVENTORY_JSON = ROOT / "data/reports/canonical_v2_gap_inventory.json"
DEFAULT_INVENTORY_MD = ROOT / "data/reports/canonical_v2_gap_inventory.md"
DEFAULT_HIGH_CONF_JSON = ROOT / "data/reports/canonical_v2_backfill_candidates_high_confidence.json"
DEFAULT_REVIEW_JSON = ROOT / "data/reports/canonical_v2_backfill_candidates_review_needed.json"
DEFAULT_REVIEW_MD = ROOT / "data/reports/canonical_v2_manual_review_queue.md"

SOURCE_ORDER = {"divisare": 0, "architizer": 1, "archello": 2, "metalocus": 3}
EXAMPLE_LIMIT = 20

STRUCTURED_FIELDS: dict[str, tuple[str, Callable[[Any], str]]] = {
    "location_country": ("country", norm_country),
    "location_city": ("city", norm),
    "project_year": ("year", lambda value: str(parse_year(value) or "")),
}

ENRICHMENT_FIELDS = (
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "material_visual",
    "visual_description",
    "image_derived",
)


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def add_example(examples: dict[str, list[dict[str, Any]]], key: str, item: dict[str, Any]) -> None:
    bucket = examples.setdefault(key, [])
    if len(bucket) < EXAMPLE_LIMIT:
        bucket.append(item)


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "source_refs": row.get("source_refs") or {},
    }


def source_ref_keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for source, ids in (row.get("source_refs") or {}).items():
        for sid in ids or []:
            keys.append((str(source), str(sid)))
    return keys


def source_members(
    row: dict[str, Any],
    source_meta: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    members: list[dict[str, Any]] = []
    missing: list[tuple[str, str]] = []
    for key in source_ref_keys(row):
        meta = source_meta.get(key)
        if meta:
            members.append(meta)
        else:
            missing.append(key)
    members.sort(key=lambda meta: (SOURCE_ORDER.get(str(meta.get("source")), 99), str(meta.get("source_id"))))
    return members, missing


def value_groups(
    members: list[dict[str, Any]],
    meta_field: str,
    normalizer: Callable[[Any], str],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for meta in members:
        value = meta.get(meta_field)
        normalized = normalizer(value)
        if normalized:
            groups[normalized].append(
                {
                    "source": meta.get("source"),
                    "source_id": meta.get("source_id"),
                    "value": value,
                }
            )
    return dict(groups)


def proposed_value(field: str, evidence: list[dict[str, Any]]) -> Any:
    value = evidence[0]["value"]
    if field == "project_year":
        return parse_year(value)
    return value


def high_candidate(
    row: dict[str, Any],
    *,
    field: str,
    proposed: Any,
    rule: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **compact_row(row),
        "field": field,
        "current": row.get(field),
        "proposed": proposed,
        "rule": rule,
        "evidence": evidence[:8],
    }


def review_item(
    row: dict[str, Any],
    *,
    field: str,
    reason: str,
    evidence: Any,
) -> dict[str, Any]:
    return {
        **compact_row(row),
        "field": field,
        "reason": reason,
        "evidence": evidence,
    }


def load_arch_names(path: Path) -> dict[str, list[str]]:
    data = json.load(path.open(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for cluster in data.get("clusters") or []:
        aid = clean(cluster.get("canonical_arch_id"))
        if not aid:
            continue
        names = [str(name).strip() for name in cluster.get("names") or [] if str(name).strip()]
        if names:
            out[aid] = dedupe(names)
    return out


def source_url_candidates(row: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, list[str]]:
    current = row.get("source_urls") or {}
    by_source: dict[str, list[str]] = defaultdict(list)
    for meta in members:
        source = str(meta.get("source") or "")
        url = clean(meta.get("source_url"))
        if source and url:
            by_source[source].append(url)

    out: dict[str, list[str]] = {}
    for source, urls in by_source.items():
        current_urls = current.get(source) or []
        if isinstance(current_urls, str):
            current_urls = [current_urls]
        missing_urls = [url for url in dedupe(urls) if url not in current_urls]
        if missing_urls:
            out[source] = dedupe([str(url) for url in current_urls if url] + missing_urls)
    return out


def image_source_summary(members: list[dict[str, Any]]) -> dict[str, Any]:
    images = dedupe([url for meta in members for url in meta.get("images") or [] if url])
    covers = dedupe([meta.get("cover") for meta in members if meta.get("cover")])
    return {
        "source_image_count": len(images),
        "source_cover_count": len(covers),
        "sample_images": images[:5],
        "sample_covers": covers[:5],
    }


def year_text_candidates(members: list[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            year
            for meta in members
            for year in years_in_text(meta.get("description"))
            if 1800 <= int(year) <= 2100
        }
    )


def analyze(
    *,
    input_path: Path,
    architects_path: Path,
    limit: int | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source_meta, source_stats = load_source_meta()
    arch_source_to_id, canonical_arch_ids = load_arch_index(architects_path)
    arch_names = load_arch_names(architects_path)

    fields: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = {}
    high_conf: list[dict[str, Any]] = []
    review_needed: list[dict[str, Any]] = []
    review_reasons: Counter[str] = Counter()
    high_by_field: Counter[str] = Counter()
    review_by_field: Counter[str] = Counter()
    source_ref_missing: Counter[str] = Counter()
    total_rows = 0

    for row in iter_buildings(input_path, limit=limit):
        total_rows += 1
        members, missing_refs = source_members(row, source_meta)
        if missing_refs:
            source_ref_missing["rows_with_missing_source_refs"] += 1
            for source, _sid in missing_refs:
                source_ref_missing[f"missing_{source}"] += 1
            add_example(
                examples,
                "source_ref_missing_in_source_db",
                {**compact_row(row), "missing_refs": [f"{s}:{sid}" for s, sid in missing_refs[:8]]},
            )

        for field, (meta_field, normalizer) in STRUCTURED_FIELDS.items():
            if not is_missing(row.get(field)):
                fields[field]["complete"] += 1
                continue

            fields[field]["missing"] += 1
            groups = value_groups(members, meta_field, normalizer)
            if len(groups) == 1:
                evidence = next(iter(groups.values()))
                fields[field]["source_available_single"] += 1
                high_conf.append(
                    high_candidate(
                        row,
                        field=field,
                        proposed=proposed_value(field, evidence),
                        rule="single_structured_source_value_no_conflict",
                        evidence=evidence,
                    )
                )
                high_by_field[field] += 1
            elif len(groups) > 1:
                fields[field]["conflicting_sources"] += 1
                reason = "conflicting_structured_source_values"
                review_needed.append(
                    review_item(
                        row,
                        field=field,
                        reason=reason,
                        evidence={
                            normalized: values[:6]
                            for normalized, values in sorted(groups.items())
                        },
                    )
                )
                review_reasons[reason] += 1
                review_by_field[field] += 1
                add_example(
                    examples,
                    f"{field}_conflicting_sources",
                    {**compact_row(row), "source_values": groups},
                )
            else:
                location_full = dedupe([meta.get("location_full") for meta in members if meta.get("location_full")])
                if field in {"location_country", "location_city"} and location_full:
                    fields[field]["text_extractable"] += 1
                    reason = "location_full_present_but_structured_field_missing"
                    review_needed.append(
                        review_item(row, field=field, reason=reason, evidence={"location_full": location_full[:8]})
                    )
                    review_reasons[reason] += 1
                    review_by_field[field] += 1
                elif field == "project_year" and year_text_candidates(members):
                    fields[field]["text_extractable"] += 1
                    years = year_text_candidates(members)
                    reason = "description_year_candidate_requires_review"
                    review_needed.append(
                        review_item(row, field=field, reason=reason, evidence={"year_candidates": years[:12]})
                    )
                    review_reasons[reason] += 1
                    review_by_field[field] += 1
                else:
                    fields[field]["source_missing"] += 1

        arch_ids = [str(v) for v in row.get("architect_canonical_ids") or [] if str(v)]
        arch_names_current = [str(v) for v in row.get("architect_names") or [] if str(v)]
        if arch_ids:
            fields["architect_canonical_ids"]["complete"] += 1
            bad_ids = [aid for aid in arch_ids if aid not in canonical_arch_ids]
            if bad_ids:
                fields["architect_canonical_ids"]["invalid_registry_ref"] += 1
                reason = "architect_canonical_id_not_in_registry"
                review_needed.append(
                    review_item(row, field="architect_canonical_ids", reason=reason, evidence={"bad_ids": bad_ids})
                )
                review_reasons[reason] += 1
                review_by_field["architect_canonical_ids"] += 1
        else:
            fields["architect_canonical_ids"]["missing"] += 1
            source_arch_refs = [
                (str(meta.get("source")), str(arch_sid))
                for meta in members
                for arch_sid in meta.get("architect_source_ids") or []
                if str(arch_sid)
            ]
            resolved = sorted(
                {
                    arch_source_to_id[(source, sid)]
                    for source, sid in source_arch_refs
                    if (source, sid) in arch_source_to_id
                }
            )
            unresolved = [
                f"{source}:{sid}"
                for source, sid in source_arch_refs
                if (source, sid) not in arch_source_to_id
            ]
            source_texts = dedupe([meta.get("architects_text") for meta in members if meta.get("architects_text")])
            if resolved and not unresolved:
                fields["architect_canonical_ids"]["source_available_single"] += 1
                high_conf.append(
                    high_candidate(
                        row,
                        field="architect_canonical_ids",
                        proposed=resolved,
                        rule="source_architect_refs_resolve_to_registry",
                        evidence=[{"source_ref": f"{s}:{sid}", "canonical_arch_id": arch_source_to_id[(s, sid)]} for s, sid in source_arch_refs],
                    )
                )
                high_by_field["architect_canonical_ids"] += 1
            elif source_arch_refs:
                fields["architect_canonical_ids"]["unresolved_source_refs"] += 1
                reason = "architect_source_refs_not_fully_clustered"
                review_needed.append(
                    review_item(
                        row,
                        field="architect_canonical_ids",
                        reason=reason,
                        evidence={"resolved": resolved, "unresolved": unresolved[:12], "source_texts": source_texts[:8]},
                    )
                )
                review_reasons[reason] += 1
                review_by_field["architect_canonical_ids"] += 1
            elif source_texts:
                fields["architect_canonical_ids"]["text_only"] += 1
                reason = "architect_text_only_no_registry_ref"
                review_needed.append(
                    review_item(row, field="architect_canonical_ids", reason=reason, evidence={"source_texts": source_texts[:8]})
                )
                review_reasons[reason] += 1
                review_by_field["architect_canonical_ids"] += 1
            else:
                fields["architect_canonical_ids"]["source_missing"] += 1

        if arch_names_current:
            fields["architect_names"]["complete"] += 1
        else:
            fields["architect_names"]["missing"] += 1
            if arch_ids and all(arch_names.get(aid) for aid in arch_ids):
                proposed = [arch_names[aid][0] for aid in arch_ids]
                fields["architect_names"]["source_available_single"] += 1
                high_conf.append(
                    high_candidate(
                        row,
                        field="architect_names",
                        proposed=proposed,
                        rule="canonical_architect_registry_names_available",
                        evidence=[{"canonical_arch_id": aid, "names": arch_names[aid][:3]} for aid in arch_ids],
                    )
                )
                high_by_field["architect_names"] += 1
            else:
                source_texts = dedupe([meta.get("architects_text") for meta in members if meta.get("architects_text")])
                if len(source_texts) == 1:
                    fields["architect_names"]["source_available_single"] += 1
                    high_conf.append(
                        high_candidate(
                            row,
                            field="architect_names",
                            proposed=[source_texts[0]],
                            rule="single_source_architect_text",
                            evidence=[{"architects_text": source_texts[0]}],
                        )
                    )
                    high_by_field["architect_names"] += 1
                elif len(source_texts) > 1:
                    fields["architect_names"]["conflicting_sources"] += 1
                    reason = "multiple_source_architect_texts"
                    review_needed.append(
                        review_item(row, field="architect_names", reason=reason, evidence={"source_texts": source_texts[:12]})
                    )
                    review_reasons[reason] += 1
                    review_by_field["architect_names"] += 1
                else:
                    fields["architect_names"]["source_missing"] += 1

        url_candidates = source_url_candidates(row, members)
        if url_candidates:
            fields["source_urls"]["source_available_single"] += 1
            high_conf.append(
                high_candidate(
                    row,
                    field="source_urls",
                    proposed={**(row.get("source_urls") or {}), **url_candidates},
                    rule="source_ref_url_reconstructable_from_source_db",
                    evidence=[{"source": source, "urls": urls} for source, urls in sorted(url_candidates.items())],
                )
            )
            high_by_field["source_urls"] += 1
        else:
            fields["source_urls"]["complete_or_no_candidate"] += 1

        all_images_missing = is_missing(row.get("all_images"))
        display_cover_missing = is_missing(row.get("display_cover_url"))
        if not all_images_missing:
            fields["all_images"]["complete"] += 1
        else:
            fields["all_images"]["missing"] += 1
        if not display_cover_missing:
            fields["display_cover_url"]["complete"] += 1
        else:
            fields["display_cover_url"]["missing"] += 1
        if all_images_missing or display_cover_missing:
            summary = image_source_summary(members)
            if summary["source_image_count"] or summary["source_cover_count"]:
                reason = "source_images_exist_but_final_image_fields_missing"
                fields["all_images"]["source_available_single"] += 1
                review_needed.append(
                    review_item(row, field="image_fields", reason=reason, evidence=summary)
                )
                review_reasons[reason] += 1
                review_by_field["image_fields"] += 1
            else:
                fields["all_images"]["source_missing"] += 1

        for field in ENRICHMENT_FIELDS:
            if is_missing(row.get(field)):
                fields[field]["missing"] += 1
            else:
                fields[field]["complete"] += 1

    inventory = {
        "status": "PASS",
        "mode": "read-only",
        "input": str(input_path),
        "limit": limit,
        "total_rows": total_rows,
        "fields": {field: dict(counter) for field, counter in sorted(fields.items())},
        "source_ref_missing": dict(source_ref_missing),
        "source_stats": {source: dict(counter) for source, counter in sorted(source_stats.items())},
        "examples": examples,
        "writes": "none",
    }
    high_report = {
        "status": "PASS",
        "mode": "read-only",
        "input": str(input_path),
        "total_candidates": len(high_conf),
        "by_field": dict(high_by_field),
        "candidates": high_conf,
        "writes": "none",
    }
    review_report = {
        "status": "PASS",
        "mode": "read-only",
        "input": str(input_path),
        "total_review_items": len(review_needed),
        "by_field": dict(review_by_field),
        "by_reason": dict(review_reasons),
        "items": review_needed,
        "writes": "none",
    }
    return inventory, high_report, review_report


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def md_table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    header = "| " + " | ".join(str(v) for v in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in rows[1:]]
    return "\n".join([header, sep, *body])


def write_inventory_md(path: Path, inventory: dict[str, Any], high: dict[str, Any], review: dict[str, Any]) -> None:
    field_rows = [["field", "complete", "missing", "source_available", "conflict", "text_extractable", "source_missing"]]
    for field, counts in inventory["fields"].items():
        field_rows.append(
            [
                field,
                counts.get("complete", 0),
                counts.get("missing", 0),
                counts.get("source_available_single", 0),
                counts.get("conflicting_sources", 0),
                counts.get("text_extractable", 0),
                counts.get("source_missing", 0),
            ]
        )

    high_rows = [["field", "candidates"]]
    for field, count in sorted(high["by_field"].items(), key=lambda item: (-item[1], item[0])):
        high_rows.append([field, count])

    review_rows = [["reason", "items"]]
    for reason, count in sorted(review["by_reason"].items(), key=lambda item: (-item[1], item[0])):
        review_rows.append([reason, count])

    text = f"""# canonical_v2 gap inventory

Generated by `tools/canonical_v2_gap_inventory.py`.

Mode: read-only.

Input: `{inventory["input"]}`

Total rows inspected: {inventory["total_rows"]}

## Field inventory

{md_table(field_rows)}

## High-confidence backfill candidates

Total candidates: {high["total_candidates"]}

{md_table(high_rows)}

## Review-needed queue

Total review items: {review["total_review_items"]}

{md_table(review_rows)}

## Interpretation

`source_available` means the canonical value is missing and the structured
source DB values agree on a single value. These are the C2 high-confidence
candidates.

`text_extractable` and `conflict` are intentionally not auto-apply candidates.
They should stay in the review queue because choosing a value would require
semantic judgment or source-priority policy.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_review_md(path: Path, review: dict[str, Any]) -> None:
    rows = [["field", "reason", "count"]]
    for reason, count in sorted(review["by_reason"].items(), key=lambda item: (-item[1], item[0])):
        fields = sorted({item["field"] for item in review["items"] if item["reason"] == reason})
        rows.append([", ".join(fields), reason, count])
    text = f"""# canonical_v2 manual review queue

Mode: read-only.

Input: `{review["input"]}`

Total review items: {review["total_review_items"]}

{md_table(rows)}

Full row-level queue is in:
`data/reports/canonical_v2_backfill_candidates_review_needed.json`.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build canonical_v2 gap inventory and C2 backfill candidates.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--architects", type=Path, default=DEFAULT_ARCHITECTS)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--inventory-json", type=Path, default=DEFAULT_INVENTORY_JSON)
    parser.add_argument("--inventory-md", type=Path, default=DEFAULT_INVENTORY_MD)
    parser.add_argument("--high-confidence-json", type=Path, default=DEFAULT_HIGH_CONF_JSON)
    parser.add_argument("--review-json", type=Path, default=DEFAULT_REVIEW_JSON)
    parser.add_argument("--review-md", type=Path, default=DEFAULT_REVIEW_MD)
    args = parser.parse_args()

    inventory, high, review = analyze(
        input_path=args.input,
        architects_path=args.architects,
        limit=args.limit,
    )
    write_json(args.inventory_json, inventory)
    write_json(args.high_confidence_json, high)
    write_json(args.review_json, review)
    write_inventory_md(args.inventory_md, inventory, high, review)
    write_review_md(args.review_md, review)

    summary = {
        "status": "PASS",
        "total_rows": inventory["total_rows"],
        "high_confidence_candidates": high["total_candidates"],
        "review_needed_items": review["total_review_items"],
        "top_high_confidence_fields": dict(sorted(high["by_field"].items(), key=lambda item: (-item[1], item[0]))[:8]),
        "top_review_reasons": dict(sorted(review["by_reason"].items(), key=lambda item: (-item[1], item[0]))[:8]),
        "writes": "reports only",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
