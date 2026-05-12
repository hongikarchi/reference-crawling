#!/usr/bin/env python3
"""Read-only validator for the proposed canonical_v2_buildings upload path.

This tool does not import upload code and does not open Neon/R2 connections.
It maps the completed strict canonical JSON into the shape we expect to upload
later, validates the mapping, and writes a compact report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/canonical/canonical_buildings_strict_embedded.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_upload_dry_run.json"
EXPECTED_IMAGE_TYPES = {"exterior", "interior", "drawing", "aerial", "detail"}
VALID_TIERS = {"T1", "T2", "T3"}

REQUIRED_TEXT_FIELDS = (
    "canonical_bld_id",
    "name",
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "visual_description",
    "confidence_tier",
)

GENERIC_NAME_TOKENS = {
    "a",
    "an",
    "and",
    "apartment",
    "apartments",
    "building",
    "casa",
    "center",
    "centre",
    "gallery",
    "garden",
    "home",
    "house",
    "housing",
    "in",
    "of",
    "office",
    "pavilion",
    "private",
    "project",
    "residence",
    "school",
    "social",
    "summer",
    "the",
    "tower",
    "villa",
}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict)) and not value:
        return False
    return True


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def is_genericish_name(value: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    if not tokens:
        return False
    distinctive = [t for t in tokens if t not in GENERIC_NAME_TOKENS and len(t) > 1]
    return len(distinctive) <= 1


def map_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return the exact row shape proposed for canonical_v2_buildings."""
    return {
        "canonical_bld_id": row.get("canonical_bld_id"),
        "name": row.get("name"),
        "names_alts": row.get("names_alts") or [],
        "location_city": row.get("location_city"),
        "location_country": row.get("location_country"),
        "project_year": row.get("project_year"),
        "architect_canonical_ids": row.get("architect_canonical_ids") or [],
        "architect_names": row.get("architect_names") or [],
        "architects_text": row.get("architects_text"),
        "program": row.get("program"),
        "style": row.get("style"),
        "color_tone": row.get("color_tone"),
        "atmosphere": row.get("atmosphere"),
        "material_visual": row.get("material_visual") or [],
        "visual_description": row.get("visual_description"),
        "image_derived": row.get("image_derived") or {},
        "covers_by_type": row.get("covers_by_type") or {},
        "all_images": row.get("all_images") or [],
        "best_image_per_cluster": row.get("best_image_per_cluster") or {},
        "source_refs": row.get("source_refs") or {},
        "identity_source": row.get("identity_source"),
        "confidence_tier": row.get("confidence_tier"),
        "n_sources": row.get("n_sources"),
        "cover_image_url_default": row.get("cover_image_url_default"),
        "embedding": row.get("embedding"),
    }


def _embedding_is_valid(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 384
        and all(isinstance(v, (int, float)) for v in value)
    )


def _covers_are_valid(value: Any) -> bool:
    return isinstance(value, dict) and set(value.keys()) == EXPECTED_IMAGE_TYPES


def _source_refs_are_valid(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    for source, ids in value.items():
        if not isinstance(source, str) or not source:
            return False
        if not isinstance(ids, list) or not ids:
            return False
        if any(not isinstance(source_id, (str, int)) or str(source_id) == "" for source_id in ids):
            return False
    return True


def _compact_sample(mapped: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_bld_id": mapped["canonical_bld_id"],
        "name": mapped["name"],
        "location_country": mapped["location_country"],
        "project_year": mapped["project_year"],
        "architect_names": mapped["architect_names"][:3],
        "n_sources": mapped["n_sources"],
        "source_refs": mapped["source_refs"],
        "covers_present": sorted(k for k, v in mapped["covers_by_type"].items() if v),
        "all_images_count": len(mapped["all_images"]),
        "embedding_dim": len(mapped["embedding"]) if isinstance(mapped["embedding"], list) else None,
    }


def validate_rows(rows: Iterable[dict[str, Any]], *, sample_limit: int = 5) -> dict[str, Any]:
    failures: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    seen: set[str] = set()
    duplicate_pks: list[str] = []
    by_name: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    samples: list[dict[str, Any]] = []
    total = 0

    for raw in rows:
        total += 1
        mapped = map_row(raw)
        cid = mapped.get("canonical_bld_id")

        if len(samples) < sample_limit:
            samples.append(_compact_sample(mapped))

        for field in REQUIRED_TEXT_FIELDS:
            if not _present(mapped.get(field)):
                failures["missing_required"] += 1
                break

        if not isinstance(cid, str) or not cid:
            failures["bad_pk"] += 1
        elif cid in seen:
            failures["duplicate_pk"] += 1
            duplicate_pks.append(cid)
        else:
            seen.add(cid)

        if mapped.get("confidence_tier") not in VALID_TIERS:
            failures["invalid_confidence_tier"] += 1
        if not isinstance(mapped.get("material_visual"), list) or not mapped["material_visual"]:
            failures["bad_material_visual"] += 1
        if not _covers_are_valid(mapped.get("covers_by_type")):
            failures["bad_covers_by_type"] += 1
        if not _source_refs_are_valid(mapped.get("source_refs")):
            failures["bad_source_refs"] += 1
        if not _embedding_is_valid(mapped.get("embedding")):
            failures["bad_embedding"] += 1

        if not mapped.get("all_images"):
            warnings["empty_all_images"] += 1
        if not mapped.get("cover_image_url_default"):
            warnings["missing_cover_image_url_default"] += 1
        if not mapped.get("location_country"):
            warnings["missing_location_country"] += 1
        if not mapped.get("location_city"):
            warnings["missing_location_city"] += 1
        if mapped.get("project_year") is None:
            warnings["missing_project_year"] += 1
        if not mapped.get("architect_canonical_ids"):
            warnings["missing_architect_canonical_ids"] += 1
        if is_genericish_name(str(mapped.get("name") or "")):
            warnings["genericish_name_rows"] += 1

        name = normalize_name(str(mapped.get("name") or ""))
        if name:
            by_name[name].append(
                {
                    "canonical_bld_id": cid,
                    "country": mapped.get("location_country"),
                    "city": mapped.get("location_city"),
                    "year": mapped.get("project_year"),
                    "architect_names": mapped.get("architect_names")[:2],
                    "n_sources": mapped.get("n_sources"),
                }
            )

        try:
            json.dumps(mapped, ensure_ascii=False)
        except (TypeError, ValueError):
            failures["bad_json_serialization"] += 1

    duplicate_name_groups = {name: group for name, group in by_name.items() if len(group) > 1}
    generic_duplicate_name_groups = {
        name: group for name, group in duplicate_name_groups.items() if is_genericish_name(name)
    }
    warnings["duplicate_name_groups"] = len(duplicate_name_groups)
    warnings["generic_duplicate_name_groups"] = len(generic_duplicate_name_groups)

    duplicate_name_samples = [
        {"name": name, "count": len(group), "rows": group[:8]}
        for name, group in sorted(
            duplicate_name_groups.items(), key=lambda item: (-len(item[1]), item[0])
        )[:25]
    ]

    return {
        "status": "FAIL" if failures else "PASS",
        "total_rows": total,
        "unique_pk": len(seen),
        "failures": dict(failures),
        "warnings": dict(warnings),
        "duplicate_pk_samples": duplicate_pks[:25],
        "duplicate_name_samples": duplicate_name_samples,
        "sample_rows": samples,
    }


def iter_buildings(path: Path, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Stream objects from the top-level `buildings` array.

    The embedded canonical file is about 1GB, so this avoids materializing the
    full file for validation.
    """
    decoder = json.JSONDecoder()
    key = '"buildings"'
    with path.open("r", encoding="utf-8") as f:
        buffer = ""
        found = False
        yielded = 0

        while not found:
            chunk = f.read(1024 * 1024)
            if not chunk:
                raise ValueError(f"{path} has no top-level buildings array")
            buffer += chunk
            key_idx = buffer.find(key)
            if key_idx == -1:
                buffer = buffer[-len(key) :]
                continue
            bracket_idx = buffer.find("[", key_idx + len(key))
            if bracket_idx == -1:
                continue
            buffer = buffer[bracket_idx + 1 :]
            found = True

        while True:
            stripped = buffer.lstrip()
            buffer = stripped
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if limit is not None and yielded >= limit:
                return

            try:
                obj, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    raise
                buffer += chunk
                continue

            if not isinstance(obj, dict):
                raise ValueError("buildings array contains a non-object item")
            yielded += 1
            yield obj
            buffer = buffer[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate canonical_v2 upload mapping")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()

    rows = iter_buildings(args.input, limit=args.limit)
    report = validate_rows(rows, sample_limit=args.sample_limit)
    report.update(
        {
            "input": str(args.input),
            "limit": args.limit,
            "table": "canonical_v2_buildings",
            "writes": "none; read-only dry-run",
        }
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps({k: report[k] for k in ("status", "total_rows", "unique_pk", "failures", "warnings")}, indent=2, ensure_ascii=False))
    print(f"report: {args.report}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
